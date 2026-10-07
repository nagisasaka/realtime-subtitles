"""Speechmatics Realtime STT only: no Speechmatics translation configuration."""

import asyncio
import contextlib
import json
import logging
import math
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .audio import FRAME_BYTES, OUTPUT_RATE, LatestQueue

ENDPOINT = "wss://global.rt.speechmatics.com/v2/"
EVENTS = ("AddPartialTranscript", "AddTranscript")
SERVER_CODES = {
    "invalid_message",
    "invalid_model",
    "invalid_language",
    "invalid_config",
    "invalid_audio_type",
    "invalid_output_format",
    "not_authorised",
    "not_allowed",
    "job_error",
    "protocol_error",
    "quota_exceeded",
    "timelimit_exceeded",
    "idle_timeout",
    "session_timeout",
    "unknown_error",
    "duration_limit_exceeded",
    "speaker_id",
}


def exception_code(exc):
    """Inspect structured handshake status through SDK wrappers, never print exception text."""
    current = exc
    for _ in range(5):
        if current is None:
            break
        status = getattr(getattr(current, "response", None), "status_code", None)
        if type(status) is int:
            return f"HTTP {status}"
        current = current.__cause__ or current.__context__
    return type(exc).__name__


@dataclass(frozen=True)
class SubtitleSegment:
    backend: str
    language: str
    text: str
    is_final: bool
    start_ms: int | None = None
    end_ms: int | None = None
    speaker: str | None = None
    session_id: str | None = None
    received_monotonic_ms: int | None = None


def milliseconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return round(value * 1000)


def parse_event(event, session_id=None):
    """Retain supplied timestamps; receive time is never substituted for audio time."""
    kind = event.get("message")
    if kind not in EVENTS:
        return []
    final = kind == "AddTranscript"
    received = time.monotonic_ns() // 1_000_000

    def segment(text, row, language, speaker=None):
        return SubtitleSegment(
            "speechmatics",
            language,
            text,
            final,
            milliseconds(row.get("start_time")),
            milliseconds(row.get("end_time")),
            speaker if isinstance(speaker, str) else None,
            session_id,
            received,
        )

    metadata = event.get("metadata", {})
    text = metadata.get("transcript", "")
    if not isinstance(text, str) or not text:
        return []
    # The API formats metadata.transcript for direct concatenation. Match each
    # result into that exact text, retaining whitespace/punctuation and word speakers.
    result, cursor = [], 0
    for row in event.get("results", []):
        alternatives = row.get("alternatives") or []
        if not alternatives:
            continue
        alternative = alternatives[0]
        content = alternative.get("content", "")
        offset = text.find(content, cursor) if content else -1
        if offset < 0:
            # Do not invent formatting or discard authoritative words on schema variation.
            return [segment(text, metadata, "en")]
        end = offset + len(content)
        result.append(segment(text[cursor:end], row, "en", alternative.get("speaker")))
        cursor = end
    if not result:
        return [segment(text, metadata, "en")]
    if cursor < len(text):
        result[-1] = replace(result[-1], text=result[-1].text + text[cursor:])
    return result


