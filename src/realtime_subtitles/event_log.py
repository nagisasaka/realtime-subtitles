"""Opt-in receive-boundary diagnostics; never log audio payloads or credentials."""

import json
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


class EventJournal:
    def __init__(self, path):
        self.path = str(path)
        self.queue = queue.Queue(maxsize=4096)
        self.dropped = 0
        self.error = ""
        self._stop = threading.Event()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._file = destination.open("a", encoding="utf-8", newline="\n")
        self._thread = threading.Thread(target=self._write, name="event-journal", daemon=True)
        self._thread.start()

    def record(self, raw, *, session_id=None, model=None, stream="translation"):
        received_at = datetime.now(UTC).isoformat()
        received_monotonic_ns = time.monotonic_ns()
        event = json.loads(raw)
        kind = event.get("type", "unknown")
        entry = {
            "received_at": received_at,
            "received_monotonic_ns": received_monotonic_ns,
            "stream": stream,
            "session_id": session_id,
            "source_model": model,
            "type": kind,
            "event_id": event.get("event_id"),
            "elapsed_ms": event.get("elapsed_ms"),
        }
        # Preserve transcript JSON exactly as received, before routing or rendering.
        # Session secrets, headers, and audio bytes are deliberately outside this log.
        if "transcript" in kind:
            entry["raw_server_event"] = raw.decode() if isinstance(raw, bytes) else raw
        elif kind in {"session.created", "session.updated", "transcription_session.updated"}:
            session = event.get("session", {})
            entry["session_id"] = session.get("id", session_id)
            entry["effective_audio"] = session.get("audio")
        elif kind == "error":
            entry["error_code"] = event.get("error", {}).get("code")
            entry["request_event_id"] = event.get("error", {}).get("event_id")
        try:
            self.queue.put_nowait(entry)
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            while not self._stop.is_set() or not self.queue.empty():
                try:
                    entry = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
        except OSError as exc:
            self.error = f"Event log write failed: {type(exc).__name__}"
        finally:
            self._file.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            self.error = "Event log writer is still draining"
