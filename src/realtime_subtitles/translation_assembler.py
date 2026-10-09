"""Pair consecutive Agent finals; only incoming events/lifecycle boundaries emit.

No punctuation heuristic, receive deadline, audio-gap limit, or periodic tick.
A lone final intentionally waits through silence. Raw sources remain immutable.
"""

import threading
import time

from .speaker_policy import UNKNOWN_SPEAKERS, same_speaker_group

FINALS_PER_UNIT = 2


class TranslationUnitAssembler:
    def __init__(self, emit, *, clock=time.monotonic):
        self.emit = emit
        self.clock = clock
        self.pending = []
        self._lock = threading.RLock()

    def _flush(self, reason):
        if self.pending:
            sources, self.pending = self.pending, []
            self.emit(sources, reason, round(self.clock() * 1000))

    def flush(self, reason="flush"):
        with self._lock:
            self._flush(reason)

    def note_speaker(self, speaker, session_id):
        """A new known speaker's partial ends the previous speaker's lone final."""
        with self._lock:
            if self.pending:
                previous = self.pending[-1]
                if session_id != previous.session_id:
                    self._flush("session_boundary")
                elif speaker not in UNKNOWN_SPEAKERS and speaker != previous.speaker:
                    self._flush("speaker_boundary")

    def accept(self, source):
        with self._lock:
            if self.pending:
                previous = self.pending[-1]
                same = (
                    source.session_id is not None
                    and source.session_id == previous.session_id
                    and same_speaker_group(source.speaker, previous.speaker)
                    and not source.break_before
                )
                if not same:
                    self._flush("speaker_or_session_boundary")
            self.pending.append(source)
            if len(self.pending) >= FINALS_PER_UNIT:
                self._flush("final_pair")

    def snapshot(self):
        with self._lock:
            return {
                "pending_source_ids": [s.segment_id for s in self.pending],
                "finals_per_unit": FINALS_PER_UNIT,
                "waiting_for": "next_final" if self.pending else None,
            }
