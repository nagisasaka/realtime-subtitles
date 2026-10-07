import json

import pytest

from realtime_subtitles.translation_assembler import TranslationUnitAssembler, is_complete
from realtime_subtitles.translation_history import TranslationHistory


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


def harness():
    clock = Clock()
    h = TranslationHistory(clock=clock)
    a = TranslationUnitAssembler(h.emit_unit, clock=clock)

    def feed(text, start=0, end=1, speaker="S1", session="a"):
        event = {
            "message": "AddSegment",
            "segment": {"transcript": text, "speaker": speaker},
            "metadata": {"start_time": start, "end_time": end},
        }
        raw = h.record_segment(event, session)
        if raw:
            a.accept(raw)
        return raw

    return clock, h, a, feed


@pytest.mark.parametrize(
    "parts",
    [
        ["The landscape is", "changing."],
        ["Implement some sort of automated emails so you'll be able", "to track."],
    ],
)
def test_real_fragments_join_and_preserve_sources(parts):
    clock, h, a, feed = harness()
    first = feed(parts[0])
    assert not h.segments()
    assert h.english_display()[0].en_text == parts[0]
    assert h.autosave_updates(0)[0][0]["kind"] == "raw_source_segment"
    clock.value += 0.4
    second = feed(parts[1], 1, 1.4)
    (unit,) = h.segments()
    assert unit.en_text == " ".join(parts)
    assert unit.source_segment_ids == (0, 1)
    assert unit.assembler_hold_ms == 400 and unit.start_ms == 0 and unit.end_ms == 1400
    assert [json.loads(s.raw_event_json)["segment"]["transcript"] for s in (first, second)] == parts
    assert not a.pending


@pytest.mark.parametrize(
    "text",
    [
        "This is a big one.",
        "Yes.",
        "No.",
        "Right.",
        "Absolutely.",
        'He said "Yes."',
        "It costs 3.14.",
    ],
)
def test_complete_immediate(text):
    _, h, _, feed = harness()
    feed(text)
    assert h.segments()[0].assembler_hold_ms == 0


@pytest.mark.parametrize("text", ["Dr.", "e.g.", "Prof.", "The price is 3.14", "The U.S."])
def test_abbreviations_and_decimal_not_sentence(text):
    assert not is_complete(text)


def test_deadline_never_extends_and_timer_flushes():
    clock, h, a, feed = harness()
    feed("It")
    clock.value += 1
    feed("is", 1, 1.2)
    clock.value += 0.5
    a.tick()
    (unit,) = h.segments()
    assert unit.en_text == "It is" and unit.assembler_hold_ms == 1500
    assert unit.assembly_reason == "timeout"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"speaker": "S2"},
        {"session": "b"},
        {"speaker": "UU"},
        {"start": 1.601},
        {"start": 0.9},
    ],
)
def test_boundaries_prevent_join(kwargs):
    clock, h, a, feed = harness()
    feed("The landscape is")
    clock.value += 0.1
    feed("changing.", **({"start": 1, "end": 2} | kwargs))
    assert len(h.segments()) == 2
    assert h.segments()[0].source_segment_ids == (0,)
    assert h.segments()[1].source_segment_ids == (1,)


@pytest.mark.parametrize("reason", ["stop", "eos", "disconnect", "session_change"])
def test_flush_retains_pending(reason):
    _, h, a, feed = harness()
    feed("Incomplete English")
    a.flush(reason)
    a.flush(reason)
    assert len(h.segments()) == 1 and h.segments()[0].assembly_reason == reason


def test_safety_limits_and_unknown_speaker():
    _, h, a, feed = harness()
    feed("a" * 2001)
    assert not a.pending
    feed("long audio", 1, 32)
    assert not a.pending
    feed("unknown", 32, 33, speaker="UU")
    feed("continuation.", 33, 34, speaker="UU")
    assert len(h.segments()) == 4


def test_unknown_session_never_merges_even_with_matching_speaker():
    clock, h, _, feed = harness()
    feed("The landscape is", session=None)
    clock.value += 0.4
    feed("changing.", 1, 1.4, session=None)
    assert [u.source_segment_ids for u in h.segments()] == [(0,), (1,)]
