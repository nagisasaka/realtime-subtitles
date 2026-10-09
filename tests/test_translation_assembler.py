import json

import pytest

from realtime_subtitles.translation_assembler import TranslationUnitAssembler
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
def test_even_complete_sentences_wait_for_second_final(text):
    clock, h, a, feed = harness()
    feed(text)
    before = h.subtitle_view()
    clock.value += 600  # Time/silence never emits or moves the displayed English.
    assert not h.segments() and h.subtitle_view() == before
    assert a.snapshot()["waiting_for"] == "next_final"
    feed("Another sentence.", 100, 103)
    (unit,) = h.segments()
    assert unit.en_text == text + " Another sentence."
    assert unit.source_segment_ids == (0, 1)
    assert unit.assembly_reason == "final_pair"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"speaker": "S2"},
        {"session": "b"},
        {"speaker": "UU"},
    ],
)
def test_boundaries_prevent_join(kwargs):
    clock, h, a, feed = harness()
    feed("The landscape is")
    clock.value += 0.1
    feed("changing.", **({"start": 1, "end": 2} | kwargs))
    assert len(h.segments()) == 1
    assert h.segments()[0].source_segment_ids == (0,)
    assert [s.segment_id for s in a.pending] == [1]
    a.flush("eos")
    assert h.segments()[1].source_segment_ids == (1,)


@pytest.mark.parametrize("reason", ["stop", "eos", "disconnect", "session_change"])
def test_flush_retains_pending(reason):
    _, h, a, feed = harness()
    feed("Incomplete English")
    a.flush(reason)
    a.flush(reason)
    assert len(h.segments()) == 1 and h.segments()[0].assembly_reason == reason


@pytest.mark.parametrize("first", ["UU", "SU", None, ""])
@pytest.mark.parametrize("second", ["UU", "SU", None, ""])
def test_unknown_speaker_run_pairs_without_inventing_identity(first, second):
    _, h, a, feed = harness()
    feed("The landscape is", speaker=first)
    feed("changing.", 1, 2, speaker=second)
    (unit,) = h.segments()
    assert unit.source_segment_ids == (0, 1)
    assert unit.en_text == "The landscape is changing."
    assert unit.speaker == first and not unit.break_before
    assert [s.speaker for s in unit.raw_source_segments] == [first, second]
    assert not a.pending


@pytest.mark.parametrize("speaker", ["S1", "UU", None])
def test_unknown_session_does_not_merge(speaker):
    _, h, a, feed = harness()
    feed("Unknown.", speaker=speaker, session=None)
    feed("Continuation.", 1, 2, speaker=speaker, session=None)
    a.flush("eos")
    assert [u.source_segment_ids for u in h.segments()] == [(0,), (1,)]


def test_unknown_run_does_not_bridge_known_speakers_or_partials():
    _, h, a, feed = harness()
    feed("Known speaker.")
    feed("Unattributed first.", 1, 2, speaker="UU")
    feed("Unattributed second.", 2, 3, speaker=None)
    feed("Unattributed third.", 3, 4, speaker="UU")
    a.note_speaker("S2", "a")
    feed("Other speaker.", 4, 5, speaker="S2")
    feed("Continues.", 5, 6, speaker="S2")
    assert [u.source_segment_ids for u in h.segments()] == [(0,), (1, 2), (3,), (4, 5)]
    assert [u.speaker for u in h.segments()] == ["S1", "UU", "UU", "S2"]
    assert h.segments()[-1].break_before


def test_unknown_speaker_run_flushes_at_session_boundary():
    _, h, a, feed = harness()
    feed("Previous session.", speaker="UU")
    feed("New session.", 1, 2, speaker="UU", session="b")
    a.flush("eos")
    assert [u.source_segment_ids for u in h.segments()] == [(0,), (1,)]


def test_punctuation_and_long_audio_gap_do_not_split_pairs():
    _, h, a, feed = harness()
    for i, text in enumerate(["Yes.", "Dr.", "Price 3.14", "Done!", "Odd remaining."]):
        feed(text, i * 80, i * 80 + 60)
    assert [u.source_segment_ids for u in h.segments()] == [(0, 1), (2, 3)]
    assert [s.segment_id for s in a.pending] == [4]
    a.flush("eos")
    assert h.segments()[-1].source_segment_ids == (4,)


def test_speaker_partial_flushes_only_on_known_change_and_does_not_translate_partial():
    _, h, a, feed = harness()
    feed("Waiting.")
    a.note_speaker("S1", "a")
    a.note_speaker("UU", "a")
    assert not h.segments()
    a.note_speaker("S2", "a")
    assert len(h.segments()) == 1 and h.segments()[0].en_text == "Waiting."
    assert h.segments()[0].assembly_reason == "speaker_boundary"
    a.note_speaker("S2", "a")
    assert len(h.segments()) == 1


def test_context_counts_pairs_and_keeps_original_sources():
    _, h, a, feed = harness()
    for i in range(12):
        feed(f"Final {i}.", i, i + 1)
    assert len(h.segments()) == 6
    assert h.context_for(5) == [
        {"speaker": "S1", "text": f"Final {i}. Final {i + 1}."} for i in range(0, 10, 2)
    ]
    assert len(h.sources()) == 12 and not a.pending
