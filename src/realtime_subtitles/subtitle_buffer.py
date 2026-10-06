"""Lossless delta history and a separate bounded display tail."""

import json
import re
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .speaker_timeline import BoundaryTimeline


def rolling_text(text, max_chars=240):
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    # Prefer a sentence boundary, but don't discard most of the current caption.
    boundary = re.search(r"[.!?。！？]\s*", tail[: max_chars // 2])
    return tail[boundary.end() :] if boundary else tail


class TranscriptHistory:
    def __init__(self):
        self._lock = threading.Lock()
        self._records = []
        self._tails = {"en": "", "ja": ""}
        self._revision = 0
        self._display_start = 0
        self.speaker_boundaries = BoundaryTimeline()
        self._audio_timeline = None
        self._char_counts = {"en": 0, "ja": 0}

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def set_audio_timeline(self, timeline):
        self._audio_timeline = timeline

    def append(
        self, language, delta, elapsed_ms=None, session_id=None, event_id=None, kind="delta"
    ):
        if language not in self._tails or not isinstance(delta, str):
            raise ValueError("Invalid transcript delta")
        with self._lock:
            record = {
                "sequence": len(self._records),
                "time": datetime.now(UTC).isoformat(),
                "session_id": session_id,
                "event_id": event_id,
                "elapsed_ms": elapsed_ms,
                "language": language,
                "delta": delta,
                "kind": kind,
                "received_at_ms": time.time_ns() // 1_000_000,
                "received_monotonic_ms": time.monotonic_ns() // 1_000_000,
                "char_start": self._char_counts[language],
                "char_end": self._char_counts[language] + len(delta),
            }
            self._char_counts[language] += len(delta)
            if self._audio_timeline:
                try:
                    record.update(self._audio_timeline.timestamp(language, elapsed_ms, session_id))
                except Exception:
                    record["timing_source"] = "unavailable"
            self._records.append(record)
            self._tails[language] = (self._tails[language] + delta)[-4000:]
            self._revision += 1

    def paragraph(self, language, elapsed_ms=None, session_id=None):
        with self._lock:
            tail = self._tails[language]
            needs_break = bool(tail) and not tail.endswith("\n")
        if needs_break:
            self.append(language, "\n", elapsed_ms, session_id, kind="paragraph")

    def clear_display(self):
        with self._lock:
            self._tails = {"en": "", "ja": ""}
            self._display_start = len(self._records)
            self._revision += 1

    def display(self):
        with self._lock:
            return self._revision, dict(self._tails)

    def display_records(self, start=0, limit=500):
        """Incremental GUI history; Clear hides earlier records without deleting them."""
        with self._lock:
            start = max(start, self._display_start)
            records = [dict(r) for r in self._records[start : start + limit]]
            return self._display_start, start + len(records), records

    def records(self, start=0):
        with self._lock:
            return [dict(record) for record in self._records[start:]]

    def full_text(self, language):
        with self._lock:
            return "".join(r["delta"] for r in self._records if r["language"] == language)

    def timeline_records(self, timeline_id, start_ms, end_ms):
        with self._lock:
            return [
                dict(r)
                for r in self._records
                if r.get("timeline_id") == timeline_id
                and start_ms <= r["received_monotonic_ms"] <= end_ms
            ]

    def rendered_records(self, boundaries=None):
        if boundaries is None:
            boundaries = self.speaker_boundaries.snapshot()[1]
        records = self.records()
        breaks = {}
        for boundary in boundaries:
            for language, anchor in boundary.positions.items():
                breaks.setdefault(anchor.sequence, []).append(
                    (anchor.offset, boundary.id, language)
                )
        for record in records:
            positions = sorted(
                set(
                    offset
                    for offset, _, lang in breaks.get(record["sequence"], [])
                    if lang == record["language"]
                )
            )
            rendered = record["delta"]
            for offset in reversed(positions):
                rendered = rendered[:offset] + "\n\n" + rendered[offset:]
            record["rendered_delta"] = rendered
            if positions:
                record["speaker_breaks"] = [
                    {"offset": offset, "boundary_id": identity}
                    for offset, identity, _ in breaks[record["sequence"]]
                ]
        return records

    def rendered_text(self, language):
        return "".join(
            r["rendered_delta"] for r in self.rendered_records() if r["language"] == language
        )

    def save(self, path, *, overwrite=True):
        boundary_snapshot = self.speaker_boundaries.snapshot()[1]
        records = self.rendered_records(boundary_snapshot)
        boundaries = [asdict(boundary) for boundary in boundary_snapshot]
        with open(path, "w" if overwrite else "x", encoding="utf-8", newline="\n") as file:
            if Path(path).suffix.lower() == ".txt":
                for language in ("en", "ja"):
                    file.write(language.upper() + ":\n")
                    file.write(
                        "".join(r["rendered_delta"] for r in records if r["language"] == language)
                    )
                    file.write("\n\n")
                return
            for record in records:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
            for boundary in boundaries:
                file.write(
                    json.dumps({"kind": "speaker_boundary", **boundary}, ensure_ascii=False) + "\n"
                )
