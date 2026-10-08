from conftest import make_unit

from realtime_subtitles.subtitle_view import wrap_subtitle
from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory


def event(text, start=0, speaker="S1"):
    return {
        "message": "AddSegment",
        "segment": {"transcript": text, "speaker": speaker},
        "metadata": {"start_time": start, "end_time": start + 0.4},
    }


def test_partial_replace_final_hold_merge_and_identity():
    clock = [10.0]
    h = TranslationHistory(clock=lambda: clock[0])
    a = TranslationUnitAssembler(h.emit_unit, clock=h.clock)
    h.begin_session("one", {"input_source": "audio_file"})
    h.set_partial("The land")
    h.set_partial("The landscape is")
    assert h.subtitle_view().en_text == "The landscape is"
    s = h.record_segment(event("The landscape is"), "one")
    h.set_partial("")
    a.accept(s)
    v = h.subtitle_view()
    assert v.en_text == "The landscape is" and v.unit_id is None
    h.set_partial("changing")
    assert h.subtitle_view().en_text == "The landscape is changing"
    clock[0] += 0.4
    a.accept(h.record_segment(event("changing.", 0.4), "one"))
    h.set_partial("")
    v = h.subtitle_view()
    assert v.en_text == "The landscape is changing." and v.unit_id == 0
    h.update_translation(0, "completed", text="状況は変わっています。")
    assert h.subtitle_view().unit_id == 0
    assert [s.en_text for s in h.sources()] == ["The landscape is", "changing."]


def test_old_translation_cannot_replace_new_or_cross_session_clear():
    h = TranslationHistory()
    first = make_unit(h, event("Old question."), "one")
    second = make_unit(h, event("New answer.", 1, "S2"), "one")
    v = h.subtitle_view()
    assert v.speaker_changed and v.speaker == "S2"
    assert v.unit_id == second.unit_id and not v.ja_text
    h.update_translation(first.unit_id, "completed", text="古い質問。")
    assert not h.subtitle_view().ja_text
    h.update_translation(second.unit_id, "completed", text="新しい回答。")
    assert h.subtitle_view().unit_id == second.unit_id
    h.begin_session("two", {"input_source": "microphone"})
    assert not h.subtitle_view().en_text and not h.subtitle_view().ja_text
    third = make_unit(h, event("Second session."), "two")
    h.clear_display()
    h.update_translation(third.unit_id, "completed", text="別セッション。")
    assert not h.subtitle_view().ja_text and not h.subtitle_view().en_text
    assert len(h.sources()) == 3


def test_latest_projection_does_not_copy_or_iterate_full_history():
    h = TranslationHistory()
    for i in range(100):
        make_unit(h, event(f"Line {i}.", i), "s")

    class NoScan(list):
        def __iter__(self):
            raise AssertionError("full history scan")

    h._sources = NoScan(h._sources)
    h._segments = NoScan(h._segments)
    assert h.subtitle_view().en_text == "Line 99."
    assert h.statistics() == {"pending": 100}


def test_pixel_wrap_tail_english_words_and_japanese():
    def measure(text):
        return sum(2 if ord(c) > 127 else 1 for c in text)

    text = "one two three four five six seven eight"
    lines = wrap_subtitle(text, measure, 15, 2).splitlines()
    assert len(lines) <= 2 and lines[-1].endswith("eight")
    assert all(measure(line) <= 15 for line in lines)
    assert all(word in text.split() for line in lines for word in line.split())
    ja = wrap_subtitle("日本語の字幕です。" * 20, measure, 20, 2)
    assert len(ja.splitlines()) == 2
    assert all(measure(line) <= 20 and not line.startswith("。") for line in ja.splitlines())
    assert wrap_subtitle("longunbreakabletoken final", measure, 10, 2).endswith("final")
    assert wrap_subtitle(text, measure, 0, 2) == ""


def test_completed_stays_live_until_next_speech_then_late_ja_updates_history():
    h = TranslationHistory()
    first = make_unit(h, event("First sentence."), "s")
    view, changes, cursor, _ = h.subtitle_frame(0)
    assert view.unit_id == first.unit_id and view.history_latest_id == -1
    assert len(changes) == 1
    h.set_partial("Next", "S2")
    view, changes, cursor, _ = h.subtitle_frame(cursor)
    assert view.en_text == "Next" and view.unit_id is None
    assert view.history_latest_id == first.unit_id and view.speaker_changed
    assert not changes
    h.update_translation(first.unit_id, "completed", text="最初の文。")
    view, changes, cursor, _ = h.subtitle_frame(cursor)
    assert not view.ja_text and changes[0].ja_text == "最初の文。"
    h.set_partial("")
    assert not h.subtitle_view().en_text  # don't resurrect the archived sentence


def test_frame_clear_and_session_keep_old_translations_out_of_history():
    h = TranslationHistory()
    u = make_unit(h, event("Old."), "s")
    _, _, cursor, start = h.subtitle_frame(0)
    h.clear_display()
    h.update_translation(u.unit_id, "completed", text="古い。")
    view, changes, cursor, new_start = h.subtitle_frame(cursor)
    assert new_start > start and not changes and not view.en_text
    h.begin_session("new", {})
    make_unit(h, event("New."), "new")
    view, changes, _, _ = h.subtitle_frame(cursor)
    assert view.en_text == "New." and len(changes) == 1