class SegmentHistory:
    """Immutable finals plus a replaceable partial suffix, independently per language."""

    def __init__(self):
        self.lock = threading.RLock()
        self.finals = []
        self.partials = {"en": [], "ja": []}
        self.frontiers = {}
        self.seen = set()
        self.revision = 0
        self.session_id = None

    def begin_session(self, identity):
        with self.lock:
            self.session_id = identity
            self.partials = {"en": [], "ja": []}
            self.revision += 1

    def accept(self, event, session_id):
        kind = event.get("message")
        if kind not in EVENTS:
            return []
        language = "en"
        if language not in self.partials:
            return []
        segments = parse_event(event, session_id)
        final = kind == "AddTranscript"
        with self.lock:
            key = (session_id, language)
            frontier = self.frontiers.get(key, -1)
            if not final:
                self.partials[language] = [
                    s for s in segments if s.end_ms is None or s.end_ms > frontier
                ]
            else:
                identity = (session_id, kind, json.dumps(event, sort_keys=True, ensure_ascii=False))
                if identity in self.seen:
                    return []
                self.seen.add(identity)
                self.finals.extend(segments)
                ends = [s.end_ms for s in segments if s.end_ms is not None]
                if ends:
                    frontier = max(frontier, *ends)
                    self.frontiers[key] = frontier
                    # Remove the resolved region, preserving an already received future partial.
                    self.partials[language] = [
                        s
                        for s in self.partials[language]
                        if s.start_ms is not None
                        and s.start_ms >= frontier
                        and s.end_ms is not None
                        and s.end_ms > frontier
                    ]
                else:
                    self.partials[language] = []
            self.revision += 1
        return segments

    def snapshot(self):
        with self.lock:
            return self.revision, list(self.finals), {k: list(v) for k, v in self.partials.items()}

    def autosave_records(self, start=0):
        with self.lock:
            return (
                list(self.finals[start:]),
                {k: list(v) for k, v in self.partials.items()},
                self.session_id,
            )

    def save(self, path):
        _, finals, _ = self.snapshot()
        with Path(path).open("x", encoding="utf-8") as output:
            for segment in finals:
                output.write(json.dumps(asdict(segment), ensure_ascii=False) + "\n")


