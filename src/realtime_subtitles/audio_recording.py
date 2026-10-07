"""Nonblocking 24 kHz PCM fan-out to recoverable, periodically flushed WAV files."""

import json
import os
import queue
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path

from .audio import OUTPUT_RATE

ROTATE_SECONDS = 1800


def default_directory():
    return Path.home() / "RealtimeSubtitles" / "Recordings"


class AudioRecorder:
    def __init__(self, directory=None, *, queue_size=100, rotate_seconds=ROTATE_SECONDS):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
        self.directory = Path(directory or default_directory()) / stamp
        self.frames = queue.Queue(queue_size)
        self.rotate_samples = round(rotate_seconds * OUTPUT_RATE)
        self.samples = self.written_samples = self.dropped_samples = 0
        self.error = ""
        self.files = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="audio-recorder", daemon=True)
        self.thread.start()

    def put_latest(self, item):
        """Called by the PCM producer; never waits for disk or queue space."""
        _, pcm = item
        start = self.samples
        self.samples += len(pcm) // 2
        if self.error:
            return
        try:
            self.frames.put_nowait((start, pcm))
        except queue.Full:
            self.dropped_samples += len(pcm) // 2

    def close(self, timeout=5):
        self._stop.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.error = "Recording close timeout"
        return not self.thread.is_alive() and not self.error

    def snapshot(self):
        return {
            "directory": str(self.directory),
            "files": list(self.files),
            "sample_rate": OUTPUT_RATE,
            "channels": 1,
            "format": "PCM16",
            "captured_seconds": self.samples / OUTPUT_RATE,
            "written_seconds": self.written_samples / OUTPUT_RATE,
            "dropped_samples": self.dropped_samples,
            "error": self.error,
            "active": self.thread.is_alive(),
        }

    def _run(self):
        handle = wav = None
        file_samples = synced = 0
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
            with (self.directory / "gaps.jsonl").open("x", encoding="utf-8") as gaps:

                def write(pcm):
                    nonlocal handle, wav, file_samples, synced
                    while pcm:
                        if wav is None:
                            name = f"audio-{len(self.files):04d}.wav"
                            handle = (self.directory / name).open("xb")
                            wav = wave.open(handle, "wb")
                            wav.setnchannels(1)
                            wav.setsampwidth(2)
                            wav.setframerate(OUTPUT_RATE)
                            self.files.append(name)
                            file_samples = synced = 0
                        count = min(len(pcm) // 2, self.rotate_samples - file_samples)
                        # writeframes patches the WAV header after every chunk.
                        wav.writeframes(pcm[: count * 2])
                        pcm = pcm[count * 2 :]
                        file_samples += count
                        self.written_samples += count
                        if file_samples - synced >= OUTPUT_RATE:
                            handle.flush()
                            os.fsync(handle.fileno())
                            synced = file_samples
                        if file_samples == self.rotate_samples:
                            wav.close()
                            wav = None
                            handle.close()
                            handle = None

                def fill_gap(end):
                    if end <= self.written_samples:
                        return
                    gaps.write(
                        json.dumps(
                            {
                                "start_sample": self.written_samples,
                                "end_sample": end,
                                "reason": "recording_queue_overflow",
                                "replacement": "silence",
                            }
                        )
                        + "\n"
                    )
                    gaps.flush()
                    while self.written_samples < end:
                        write(bytes(min(OUTPUT_RATE, end - self.written_samples) * 2))

                while not self._stop.is_set() or not self.frames.empty():
                    try:
                        start, pcm = self.frames.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    fill_gap(start)
                    write(pcm)
                fill_gap(self.samples)
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            try:
                if wav:
                    wav.close()
                if handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                    handle.close()
            except Exception as exc:
                self.error = type(exc).__name__
