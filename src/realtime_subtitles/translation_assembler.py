"""Bounded hold of incomplete Agent segments; no network, sleeps, or local VAD."""

import re
import threading
import time

MAX_HOLD_MS = 1500
MAX_AUDIO_GAP_MS = 600
MAX_UNIT_CHARS = 2000
MAX_UNIT_AUDIO_MS = 30_000
MAX_SOURCE_SEGMENTS = 20
UNKNOWN_SPEAKERS = {None, "", "UU", "SU"}
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "e.g", "i.e", "vs", "etc", "inc"}


def is_complete(text):
    value = text.strip().rstrip("\"'”’)]}")
    if value.endswith(("?", "!", "。", "？", "！")):
        return True
    if not value.endswith(".") or value.endswith("..."):
        return False
    last = value.split()[-1].lower().rstrip(".")
    if last in ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.)+[a-z]?", last):
        return False
    # A single initial is ambiguous; the deadline guarantees progress.
    return not re.fullmatch(r"[A-Z]\.", value.split()[-1])


class TranslationUnitAssembler:
    def __init__(self, emit, *, clock=time.monotonic, max_hold_ms=MAX_HOLD_MS):
        self.emit = emit
        self.clock = clock
        self.max_hold_ms = max_hold_ms
        self.pending = []
        self.deadline = None
        self._lock = threading.RLock()

    def _flush(self, reason):
        if self.pending:
            sources, self.pending = self.pending, []
            self.deadline = None
            self.emit(sources, reason, round(self.clock() * 1000))

    def flush(self, reason="flush"):
        with self._lock:
            self._flush(reason)

    def tick(self):
        with self._lock:
            if self.pending and self.clock() * 1000 >= self.deadline:
                self._flush("timeout")

    def accept(self, source):
        with self._lock:
            self.tick()
            if self.pending:
                first, previous = self.pending[0], self.pending[-1]
                adjacent = (
                    previous.end_ms is not None
                    and source.start_ms is not None
                    and 0 <= source.start_ms - previous.end_ms <= MAX_AUDIO_GAP_MS
                )
                same = (
                    source.session_id is not None
                    and source.session_id == previous.session_id
                    and source.speaker not in UNKNOWN_SPEAKERS
                    and source.speaker == previous.speaker
                )
                size = sum(len(s.en_text) for s in self.pending) + len(source.en_text)
                duration = (
                    source.end_ms - first.start_ms
                    if source.end_ms is not None and first.start_ms is not None
                    else None
                )
                within_limits = (
                    size + len(self.pending) <= MAX_UNIT_CHARS
                    and duration is not None
                    and duration <= MAX_UNIT_AUDIO_MS
                    and len(self.pending) < MAX_SOURCE_SEGMENTS
                )
                if not (same and adjacent and within_limits):
                    self._flush("boundary_or_limit")
            if not self.pending:
                self.deadline = source.received_monotonic_ms + self.max_hold_ms
            self.pending.append(source)
            if is_complete(source.en_text):
                self._flush("complete")
            elif len(source.en_text) >= MAX_UNIT_CHARS or (
                source.start_ms is not None
                and source.end_ms is not None
                and source.end_ms - source.start_ms >= MAX_UNIT_AUDIO_MS
            ):
                self._flush("limit")
            else:
                self.tick()

    def snapshot(self):
        with self._lock:
            return {
                "pending_source_ids": [s.segment_id for s in self.pending],
                "deadline_monotonic_ms": self.deadline,
            }