class SpeechmaticsClient:
    """One SDK/WebSocket on its own thread. Capture only enqueues immutable PCM."""

    def __init__(
        self, *, endpoint=ENDPOINT, on_event=None, on_session=None, max_delay=4.0, retry_base=5.0
    ):
        self.endpoint, self.on_event = endpoint, on_event
        self.on_session = on_session
        self.max_delay, self.retry_base = max_delay, retry_base
        self.history = SegmentHistory()
        self.frames = LatestQueue(5)
        self.state, self.error, self.warning = "STOPPED", "", ""
        self.sent_frames = self.acknowledged = 0
        self.event_counts = dict.fromkeys(EVENTS, 0)
        self.session_id = None
        self.stop_requested = threading.Event()
        self.thread = None
        self.loop = None
        self.task = None
        self.session_ready = False

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.active:
            return False
        key = os.environ.get("SPEECHMATICS_API_KEY", "").strip()
        if not key:
            self.state, self.error = "ERROR", "SPEECHMATICS_API_KEY が未設定です。"
            return False
        try:
            import speechmatics.rt  # noqa: F401
        except ImportError:
            self.state, self.error = "ERROR", "pip install -e . が必要です。"
            return False
        self.stop_requested.clear()
        self.error = self.warning = ""
        self.state = "CONNECTING"
        self.frames = LatestQueue(5)
        self.thread = threading.Thread(target=self._thread_main, args=(key,), daemon=False)
        self.thread.start()
        return True

    def stop(self):
        self.stop_requested.set()
        if self.active:
            self.state = "STOPPING"
            # Cancel a handshake/backoff immediately; a running session receives EOS instead.
            if self.loop and self.task and not self.session_ready:
                with contextlib.suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self.task.cancel)

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return not self.active

    def snapshot(self):
        return {
            "state": self.state,
            "error": self.error,
            "warning": self.warning,
            "session_id": self.session_id,
            "endpoint": self.endpoint,
            "model": "enhanced",
            "max_delay": self.max_delay,
            "queue": self.frames.qsize(),
            "dropped_frames": self.frames.dropped,
            "sent_frames": self.sent_frames,
            "acknowledged": self.acknowledged,
            "events": dict(self.event_counts),
        }

    def _thread_main(self, key):
        try:
            asyncio.run(self._run(key))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = type(exc).__name__  # No credentials or raw SDK exception strings.
            self.state = "ERROR"
        finally:
            self.loop = self.task = None
            self.session_ready = False
            self.history.begin_session(None)  # A disconnected partial is not a final result.
            if self.stop_requested.is_set():
                self.state = "STOPPED"

    async def _run(self, key):
        from speechmatics.rt import (
            AsyncClient,
            AudioEncoding,
            AudioFormat,
            TranscriptionConfig,
        )

        # SDK errors may include server-provided reasons. Surface safe types via our status.
        for name in (
            "speechmatics.rt",
            "speechmatics.rt.async_client",
            "speechmatics.rt.base_client",
            "speechmatics.rt.transport",
        ):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        for attempt in range(9):
            if self.stop_requested.is_set():
                return
            server_error, ended = [], []
            client = AsyncClient(api_key=key, url=self.endpoint)
            self.sent_frames = self.acknowledged = 0
            self.session_ready = False

            def receive(event, server_error=server_error, ended=ended):
                kind = event.get("message")
                if kind == "Error":
                    code = event.get("type")
                    server_error.append(code if code in SERVER_CODES else "unknown_error")
                elif kind == "Warning":
                    # Enumerated type only: no server echo of request credentials.
                    code = event.get("type")
                    self.warning = code if code in SERVER_CODES else "warning"
                elif kind == "EndOfTranscript":
                    ended.append(True)
                elif kind == "AudioAdded":
                    self.acknowledged = event.get("seq_no", self.acknowledged)
                elif kind in EVENTS:
                    self.event_counts[kind] += 1
                    try:
                        segments = self.history.accept(event, self.session_id)
                    except (ValueError, TypeError, AttributeError, KeyError):
                        self.warning = "Malformed subtitle event"
                        return
                    if self.on_event:
                        self.on_event(event, segments)

            for kind in (*EVENTS, "Error", "Warning", "AudioAdded", "EndOfTranscript"):
                client.on(kind, receive)
            try:
                async with asyncio.timeout(15):
                    await client.start_session(
                        transcription_config=TranscriptionConfig(
                            model="enhanced",
                            language="en",
                            enable_partials=True,
                            diarization="speaker",
                            max_delay=self.max_delay,
                        ),
                        audio_format=AudioFormat(
                            encoding=AudioEncoding.PCM_S16LE,
                            sample_rate=OUTPUT_RATE,
                            chunk_size=FRAME_BYTES,
                        ),
                    )
                self.session_id = client.session_id
                self.history.begin_session(self.session_id)
                if self.on_session:
                    self.on_session(self.session_id)
                self.session_ready = True
                self.state, self.error = "RUNNING", ""
                while not self.stop_requested.is_set():
                    if server_error or ended:
                        raise ConnectionError("Session ended")
                    try:
                        captured, frame = self.frames.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    if time.monotonic() - captured > 1:
                        self.frames.dropped += 1
                        continue
                    async with asyncio.timeout(3):
                        while self.sent_frames - self.acknowledged >= 10:
                            await asyncio.sleep(0.01)
                        await client.send_audio(frame)
                    self.sent_frames += 1
                # Bounded graceful shutdown returns pending English finals before closing.
                async with asyncio.timeout(8):
                    # SDK 1.2.1 overwrites its send counter when AudioAdded arrives.
                    # Supply our actual binary-message count via the public API.
                    await client.send_message(
                        {"message": "EndOfStream", "last_seq_no": self.sent_frames}
                    )
                    while not ended and not server_error:
                        await asyncio.sleep(0.01)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = server_error[-1] if server_error else exception_code(exc)
                self.error = code[:120]
                if self.stop_requested.is_set():
                    return
                if code in {
                    "AuthenticationError",
                    "ConfigurationError",
                    "not_authorised",
                    "invalid_model",
                    "invalid_config",
                    "invalid_audio_type",
                    "invalid_language",
                    "invalid_output_format",
                    "not_allowed",
                    "HTTP 401",
                    "HTTP 403",
                }:
                    self.state = "ERROR"
                    return
            finally:
                self.session_ready = False
                self.history.begin_session(None)
                if self.on_session:
                    self.on_session(None)
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(3):
                        await client.close()
            if attempt == 8:
                self.state = "ERROR"
                return
            self.state = "RECONNECTING"
            until = time.monotonic() + min(30, self.retry_base * 2**attempt)
            while not self.stop_requested.is_set() and time.monotonic() < until:
                await asyncio.sleep(0.05)
