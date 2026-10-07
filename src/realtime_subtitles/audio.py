"""Capture callbacks only copy audio; conversion runs on a separate worker."""

import math
import queue
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
import soxr

OUTPUT_RATE = 24_000
FRAME_SAMPLES = 4_800
FRAME_BYTES = FRAME_SAMPLES * 2


class LatestQueue(queue.Queue):
    """Bounded, single-producer queue that drops oldest entries on overflow."""

    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.dropped = 0
        self.sinks = ()

    def put_latest(self, item):
        for sink in self.sinks:
            sink.put_latest(item)
        while True:
            try:
                self.put_nowait(item)
                return
            except queue.Full:
                try:
                    self.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass


def mono_float(samples):
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 2:
        data = data.mean(axis=1)
    elif data.ndim != 1:
        raise ValueError("Audio must be [samples] or [samples, channels]")
    return np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=-1.0)


def pcm16(samples):
    data = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(np.rint(np.clip(data, -1, 1) * 32768), -32768, 32767).astype("<i2").tobytes()


class SpeechPause:
    """Paragraph hint only: detect speech resuming after >= 1 s below -45 dBFS."""

    def __init__(self, silence_seconds=1.0, threshold_dbfs=-45):
        self.minimum = round(silence_seconds * OUTPUT_RATE)
        self.threshold = 10 ** (threshold_dbfs / 20)
        self.quiet_samples = 0
        self.heard_speech = False

    def feed(self, frame):
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0
        if rms < self.threshold:
            self.quiet_samples = min(self.minimum, self.quiet_samples + samples.size)
            return False
        resumed = self.heard_speech and self.quiet_samples >= self.minimum
        self.quiet_samples = 0
        self.heard_speech = True
        return resumed


class AudioConverter:
    def __init__(self, input_rate):
        self.resampler = (
            soxr.ResampleStream(input_rate, OUTPUT_RATE, 1, dtype="float32", quality="HQ")
            if input_rate != OUTPUT_RATE
            else None
        )
        self.pending = bytearray()

    def feed(self, samples, *, last=False):
        mono = mono_float(samples)
        if self.resampler is not None:
            mono = self.resampler.resample_chunk(mono, last=last)
        self.pending.extend(pcm16(mono))
        chunks = []
        while len(self.pending) >= FRAME_BYTES:
            chunks.append(bytes(self.pending[:FRAME_BYTES]))
            del self.pending[:FRAME_BYTES]
        return chunks

    def finish(self, *, pad=True):
        chunks = self.feed(np.empty(0, dtype=np.float32), last=True)
        if self.pending:
            tail = bytes(self.pending)
            chunks.append(tail.ljust(FRAME_BYTES, b"\0") if pad else tail)
            self.pending.clear()
        return chunks


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    sample_rate: int
    max_channels: int
    is_default: bool = False

    @property
    def key(self):
        return f"{self.hostapi}|{self.name}"

    @property
    def label(self):
        return f"{'★ ' if self.is_default else ''}{self.name} [{self.hostapi}] ({self.index})"


