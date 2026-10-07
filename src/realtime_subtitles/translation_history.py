"""Agent AddSegment is the only translation unit; raw words are separate metadata."""

import json
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .speechmatics_api import milliseconds

TRANSLATION_CONTEXT_SEGMENTS = 5
UNKNOWN_SPEAKERS = {None, "", "UU", "SU"}


@dataclass(frozen=True)
class TranslationUnit:
    sequence_id: int
    en_text: str
    speaker: str | None
    start_ms: int | None
    end_ms: int | None
    session_id: str | None
    received_at: str
    received_monotonic_ms: int
    ja_text: str | None = None
    translation_status: str = "pending"
    break_before: bool = False
    translation_error: str = ""
    translation_latency_ms: int | None = None
    usage: dict | None = None


class TranslationHistory:
    def __init__(self, context_segments=TRANSLATION_CONTEXT_SEGMENTS):
        self._lock = threading.RLock()
        self._segments = []
        self.word_metadata = []
        self._journal = []
        self._seen = set()
        self._revision = 0
        self._display_start = 0
        self._partial = ""
        self._partial_speaker = None
        self._session = None
        self._last_speaker = None
        self.context_segments = max(0, context_segments)

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def _record(self, segment):
        self._journal.append({"kind": "translation_unit", **asdict(segment)})
        self._revision += 1

    def set_partial(self, text, speaker=None):
        with self._lock:
            if (self._partial, self._partial_speaker) != (text, speaker):
                self._partial, self._partial_speaker = text, speaker
                self._revision += 1

    def add_segment(self, event, session_id):
        """Never split/merge finals, wait for punctuation, or translate a partial."""
        if event.get("message") != "AddSegment":
            return None
        meta = event.get("metadata") or {}
        payload = event.get("segment") or {}
        text = payload.get("transcript")
        if not isinstance(text, str) or not text.strip():
            return None
        identity = (session_id, meta.get("start_time"), meta.get("end_time"), text)
        speaker = payload.get("speaker")
        known_speaker = speaker if speaker not in UNKNOWN_SPEAKERS else None
        with self._lock:
            if identity in self._seen:
                return None
            self._seen.add(identity)
            changed_session = bool(self._segments) and session_id != self._session
            boundary = changed_session or bool(
                self._last_speaker and known_speaker and self._last_speaker != known_speaker
            )
            segment = TranslationUnit(
                sequence_id=len(self._segments),
                en_text=text,
                speaker=speaker,
                start_ms=milliseconds(meta.get("start_time")),
                end_ms=milliseconds(meta.get("end_time")),
                session_id=session_id,
                received_at=datetime.now(UTC).isoformat(),
                received_monotonic_ms=time.monotonic_ns() // 1_000_000,
                break_before=boundary,
            )
            self._segments.append(segment)
            self._session = session_id
            if changed_session:
                self._last_speaker = None
            if known_speaker:
                self._last_speaker = known_speaker
            self._record(segment)
            return segment

    def record_word_metadata(self, event, session_id):
        """Diagnostic data only. Never creates a unit or a translation request."""
        if event.get("message") != "AddTranscript":
            return
        record = {
            "kind": "raw_word_metadata",
            "session_id": session_id,
            "event": json.loads(json.dumps(event)),
        }
        with self._lock:
            self.word_metadata.append(record)
            self._journal.append(record)

    def context_for(self, sequence_id):
        with self._lock:
            if not self.context_segments:
                return []
            target = self._segments[sequence_id]
            return [
                {"speaker": s.speaker, "text": s.en_text}
                for s in self._segments[max(0, sequence_id - self.context_segments) : sequence_id]
                if s.session_id == target.session_id
            ]

    def update_translation(
        self, sequence_id, status, *, text=None, error="", latency_ms=None, usage=None
    ):
        with self._lock:
            old = self._segments[sequence_id]
            if old.translation_status == "completed":
                return  # Completed text is immutable, including after retries.
            new = replace(
                old,
                translation_status=status,
                ja_text=text,
                translation_error=error,
                translation_latency_ms=latency_ms,
                usage=usage,
            )
            self._segments[sequence_id] = new
            self._record(new)

    def segments(self):
        with self._lock:
            return list(self._segments)

    def clear_display(self):
        with self._lock:
            self._display_start = len(self._segments)
            self._partial = ""
            self._revision += 1

    def display_snapshot(self):
        with self._lock:
            return (
                self._revision,
                self._display_start,
                list(self._segments[self._display_start :]),
                self._partial,
                bool(
                    self._last_speaker
                    and self._partial_speaker not in UNKNOWN_SPEAKERS
                    and self._last_speaker != self._partial_speaker
                ),
            )

    def autosave_updates(self, cursor):
        with self._lock:
            return list(self._journal[cursor:]), {
                "kind": "partial_snapshot",
                "en": self._partial,
                "session_id": self._session,
            }

    def statistics(self):
        with self._lock:
            counts = {}
            for segment in self._segments:
                status = segment.translation_status
                counts[status] = counts.get(status, 0) + 1
            return counts

    def saved_text(self):
        parts = []
        for s in self.segments():
            if s.break_before:
                parts.append("\n---\n")
            parts.append(
                f"[#{s.sequence_id} {s.start_ms}–{s.end_ms} ms / {s.session_id}]\n"
                f"EN: {s.en_text}\nJA: {s.ja_text or '[' + s.translation_status + ']'}\n"
            )
        return "\n".join(parts)

    def save(self, path, *, overwrite=True):
        path = Path(path)
        with path.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as output:
            if path.suffix.lower() == ".txt":
                output.write(self.saved_text())
            else:
                for segment in self.segments():
                    output.write(json.dumps(asdict(segment), ensure_ascii=False) + "\n")
