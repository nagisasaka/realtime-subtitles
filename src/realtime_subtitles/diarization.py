"""Best-effort delayed diarization. No audio device or Realtime connection ownership."""

import io
import json
import logging
import logging.handlers
import math
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from .audio import OUTPUT_RATE, LatestQueue
from .settings import settings_path
from .speaker_timeline import SpeakerSegment, segment_boundaries

DIARIZATION_ENABLED = True
DIARIZATION_WINDOW_SEC = 30
DIARIZATION_OVERLAP_SEC = 5
DIARIZATION_MODEL = "gpt-4o-transcribe-diarize"
DIARIZATION_TIMEOUT_SEC = 40
MAX_JOB_AGE_SEC = 60


class Diarizer(Protocol):
    def diarize(self, audio: bytes, start_time_ms: int) -> list[SpeakerSegment]: ...


class DiarizationError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUTPUT_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


class OpenAIDiarizer:
    def __init__(
        self,
        key,
        *,
        endpoint="https://api.openai.com/v1/audio/transcriptions",
        model=DIARIZATION_MODEL,
        timeout=DIARIZATION_TIMEOUT_SEC,
    ):
        self._key, self.endpoint, self.model, self.timeout = key, endpoint, model, timeout

    def diarize(self, audio, start_time_ms):
        boundary = "subtitle-" + uuid.uuid4().hex
        parts = []
        for name, value in [
            ("model", self.model),
            ("response_format", "diarized_json"),
            ("chunking_strategy", "auto"),
            ("language", "en"),
        ]:
            parts.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{name}"\r\n\r\n{value}\r\n'
                ).encode()
            )
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="window.wav"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode()
        )
        parts.extend([audio, f"\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(
            self.endpoint,
            data=b"".join(parts),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise ValueError("response size")
            with wave.open(io.BytesIO(audio), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            return self.parse_response(json.loads(payload), start_time_ms, duration)
        except urllib.error.HTTPError as exc:
            # Do not propagate headers, response bodies or request objects into logs.
            raise DiarizationError(f"HTTP {exc.code}", exc.code == 429 or exc.code >= 500) from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise DiarizationError("Network/timeout", True) from None
        except (ValueError, TypeError, KeyError, wave.Error):
            # One bad window must not disable diarization for the whole capture.
            # The worker retries only with the next fresh window, never in a tight loop.
            raise DiarizationError("Malformed diarization response", True) from None

    @staticmethod
    def parse_response(payload, start_time_ms, duration_sec):
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise ValueError("segments required")
        segments = []
        for row in payload["segments"]:
            speaker, start, end, text = row["speaker"], row["start"], row["end"], row.get("text")
            if (
                not isinstance(speaker, str)
                or not speaker
                or type(start) not in (int, float)
                or type(end) not in (int, float)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end <= duration_sec + 0.25
                or (text is not None and not isinstance(text, str))
            ):
                raise ValueError("invalid segment")
            segments.append(
                SpeakerSegment(
                    speaker,
                    start_time_ms + round(start * 1000),
                    start_time_ms + round(end * 1000),
                    text,
                )
            )
        return segments


@dataclass(frozen=True)
class AudioChunk:
    start_ms: int
    end_ms: int
    pcm: bytes


@dataclass(frozen=True)
class AudioWindow:
    timeline_id: str
    start_ms: int
    end_ms: int
    pcm: bytes
    created_at: float


class WindowRing:
    """At most one window of PCM. Missing frames invalidate, rather than compress, time."""

    def __init__(self, window_sec=DIARIZATION_WINDOW_SEC, overlap_sec=DIARIZATION_OVERLAP_SEC):
        self.window_ms = round(window_sec * 1000)
        self.step_ms = round((window_sec - overlap_sec) * 1000)
        if not 0 < self.step_ms <= self.window_ms:
            raise ValueError("Invalid window/overlap")
        self.frames = deque()
        self.timeline_id = None
        self.next_start = 0
        self.last_end = None

    def feed(self, timeline_id, start_ms, pcm):
        duration = len(pcm) * 1000 // (OUTPUT_RATE * 2)
        if timeline_id != self.timeline_id or self.last_end != start_ms:
            self.frames.clear()
            self.timeline_id, self.next_start = timeline_id, start_ms
        self.last_end = start_ms + duration
        self.frames.append((start_ms, self.last_end, pcm))
        result = []
        while self.last_end >= self.next_start + self.window_ms:
            end_ms = self.next_start + self.window_ms
            parts = []
            for start, end, frame in self.frames:
                if end > self.next_start and start < end_ms:
                    low = max(0, self.next_start - start) * OUTPUT_RATE * 2 // 1000
                    high = min(end - start, end_ms - start) * OUTPUT_RATE * 2 // 1000
                    parts.append(frame[low:high])
            result.append(
                AudioWindow(timeline_id, self.next_start, end_ms, b"".join(parts), time.monotonic())
            )
            self.next_start += self.step_ms
            while self.frames and self.frames[0][1] <= self.next_start:
                self.frames.popleft()
        return result


class AudioTimelineTap:
    """Constant-size enqueue at the existing PCM fan-out; no HTTP, logging or DSP here."""

    def __init__(self, timeline_id, frames):
        self.timeline_id, self.frames = timeline_id, frames
        self.started_at = time.monotonic()
        self.next_ms = 0
        self.total_samples = 0
        self.sent_ms = 0
        self._identities = OrderedDict()
        self._sent = deque(maxlen=600)  # 120 seconds for late transcript timestamps.
        self.error = ""
        self.active = True

    def put_latest(self, item):
        if not self.active:
            return
        try:
            _, pcm = item
            start_ms = self.next_ms
            self.total_samples += len(pcm) // 2
            self.next_ms = self.total_samples * 1000 // OUTPUT_RATE
            chunk = AudioChunk(start_ms, self.next_ms, pcm)
            self._identities[id(pcm)] = chunk
            if len(self._identities) > 32:
                self._identities.popitem(last=False)
            self.frames.put_latest((self.timeline_id, chunk))
        except Exception:
            self.error = "PCM tap failed"
            self.active = False  # Sidecar failure must not escape into the microphone worker.

    def note_sent(self, pcm):
        identity = self._identities.pop(id(pcm), None)
        duration = len(pcm) * 1000 // (OUTPUT_RATE * 2)
        start = identity.start_ms if identity and identity.pcm is pcm else None
        self._sent.append((self.sent_ms, self.sent_ms + duration, start))
        self.sent_ms += duration

    def timestamp(self, language, elapsed_ms, session_id):
        stamp, source = None, "unavailable"
        if language == "ja" and session_id == self.timeline_id and type(elapsed_ms) in (int, float):
            for low, high, capture in reversed(self._sent):
                if (low < elapsed_ms <= high or elapsed_ms == low == 0) and capture is not None:
                    stamp, source = round(capture + elapsed_ms - low), "elapsed"
                    break
        return {
            "timeline_id": self.timeline_id,
            "audio_time_ms": stamp,
            "timing_source": source,
        }


def debug_logger(path=None):
    logger = logging.Logger("diarization", level=logging.DEBUG)
    path = path or settings_path().parent / "diarization-debug.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    logger.addHandler(handler)
    return logger


class DiarizationSidecar:
    def __init__(
        self,
        history,
        *,
        enabled=DIARIZATION_ENABLED,
        provider_factory=OpenAIDiarizer,
        window_sec=DIARIZATION_WINDOW_SEC,
        overlap_sec=DIARIZATION_OVERLAP_SEC,
        logger=None,
    ):
        self.history, self.enabled, self.provider_factory = history, enabled, provider_factory
        self.frames, self.jobs = LatestQueue(20), LatestQueue(1)
        self.ring = WindowRing(window_sec, overlap_sec)
        self.logger = logger
        self.tap = None
        self._providers = {}
        self._stop = threading.Event()
        self._threads = []
        self._status = {
            "state": "IDLE" if enabled else "DISABLED",
            "error": "",
            "last_window": None,
            "completed_window": None,
            "segments": 0,
            "requests": 0,
        }
        self._alignment_signature = None
        self._windows = OrderedDict()
        self._aligned = {}

    def attach(self, mic, key, timeline_id):
        if not self.enabled or self._stop.is_set():
            return
        try:
            if not self._threads:
                self.logger = self.logger or debug_logger()
                self._threads = [
                    threading.Thread(target=self._collect, name="diarization-ring", daemon=True),
                    threading.Thread(target=self._work, name="diarization-api", daemon=True),
                ]
                for thread in self._threads:
                    thread.start()
            self._providers.clear()
            self._providers[timeline_id] = self.provider_factory(key)
            self.tap = AudioTimelineTap(timeline_id, self.frames)
            self.history.set_audio_timeline(self.tap)
            mic.frames.sinks = (*mic.frames.sinks, self.tap)
            self._status.update(state="BUFFERING", error="", completed_window=None)
        except Exception as exc:
            self._status.update(state="ERROR", error=f"Attach: {type(exc).__name__}")

    def detach(self):
        if self.tap:
            self.tap.active = False
        if self.enabled:
            self._status["state"] = "IDLE"

    def note_sent(self, pcm):
        try:
            if self.tap:
                self.tap.note_sent(pcm)
        except Exception as exc:
            self._status["error"] = f"Timeline: {type(exc).__name__}"

    def _log(self, kind, **values):
        if self.logger:
            self.logger.debug(
                json.dumps({"time": time.time(), "kind": kind, **values}, ensure_ascii=False)
            )

    def _collect(self):
        while not self._stop.is_set():
            try:
                timeline_id, chunk = self.frames.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                for window in self.ring.feed(timeline_id, chunk.start_ms, chunk.pcm):
                    self.jobs.put_latest(window)
            except Exception as exc:
                self._status.update(state="ERROR", error=f"Ring: {type(exc).__name__}")

    def _realign(self):
        revision, boundaries = self.history.speaker_boundaries.snapshot()
        signature = (revision, self.history.revision)
        if signature == self._alignment_signature:
            return
        for boundary in boundaries:
            identity = (boundary.window_start_ms, boundary.observations)
            reconsider = self._aligned.get(boundary.id) != identity
            if not reconsider and set(boundary.positions) == {"en", "ja"}:
                continue
            # Revisit pending/moving alignments while new captions can still arrive.
            if (
                self.tap
                and boundary.timeline_id == self.tap.timeline_id
                and self.tap.next_ms - boundary.time_ms > 120_000
                and boundary.positions
            ):
                continue
            window = self._windows.get((boundary.timeline_id, boundary.window_start_ms))
            if window is None:
                continue
            # Receipt time only limits search to a generous recent window. It never
            # supplies an audio timestamp, score, or EN character position.
            records = self.history.timeline_records(
                boundary.timeline_id,
                round(window.created_at * 1000) - (window.end_ms - window.start_ms) - 30_000,
                round(window.created_at * 1000) + 60_000,
            )
            updated = self.history.speaker_boundaries.align(
                boundary.id, records, reconsider_en=reconsider
            )
            self._aligned[boundary.id] = identity
            if updated != boundary:
                for language, anchor in updated.positions.items():
                    self._log(
                        "alignment",
                        id=boundary.id,
                        language=language,
                        raw_time_ms=boundary.time_ms,
                        aligned_time_ms=anchor.time_ms,
                        confidence=anchor.confidence,
                        text_similarity=anchor.text_similarity,
                        time_distance_ms=abs(anchor.time_ms - boundary.time_ms)
                        if anchor.time_ms is not None
                        else None,
                        char_offset=anchor.char_offset,
                        timing=anchor.timing,
                    )
        self._alignment_signature = (
            self.history.speaker_boundaries.revision,
            self.history.revision,
        )

    def _work(self):
        while not self._stop.is_set():
            try:
                window = self.jobs.get(timeout=0.5)
            except queue.Empty:
                try:
                    self._realign()
                except Exception as exc:
                    self._status["error"] = f"Alignment: {type(exc).__name__}"
                continue
            if time.monotonic() - window.created_at > MAX_JOB_AGE_SEC:
                self.jobs.dropped += 1
                continue
            provider = self._providers.get(window.timeline_id)
            if provider is None:
                continue
            self._status.update(state="PROCESSING", last_window=[window.start_ms, window.end_ms])
            self._log(
                "window",
                timeline_id=window.timeline_id,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
            )
            try:
                self._status["requests"] += 1
                segments = provider.diarize(wav_bytes(window.pcm), window.start_ms)
                if self._stop.is_set():
                    return
                self._log(
                    "segments",
                    timeline_id=window.timeline_id,
                    segments=[asdict(s) for s in segments],
                )
                self._windows[(window.timeline_id, window.start_ms)] = replace(window, pcm=b"")
                while len(self._windows) > 6:
                    self._windows.popitem(last=False)
                for boundary in segment_boundaries(
                    segments, window.timeline_id, window.start_ms, window.end_ms
                ):
                    updated, old = self.history.speaker_boundaries.merge(boundary)
                    self._log(
                        "boundary",
                        id=updated.id,
                        raw_time_ms=updated.time_ms,
                        confidence=updated.confidence,
                        old_time_ms=old.time_ms if old else None,
                        new_time_ms=updated.time_ms,
                    )
                self._realign()
                self._status.update(
                    state="BUFFERING" if self.tap and self.tap.active else "IDLE",
                    error="",
                    completed_window=[window.start_ms, window.end_ms],
                    segments=len(segments),
                )
            except Exception as exc:
                # Retry only on the next fresh window, never a Realtime reconnect.
                error = str(exc) if isinstance(exc, DiarizationError) else type(exc).__name__
                self._status.update(state="ERROR", error=error)
                self._log("error", error=error)
                if isinstance(exc, DiarizationError) and not exc.retryable:
                    self._providers.pop(window.timeline_id, None)
            # Keep provider credentials for the active capture only.
            for timeline in list(self._providers):
                if self.tap and timeline != self.tap.timeline_id:
                    self._providers.pop(timeline, None)

    def snapshot(self):
        _, boundaries = self.history.speaker_boundaries.snapshot()
        completed = self._status["completed_window"]
        lag = max(0, self.tap.next_ms - completed[1]) if self.tap and completed else None
        return {
            "diarization_enabled": self.enabled,
            "diarization": dict(self._status),
            "diarization_lag_ms": lag,
            "speaker_boundaries": len(boundaries),
            "speaker_boundaries_aligned": sum(bool(b.positions) for b in boundaries),
            "last_boundary_confidence": boundaries[-1].confidence if boundaries else None,
            "diarization_frame_queue": self.frames.qsize(),
            "diarization_job_queue": self.jobs.qsize(),
            "diarization_dropped_frames": self.frames.dropped,
            "diarization_dropped_jobs": self.jobs.dropped,
            "diarization_tap_error": self.tap.error if self.tap else "",
        }

    def close(self):
        self.detach()
        self._stop.set()
        # HTTP worker is daemonized and timeout-bounded; never wait on it in Tk.