def list_microphones():
    require_windows()
    import sounddevice as sd

    default = sd.default.device[0]
    apis = sd.query_hostapis()
    return [
        InputDevice(
            i,
            d["name"],
            apis[d["hostapi"]]["name"],
            int(d["default_samplerate"]),
            d["max_input_channels"],
            i == default,
        )
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


class AudioError(Exception):
    pass


def require_windows():
    if sys.platform != "win32":
        raise AudioError("Windows側のPythonで実行してください。WSL/WSLg音声入力は対象外です。")


class Microphone:
    """Native-rate float32 capture -> bounded raw queue -> PCM frame queue."""

    def __init__(self, device_index=None):
        self.device_index = device_index
        self.raw = LatestQueue(8)  # 8 x 50 ms = 400 ms
        self.frames = LatestQueue(3)  # 3 x 200 ms = 600 ms
        self.stop_event = threading.Event()
        self.stream = None
        self.worker = None
        self.error = None
        self.dbfs = -120.0
        self.rms = 0.0
        self.overflows = 0
        self.last_capture = 0.0
        self.info = {}

    def prepare(self):
        require_windows()
        import sounddevice as sd

        try:
            info = sd.query_devices(self.device_index, "input")
            rate = int(info["default_samplerate"])
            # Most microphones expose mono or stereo. Try mono first, then native channels.
            candidates = list(
                dict.fromkeys([1, min(2, info["max_input_channels"]), info["max_input_channels"]])
            )
            for channels in candidates:
                if channels < 1:
                    continue
                try:
                    sd.check_input_settings(
                        device=self.device_index,
                        channels=channels,
                        samplerate=rate,
                        dtype="float32",
                    )
                    break
                except sd.PortAudioError:
                    continue
            else:
                raise AudioError("マイクのnative sample rate / float32入力を開けません。")
            self.info = {
                "device": info["name"],
                "input_rate": rate,
                "input_channels": channels,
                "input_format": "float32",
                "output_rate": OUTPUT_RATE,
                "output_channels": 1,
                "output_format": "PCM16 little endian",
            }
        except (sd.PortAudioError, ValueError) as exc:
            raise AudioError(f"マイクを確認できません: {exc}") from None

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.overflows += 1
        self.last_capture = time.monotonic()
        self.raw.put_latest((self.last_capture, indata.copy()))

    def start(self):
        import sounddevice as sd

        try:
            self.stream = sd.InputStream(
                device=self.device_index,
                samplerate=self.info["input_rate"],
                channels=self.info["input_channels"],
                dtype="float32",
                latency="low",
                blocksize=max(1, self.info["input_rate"] // 20),
                callback=self._callback,
            )
            self.worker = threading.Thread(target=self._convert, name="audio-convert")
            self.worker.start()
            self.last_capture = time.monotonic()
            self.stream.start()
        except Exception as exc:
            self.stop()
            raise AudioError(f"マイクを開始できません: {exc}") from None

    def _convert(self):
        converter = AudioConverter(self.info["input_rate"])
        try:
            while not self.stop_event.is_set() or not self.raw.empty():
                try:
                    captured_at, data = self.raw.get(timeout=0.05)
                except queue.Empty:
                    continue
                if time.monotonic() - captured_at > 0.5:
                    self.raw.dropped += 1
                    continue
                mono = mono_float(data)
                self.rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
                self.dbfs = max(-120.0, 20 * math.log10(max(self.rms, 1e-6)))
                for frame in converter.feed(mono):
                    self.frames.put_latest((captured_at, frame))
            for frame in converter.finish():
                self.frames.put_latest((time.monotonic(), frame))
        except Exception as exc:
            self.error = AudioError(f"音声変換エラー: {type(exc).__name__}: {exc}")

    def check_health(self):
        if self.error:
            raise self.error
        if self.stream is not None and (
            not self.stream.active or time.monotonic() - self.last_capture > 3
        ):
            raise AudioError("マイク入力が停止しました。接続・プライバシー設定を確認してください。")

    def stop(self):
        try:
            if self.stream is not None:
                self.stream.abort()
                self.stream.close()
                self.stream = None
        finally:
            self.stop_event.set()
            if self.worker is not None:
                self.worker.join(timeout=2)
                if self.worker.is_alive():
                    raise AudioError("音声workerが終了しませんでした。")
                self.worker = None

    def diagnostics(self):
        return {
            **self.info,
            "rms": self.rms,
            "dbfs": self.dbfs,
            "raw_queue": self.raw.qsize(),
            "audio_queue": self.frames.qsize(),
            "dropped_blocks": self.raw.dropped,
            "dropped_frames": self.frames.dropped,
            "input_overflows": self.overflows,
        }
