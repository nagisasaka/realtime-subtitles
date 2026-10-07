"""Incremental WAV input with the same 24 kHz PCM interface as Microphone.

AgentSttClient performs the existing final 16 kHz conversion. No output device is
opened, and no normalization, denoising, silence removal or speed change occurs.
"""

import math
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .audio import OUTPUT_RATE, AudioConverter, AudioError, LatestQueue


class RealtimePacer:
    """Absolute deadlines; after a stall rebase rather than burst to catch up."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock()
        self.last = None

    def delay(self, seconds):
        self.deadline += seconds
        now = self.clock()
        if self.last is not None and now > self.deadline + 0.02:
            self.deadline = now + seconds
        return max(0, self.deadline - now)

    def sent(self):
        self.last = self.clock()


def inspect_wav(path):
    """Read only the header. Error messages deliberately omit the absolute path."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate, channels = wav.getframerate(), wav.getnchannels()
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise AudioError("PCM16 WAVを選択してください。")
            if channels not in (1, 2) or not 8000 <= rate <= 192000 or not wav.getnframes():
                raise AudioError("WAVは8–192kHz・mono/stereo・空でない音声が必要です。")
            return {
                "input_source": "audio_file",
                "filename": Path(path).name,
                "audio_duration_ms": round(wav.getnframes() * 1000 / rate),
                "audio_sample_rate": rate,
                "audio_channels": channels,
                "audio_frames": wav.getnframes(),
                "audio_format": "WAV / PCM16",
            }
    except FileNotFoundError:
        raise AudioError("音声ファイルが見つかりません。再選択してください。") from None
    except (wave.Error, EOFError, OSError):
        raise AudioError("WAVを読めません。PCM16形式・アクセス権を確認してください。") from None


class AudioFileSource:
    def __init__(self, path, *, clock=time.monotonic, waiter=None):
        self.path = Path(path)
        self.clock = clock
        self.stop_event = threading.Event()
        self.waiter = waiter or self.stop_event.wait
        self.frames = LatestQueue(2)
        self.worker = None
        self.error = None
        self.finished = False
        self.info = {}
        self.sent_samples = 0
        self.rms, self.dbfs = 0.0, -120.0

    def prepare(self):
        self.info = inspect_wav(self.path)

    def start(self):
        if self.worker and self.worker.is_alive():
            return False
        self.stop_event.clear()
        self.finished = False
        self.sent_samples = 0
        self.error = None
        self.frames = LatestQueue(2)
        self.worker = threading.Thread(target=self._read, name="audio-file", daemon=False)
        self.worker.start()
        return True

    def _read(self):
        pacer = RealtimePacer(self.clock)

        def emit(pcm):
            if self.waiter(pacer.delay(len(pcm) / 2 / OUTPUT_RATE)):
                return False
            # File replay pauses under backpressure instead of losing recording samples.
            while not self.stop_event.is_set():
                try:
                    self.frames.put((self.clock(), pcm), timeout=0.05)
                    pacer.sent()
                    return True
                except queue.Full:
                    continue
            return False

        try:
            with wave.open(str(self.path), "rb") as wav:
                converter = AudioConverter(wav.getframerate())
                block = max(1, wav.getframerate() // 5)
                read_samples = 0
                while not self.stop_event.is_set():
                    data = wav.readframes(block)
                    if not data:
                        if read_samples != wav.getnframes():
                            raise AudioError("WAVの音声データが途中で切れています。")
                        for frame in converter.finish(pad=False):
                            if not emit(frame):
                                return
                        self.finished = True
                        return
                    if len(data) % (2 * wav.getnchannels()):
                        raise AudioError("WAVの音声データが壊れています。")
                    samples = np.frombuffer(data, dtype="<i2").reshape(-1, wav.getnchannels())
                    read_samples += len(samples)
                    for frame in converter.feed(samples.astype(np.float32) / 32768):
                        if not emit(frame):
                            return
        except Exception as exc:
            self.error = exc if isinstance(exc, AudioError) else AudioError("WAV読み込みエラー")

    def note_sent(self, samples):
        self.sent_samples += samples

    def meter(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
        self.rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0
        self.dbfs = max(-120, 20 * math.log10(max(self.rms, 1e-6)))

    def check_health(self):
        if self.error:
            raise self.error

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(2)
            if self.worker.is_alive():
                raise AudioError("音声ファイルworkerが停止しません。")

    def diagnostics(self):
        return {
            **self.info,
            "position_ms": min(
                self.info.get("audio_duration_ms", 0), round(self.sent_samples * 1000 / OUTPUT_RATE)
            ),
            "audio_queue": self.frames.qsize(),
            "dbfs": self.dbfs,
            "rms": self.rms,
            "output_rate": OUTPUT_RATE,
            "output_channels": 1,
            "output_format": "PCM16 little endian",
        }
