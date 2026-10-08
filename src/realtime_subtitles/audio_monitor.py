"""Best-effort local playback, isolated from recognition and translation workers."""

import queue
import threading
import time

import numpy as np
import soxr

from .audio import OUTPUT_RATE, LatestQueue, require_windows


class AudioMonitor:
    def __init__(self, *, output_factory=None):
        self.frames = LatestQueue(3)
        self.cancel = threading.Event()
        self.finish = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.error = ""
        self.device = ""
        self.played_samples = 0
        self.underflows = 0
        self.output_latency_ms = None
        self.output_factory = output_factory or self._open_output

    def _open_output(self):
        require_windows()
        import sounddevice as sd

        info = sd.query_devices(kind="output")
        self.device = info["name"]
        rate = int(info["default_samplerate"])
        channels = min(2, info["max_output_channels"])
        stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", latency=0.1)
        self.output_latency_ms = round(stream.latency * 1000)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream, rate, channels

    def start(self):
        # A broken optional device driver must not keep the whole app alive.
        self.thread = threading.Thread(target=self._run, name="audio-monitor", daemon=True)
        self.thread.start()

    def submit(self, pcm):
        if not self.cancel.is_set() and not self.finish.is_set() and not self.error:
            self.frames.put_latest((time.monotonic(), pcm))

    def stop(self):
        # Nonblocking, safe to call from Tk. No more queued audio is played.
        self.cancel.set()

    def close(self, *, drain=False):
        self.finish.set()
        if not drain:
            self.stop()
        if self.thread:
            self.thread.join(2)
            if self.thread.is_alive():
                self.stop()
                self.error = "音声モニターの終了が遅れています。"

    def _run(self):
        stream = None
        try:
            stream, rate, channels = self.output_factory()
            self.ready.set()
            resampler = (
                soxr.ResampleStream(OUTPUT_RATE, rate, 1, dtype="int16")
                if rate != OUTPUT_RATE
                else None
            )

            def write(samples):
                if len(samples):
                    if channels > 1:
                        samples = np.repeat(samples[:, None], channels, axis=1)
                    if stream.write(samples.astype("<i2").tobytes()):
                        self.underflows += 1

            while not self.cancel.is_set():
                try:
                    captured, pcm = self.frames.get(timeout=0.05)
                except queue.Empty:
                    if self.finish.is_set():
                        break
                    continue
                if time.monotonic() - captured > 0.6:
                    self.frames.dropped += 1
                    continue
                samples = np.frombuffer(pcm, dtype="<i2")
                write(resampler.resample_chunk(samples) if resampler else samples)
                self.played_samples += len(samples)
            if not self.cancel.is_set():
                if resampler:
                    write(resampler.resample_chunk(np.empty(0, dtype="int16"), last=True))
                stream.stop()
        except Exception as exc:
            self.error = (
                f"音声モニターを再生できません ({type(exc).__name__})。"
                "Windowsの出力先を確認してください。"
            )
        finally:
            self.ready.set()
            if stream:
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    self.error = self.error or "音声モニターを閉じられませんでした。"
            while not self.frames.empty():
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    break

    def snapshot(self):
        return {
            "device": self.device,
            "error": self.error,
            "played_samples": self.played_samples,
            "dropped_frames": self.frames.dropped,
            "queue": self.frames.qsize(),
            "underflows": self.underflows,
            "output_latency_ms": self.output_latency_ms,
        }
