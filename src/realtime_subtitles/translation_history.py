"""Immutable raw ASR sources and separately assembled, validated translation units."""

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
class SourceSegment:
    segment_id: int
    en_text: str
    speaker: str | None
    start_ms: int | None
    end_ms: int | None
    session_id: str | None
    received_at: str
    received_monotonic_ms: int
    break_before: bool
    raw_event_json: str


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
    source_segment_ids: tuple[int, ...] = ()
    raw_source_segments: tuple[SourceSegment, ...] = ()
    assembler_hold_ms: int = 0
    assembled_monotonic_ms: int = 0
    assembly_reason: str = ""
    validation_status: str = "pending"
    validation_issues: tuple = ()
    candidates: tuple = ()
    retry_count: int = 0
    validation_latency_ms: float = 0
    end_to_end_ja_latency_ms: int | None = None
    latency_origin: str = "first_source_segment_received"
    queue_wait_ms: int | None = None

    @property
    def unit_id(self):
        return self.sequence_id


class TranslationHistory:
    def __init__(self, context_segments=TRANSLATION_CONTEXT_SEGMENTS, *, clock=time.monotonic):
        self._lock = threading.RLock()
        self._segments = []
        self._sources = []
        self.clock = clock
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
        self._journal.append(
            {"kind": "translation_unit", "translation_unit_id": segment.unit_id, **asdict(segment)}
        )
        self._revision += 1

    def set_partial(self, text, speaker=None):
        with self._lock:
            if (self._partial, self._partial_speaker) != (text, speaker):
                self._partial, self._partial_speaker = text, speaker
                self._revision += 1

    def record_segment(self, event, session_id):
        """Persist and display final EN immediately, before assembly/translation."""
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
            changed_session = bool(self._sources) and session_id != self._session
            boundary = changed_session or bool(
                self._last_speaker and known_speaker and self._last_speaker != known_speaker
            )
            segment = SourceSegment(
                segment_id=len(self._sources),
                en_text=text,
                speaker=speaker,
                start_ms=milliseconds(meta.get("start_time")),
                end_ms=milliseconds(meta.get("end_time")),
                session_id=session_id,
                received_at=datetime.now(UTC).isoformat(),
                received_monotonic_ms=round(self.clock() * 1000),
                raw_event_json=json.dumps(event, ensure_ascii=False),
                break_before=boundary,
            )
            self._sources.append(segment)
            self._session = session_id
            if changed_session:
                self._last_speaker = None
            if known_speaker:
                self._last_speaker = known_speaker
            self._journal.append({"kind": "raw_source_segment", **asdict(segment)})
            self._revision += 1
            return segment

    def emit_unit(self, sources, reason, now_ms):
        with self._lock:
            first, last = sources[0], sources[-1]
            unit = TranslationUnit(
                sequence_id=len(self._segments),
                en_text=(
                    first.en_text
                    if len(sources) == 1
                    else " ".join(s.en_text.strip() for s in sources)
                ),
                speaker=first.speaker,
                start_ms=first.start_ms,
                end_ms=last.end_ms,
                session_id=first.session_id,
                received_at=first.received_at,
                received_monotonic_ms=first.received_monotonic_ms,
                break_before=first.break_before,
                source_segment_ids=tuple(s.segment_id for s in sources),
                raw_source_segments=tuple(sources),
                assembler_hold_ms=max(0, now_ms - first.received_monotonic_ms),
                assembled_monotonic_ms=now_ms,
                assembly_reason=reason,
            )
            self._segments.append(unit)
            self._record(unit)
            return unit

    def sources(self):
        with self._lock:
            return list(self._sources)

    def english_display(self):
        with self._lock:
            return list(self._sources[self._display_start :])

    def previous_translations(self, sequence_id):
        with self._lock:
            unit = self._segments[sequence_id]
            return [
                (s.en_text, s.ja_text)
                for s in self._segments[max(0, sequence_id - 5) : sequence_id]
                if s.session_id == unit.session_id and s.translation_status == "completed"
            ]

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
        self, sequence_id, status, *, text=None, error="", latency_ms=None, usage=None, **metadata
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
                **metadata,
            )
            self._segments[sequence_id] = new
            self._record(new)

    def segments(self):
        with self._lock:
            return list(self._segments)

    def clear_display(self):
        with self._lock:
            self._display_start = len(self._sources)
            self._partial = ""
            self._revision += 1

    def display_snapshot(self):
        with self._lock:
            return (
                self._revision,
                self._display_start,
                [s for s in self._segments if s.source_segment_ids[0] >= self._display_start],
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
        included = {i for s in self.segments() for i in s.source_segment_ids}
        for source in self.sources():
            if source.segment_id not in included:
                parts.append(f"[source #{source.segment_id} / holding]\nEN: {source.en_text}\n")
        return "\n".join(parts)

    def save(self, path, *, overwrite=True):
        path = Path(path)
        with path.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as output:
            if path.suffix.lower() == ".txt":
                output.write(self.saved_text())
            else:
                for segment in self.segments():
                    output.write(json.dumps(asdict(segment), ensure_ascii=False) + "\n")
