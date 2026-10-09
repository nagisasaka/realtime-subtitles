import gc
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import make_unit

from realtime_subtitles.live_client import LiveClient

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Tk test")


def event(text, start=0, speaker="S1"):
    return {
        "message": "AddSegment",
        "segment": {"transcript": text, "speaker": speaker},
        "metadata": {"start_time": start, "end_time": start + 1},
    }


def test_summary_dialog_is_manual_and_keeps_live_captions_responsive(app):
    from test_lecture_summary import FakeAssistant, source

    notes = app.lecture_notes
    fake = FakeAssistant()
    notes.factory = fake
    notes.key_provider = lambda: "test-key"
    source(app.client.history, "Reliability matters.")
    app.summary_button.invoke()
    app.pump()
    dialog = app.summary_window
    assert dialog.window.winfo_viewable()
    assert not fake.calls and not notes.active
    assert dialog.questions_button.instate(["disabled"])
    fake.gate.clear()
    dialog.regenerate_button.invoke()
    app.pump()
    assert dialog.regenerate_button.instate(["disabled"])
    app.client.history.set_partial("Still updating English", "UU")
    app.pump()
    assert "Still updating English" in app.live_text.cget("text").replace("\n", " ")
    assert app.speaker_var.get() == "S1"
    dialog.window.withdraw()
    app.show_summary()
    assert len(fake.calls) == 1  # opening again never duplicates requests
    fake.gate.set()
    for _ in range(40):
        app.pump(0.05)
        if not notes.active:
            break
    app.pump()
    assert not notes.error
    assert "信頼性" in dialog.texts[0].get("1.0", "end")
    assert "1区間" in dialog.scope.get()
    dialog.questions_button.invoke()
    for _ in range(40):
        app.pump(0.05)
        if not notes.active:
            break
    app.pump()
    assert "How would you" in dialog.texts[1].get("1.0", "end")
    assert dialog.notebook.index("current") == 1
    assert len(fake.calls) == 2


def test_summary_dialog_has_independent_monitor_dpi(app, monkeypatch):
    import realtime_subtitles.ui as ui

    app.show_summary()
    app.pump()
    dialog = app.summary_window
    original = ui.window_dpi
    old_main_size = app.en_font.cget("size")
    old_body = dialog.body_font
    new_dpi = 192 if dialog.scale < 2 else 96
    monkeypatch.setattr(ui, "window_dpi", lambda w: new_dpi if w is dialog.window else original(w))
    app.pump()
    assert dialog.scale == new_dpi / 96
    assert dialog.body_font is old_body
    assert dialog.body_font.cget("size") == -round(16 * new_dpi / 96)
    assert app.en_font.cget("size") == old_main_size
    assert dialog.window.winfo_width() >= round(460 * dialog.scale)
    assert not app.lecture_notes.active


@pytest.fixture
def app(tmp_path):
    import tkinter as tk

    from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

    # Reclaim previous Tk fixtures on the UI thread before starting new workers.
    gc.collect()
    enable_dpi_awareness()
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    value = SubtitleApp(
        root,
        client=LiveClient(),
        settings_file=tmp_path / "settings.json",
        device_loader=lambda: [],
    )

    def pump(seconds=0.15):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            root.update()
            time.sleep(0.005)

    value.pump = pump
    pump()
    yield value
    value.close()
    deadline = time.monotonic() + 5
    while value.autosave.active and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    try:
        root.destroy()
    except tk.TclError:
        pass
    assert not errors


def test_fixed_widgets_coalescing_partial_correction_long_lines(app):
    widgets = dict(app.caption_widgets)
    bounds = {key: (w.winfo_y(), w.winfo_height()) for key, w in widgets.items()}
    height = app.root.winfo_height()
    previous = app._render_count
    for i in range(1000):
        app.client.history.set_partial(f"Old incorrect version {i}")
    app.client.history.set_partial("I think the biggest problem is reliability in production")
    app.pump()
    assert app._render_count - previous <= 2
    assert (
        app.live_text.cget("text").replace("\n", " ")
        == "I think the biggest problem is reliability in production"
    )
    assert "incorrect" not in app.live_text.cget("text")
    app.client.history.set_partial("Very long live statement with many words. " * 100)
    u = make_unit(
        app.client.history, event("A long confirmed statement with many words. " * 100), "s"
    )
    app.client.history.update_translation(u.unit_id, "completed", text="長い日本語字幕。" * 100)
    app.pump()
    assert dict(app.caption_widgets) == widgets
    for key, w in widgets.items():
        assert len(w.cget("text").splitlines()) <= 2
        assert (w.winfo_y(), w.winfo_height()) == bounds[key]
        assert w.winfo_class() == "Label"  # no Text scroll buffer
    assert app.root.winfo_height() == height
    app.client.history.clear_display()
    app.pump()
    assert all(not w.cget("text") for w in widgets.values())
    assert app.client.history.sources()


def test_controls_fonts_dpi_drag_clickthrough_and_missing_file(app, tmp_path):
    from realtime_subtitles.settings import Settings

    assert app.settings_window.state() == "withdrawn"
    app.settings_button.invoke()
    app.pump()
    assert app.start_button.winfo_viewable()
    app.en_size.set(32)
    app.ja_size.set(29)
    app.live_en_size.set(36)
    app.live_ja_size.set(21)
    app.en_weight.set("bold")
    app.ja_weight.set("normal")
    app._font_changed()
    app.pump()
    assert app.en_font.cget("size") == -round(36 * app.scale)
    assert app.confirmed_font.cget("size") == -round(32 * app.scale)
    assert app.live_ja_font.cget("size") == -round(21 * app.scale)
    assert app.ja_font.cget("weight") == "normal"
    assert app.live_text.winfo_height() == app.en_font.metrics("linespace") * 2
    app.transparency.set(40)
    app._transparency_changed(40)
    assert abs(app.root.attributes("-alpha") - 0.6) < 0.01
    app.topmost.set(False)
    app._toggle_topmost()
    app.pump()
    assert not app.root.attributes("-topmost")
    x, y = app.root.winfo_x(), app.root.winfo_y()
    app._drag_start(SimpleNamespace(x_root=x + 10, y_root=y + 10))
    app._drag_move(SimpleNamespace(x_root=x + 25, y_root=y + 30))
    app.pump()
    assert abs(app.root.winfo_x() - (x + 15)) < 3
    app.transparency.set(0)
    app._transparency_changed(0)
    app.click_through.set(True)
    app._toggle_click_through()
    assert app.click_through.get() and app._hotkey
    # Deliver the native hotkey message through the real Windows WndProc.
    import ctypes

    user = ctypes.windll.user32
    user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    hwnd = user.GetAncestor(app.root.winfo_id(), 2)
    assert user.GetWindowLongW(hwnd, -20) & 0x80020 == 0x80020
    user.PostMessageW(hwnd, 0x312, 0x5342, 0)
    app.pump()
    assert not app.click_through.get()
    assert not app._hotkey
    app.source.set("audio_file")
    app.file_path.set(str(tmp_path / "missing.wav"))
    app._source_changed()
    app.pump()
    assert "見つかりません" in app.file_info.get()
    assert app.file_entry.winfo_viewable() and not app.microphone.winfo_viewable()
    app.monitor_enabled.set(True)
    assert app.monitor_check.winfo_viewable()
    app._save_settings()
    saved = Settings.load(app.settings_file)
    assert saved.input_source == "audio_file" and saved.english_size == 32
    assert saved.live_english_size == 36 and saved.live_japanese_size == 21
    assert saved.audio_monitor is True


def test_save_remains_nonmodal_and_late_ja_does_not_replace_current(app, tmp_path, monkeypatch):
    h = app.client.history
    first = make_unit(h, event("Earlier question."), "s")
    second = make_unit(h, event("Current answer.", 2, "S2"), "s")
    h.update_translation(second.unit_id, "completed", text="現在の回答。")
    app.pump()
    assert app.ja_text.cget("text") == "現在の回答。"
    release = threading.Event()
    original = h.save

    def slow_save(path, **kw):
        assert release.wait(5)
        original(path, **kw)

    monkeypatch.setattr(h, "save", slow_save)
    try:
        app.save_transcript()
        assert app.root.grab_current() is None
        target = tmp_path / "session.jsonl"
        app.save_path.set(str(target))
        app.save_confirm.invoke()
        assert app.saving
        h.update_translation(first.unit_id, "completed", text="過去の質問。")
        h.set_partial("New live words")
        app.pump()
        assert app.ja_text.cget("text") == ""
        assert "現在の回答。" in app.history_text.get("1.0", "end")
        assert "過去の質問。" in app.history_text.get("1.0", "end")
        assert app.live_text.cget("text") == "New live words"
        release.set()
        deadline = time.monotonic() + 3
        while app.saving and time.monotonic() < deadline:
            app.pump(0.05)
        assert not app.saving
        rows = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
        assert [r["ja_text"] for r in rows] == ["過去の質問。", "現在の回答。"]
        app.save_confirm.invoke()
        app.pump()
        assert "同名" in app.save_result.get()
    finally:
        release.set()


def test_reverse_history_ruby_late_ja_scroll_anchor_and_live_hold(app):
    h = app.client.history
    units = []
    for i in range(30):
        units.append(make_unit(h, event(f"Sentence number {i}.", i), "s"))
    app.pump()
    assert app.live_text.cget("text") == "Sentence number 29."
    text = app.history_text
    content = text.get("1.0", "end")
    assert content.index("number 28") < content.index("number 27")
    assert "number 29" not in content
    bounds = app.live_text.winfo_y(), app.live_text.winfo_height()
    h.update_translation(29, "completed", text="29番の文。")
    app.pump()
    assert app.ja_text.winfo_y() < app.live_text.winfo_y()
    assert app.ja_text.cget("text") == "29番の文。"
    h.set_partial("New speech", "S2")
    app.pump()
    assert not app.ja_text.cget("text")
    assert "29番の文。\nSentence number 29." in text.get("1.0", "end")
    text.yview("pair_15_start")
    app.pump()
    anchor = text.get("@0,0", "@0,0 lineend")
    # Longer late translation above the reader + a newly archived sentence.
    h.update_translation(25, "completed", text="遅れて届いた長い訳です。" * 12)
    h.set_partial("")
    make_unit(h, event("Another final.", 31, "S2"), "s")
    h.set_partial("Still live", "S2")
    app.pump()
    assert text.get("@0,0", "@0,0 lineend") == anchor
    assert (app.live_text.winfo_y(), app.live_text.winfo_height()) == bounds
    content = text.get("1.0", "end")
    assert content.count("Sentence number 25.") == 1
    assert content.index("長い訳") < content.index("Sentence number 25.")
    text.yview_moveto(0)
    app.pump()
    assert text.yview()[0] == 0
    h.clear_display()
    app.pump()
    assert text.get("1.0", "end").strip() == ""
    assert len(h.sources()) == 31


def test_history_unicode_replacement_does_not_damage_adjacent_pairs(app):
    h = app.client.history
    first = make_unit(h, event("First 🚀 sentence."), "s")
    make_unit(h, event("Second sentence.", 2), "s")
    h.set_partial("Third")
    app.pump()
    h.update_translation(first.unit_id, "completed", text="最初の🚀文。")
    app.pump()
    content = app.history_text.get("1.0", "end")
    assert content.count("First 🚀 sentence.") == 1
    assert content.count("Second sentence.") == 1
    assert "最初の🚀文。\nFirst 🚀 sentence." in content


def test_late_ja_preserves_reading_position_inside_its_own_english(app):
    h = app.client.history
    for i in range(30):
        make_unit(h, event(f"Reading sentence {i}.", i), "s")
    app.pump()
    text = app.history_text
    text.yview("pair_15_en")
    app.pump()
    before = text.get("@0,0", "@0,0 lineend")
    assert "Reading sentence 15." in before
    h.update_translation(15, "completed", text="あとから届いた非常に長い日本語訳です。" * 12)
    app.pump()
    assert text.get("@0,0", "@0,0 lineend") == before
    assert text.get("pair_15_en", "pair_15_en lineend") == "Reading sentence 15."


def test_final_pair_is_held_live_and_moves_as_a_whole_on_next_partial(app):
    h, assembler = app.client.history, app.client.assembler
    now = [time.monotonic()]
    assembler.clock = lambda: now[0]
    assembler.accept(h.record_segment(event("One complete sentence.", 0), "s"))
    app.pump()
    assert app.live_text.cget("text") == "One complete sentence."
    assert not app.history_text.get("1.0", "end").strip()
    now[0] += 3600
    app.pump()
    assert not h.segments() and app.live_text.cget("text") == "One complete sentence."
    h.set_partial("And another", "S1")
    app.pump()
    assert "One complete sentence. And another" == app.live_text.cget("text").replace("\n", " ")
    assembler.accept(h.record_segment(event("And another.", 2), "s"))
    h.set_partial("")
    app.pump()
    assert h.segments()[0].source_segment_ids == (0, 1)
    assert not app.history_text.get("1.0", "end").strip()
    h.update_translation(0, "completed", text="完結した文と、もう一文。")
    now[0] += 3600
    app.pump()
    assert app.ja_text.cget("text") == "完結した文と、もう一文。"
    assert not app.history_text.get("1.0", "end").strip()
    h.set_partial("Next pair", "S1")
    app.pump()
    assert app.live_text.cget("text") == "Next pair" and not app.ja_text.cget("text")
    assert "完結した文と、もう一文。\nOne complete sentence. And another." in app.history_text.get(
        "1.0", "end"
    )


def test_llm_revision_waits_for_archive_and_replaces_pairs_without_late_overwrite(app):
    h = app.client.history
    make_unit(h, event("The landscape is", 0), "s")
    last = make_unit(h, event("changing rapidly.", 1), "s")
    target = h.reconstructions.plan(last)
    complete_revision(h, target, "状況は急速に変化しています。")
    app.pump()
    assert app.live_text.cget("text") == "changing rapidly."
    assert not app._history_group_for_unit
    h.set_partial("Another live statement")
    app.pump()
    content = app.history_text.get("1.0", "end")
    assert "状況は急速に変化しています。\nThe landscape is changing rapidly." in content
    assert content.count("The landscape is") == 1 and content.count("changing rapidly.") == 1
    h.update_translation(0, "completed", text="以前の断片訳")
    app.pump()
    assert "以前の断片訳" not in app.history_text.get("1.0", "end")
    assert h.segments()[0].ja_text == "以前の断片訳"
    h.clear_display()
    app.pump()
    assert not app.history_text.get("1.0", "end").strip()


def test_revision_merge_preserves_reader_and_newer_response_wins(app):
    h = app.client.history
    units = []
    for i in range(30):
        units.append(make_unit(h, event(f"Sentence number {i}.", i), "s"))
    a = h.reconstructions.plan(units[15])
    b = h.reconstructions.plan(units[16])
    app.pump()
    text = app.history_text
    text.yview("pair_15_en")
    app.pump()
    y = app.live_text.winfo_y()
    complete_revision(h, b, "新しい再構成訳。")
    app.pump()
    assert "Sentence number 15." in text.get("@0,0", "@0,0 + 150 chars")
    assert app.live_text.winfo_y() == y
    complete_revision(h, a, "古い再構成訳。")
    app.pump()
    content = text.get("1.0", "end")
    assert "古い再構成訳。" not in content and "新しい再構成訳。" in content
    assert content.count("Sentence number 15.") == 1
    assert "Sentence number 15. Sentence number 16." in content
    assert content.count("Sentence number 14.") == 1


def test_semantic_paragraphs_can_split_inside_old_units_and_keep_reading_anchor(app):
    from realtime_subtitles.history_reconstruction import ReconstructedParagraph

    h = app.client.history
    units = []
    for i in range(25):
        units.append(make_unit(h, event(f"Sentence number {i}.", i), "s"))
    a = h.reconstructions.plan(units[15])
    b = h.reconstructions.plan(units[16])
    app.pump()
    text = app.history_text
    # Split within unit 15, which used to be indivisible in the history UI.
    cut = a.en_text.index("number 15")
    middle = a.en_text.index("Sentence number 14.")
    h.reconstructions.set_paragraphs(
        a.unit_id,
        (
            ReconstructedParagraph(0, middle - 1, "最初の部分。"),
            ReconstructedParagraph(middle, cut - 1, "途中の部分。"),
            ReconstructedParagraph(cut, len(a.en_text), "後ろの部分。"),
        ),
    )
    h.reconstructions.update_translation(a.unit_id, "completed", text="最初の部分。後ろの部分。")
    app.pump()
    assert "後ろの部分。\nnumber 15." in text.get("1.0", "end")
    run = app._history_english_runs[15][1][0]
    text.yview(run)
    app.pump()
    assert "number 15." in text.get("@0,0", "@0,0 lineend")
    b = h.reconstructions.prepare(b.unit_id)
    assert b.en_text == "Sentence number 14. Sentence number 15. Sentence number 16."
    complete_revision(h, b, "新しい後半。")
    app.pump()
    assert "number 15." in text.get("@0,0", "@0,0 + 150 chars")
    content = text.get("1.0", "end")
    assert content.count("Sentence number 14.") == 1
    assert content.count("number 15.") == 1
    assert "新しい後半。\nSentence number 14. Sentence number 15. Sentence number 16." in content
    assert "最初の部分。" in content  # Prefix JA was never sent or replaced.


def complete_revision(history, target, japanese):
    from realtime_subtitles.history_reconstruction import ReconstructedParagraph

    history.reconstructions.set_paragraphs(
        target.unit_id, (ReconstructedParagraph(0, len(target.en_text), japanese),)
    )
    history.reconstructions.update_translation(target.unit_id, "completed", text=japanese)


def test_surface_cleanup_renders_without_losing_raw_offsets_or_live_frame(app):
    from realtime_subtitles.history_english import JoinCleanup, apply_join_cleanup
    from realtime_subtitles.history_reconstruction import (
        BatchTranslation,
        EnglishSplit,
        pair_translations,
        split_english,
    )

    h = app.client.history
    units = []
    for i in range(30):
        value = "We need," if i == 14 else "More capacity." if i == 15 else f"Sentence {i}."
        units.append(make_unit(h, event(value, i), "s"))
    target = h.reconstructions.plan(units[15])
    h.set_partial("Live unchanged", "S1")
    app.pump()
    widget = app.history_text
    widget.yview("pair_15_en")
    app.pump()
    y, height = app.live_text.winfo_y(), app.root.winfo_height()
    chunks, rejected = apply_join_cleanup(
        target.en_text,
        split_english(target.en_text, EnglishSplit(end_tokens=[1, 5])),
        [JoinCleanup(start_token=4, remove_previous_punctuation=True, lowercase_initial=True)],
        (2, 4),
    )
    assert not rejected
    h.reconstructions.set_chunks(target.unit_id, chunks)
    h.reconstructions.set_paragraphs(
        target.unit_id,
        pair_translations(
            chunks,
            BatchTranslation(
                translations=[
                    {"id": 0, "ja": "以前の文。"},
                    {"id": 1, "ja": "もっと容量が必要です。"},
                ]
            ),
        ),
    )
    h.reconstructions.update_translation(target.unit_id, "completed", text="もっと容量が必要です。")
    app.pump()
    content = widget.get("1.0", "end")
    assert "もっと容量が必要です。\nWe need more capacity.\n" in content
    assert "We need," not in content and "More capacity." not in content
    assert h.segments()[14].en_text == "We need,"
    assert h.segments()[15].en_text == "More capacity."
    assert "capacity" in widget.get("@0,0", "@0,0 + 150 chars")
    for identity in (14, 15):
        for mark, offset, length in app._history_english_runs[identity]:
            assert widget.get(mark, f"{mark}+{length}c").lower() == (
                h.segments()[identity].en_text[offset : offset + length].lower()
            )
    assert app.live_text.cget("text") == "Live unchanged"
    assert app.history_text is widget
    assert (app.live_text.winfo_y(), app.root.winfo_height()) == (y, height)


@pytest.mark.parametrize("coalesced", [False, True])
def test_retranslated_paragraphs_always_render_newest_first(app, coalesced):
    from realtime_subtitles.history_reconstruction import ReconstructedParagraph

    h = app.client.history
    make_unit(h, event("Older baseline.", 0), "s")
    make_unit(h, event("First 🚀 thought. Middle thought.", 1), "s")
    second = make_unit(h, event("Newest thought.", 2), "s")
    a = h.reconstructions.plan(second)
    continuation = make_unit(h, event("Continuation 🚀.", 3), "s")
    h.set_partial("Live English", "S1")
    app.pump()
    widget = app.history_text
    live_bounds = app.live_text.winfo_y(), app.live_text.winfo_height()
    parts = []
    for en, ja in [
        ("Older baseline.", "古い文。"),
        ("First 🚀 thought.", "最初の考え。"),
        ("Middle thought.", "途中の考え。"),
        ("Newest thought.", "最新の考え。"),
    ]:
        start = a.en_text.index(en)
        parts.append(ReconstructedParagraph(start, start + len(en), ja))
    h.reconstructions.set_paragraphs(a.unit_id, tuple(parts))
    h.reconstructions.update_translation(a.unit_id, "completed", text="再翻訳結果")

    def assert_order():
        blocks = h.reconstructions.effective_blocks()
        expected = "".join(f"{b.ja_text or '翻訳待ち…'}\n{b.en_text}\n" for b in reversed(blocks))
        assert widget.get("1.0", "end-1c") == expected
        # Internal history and exported TXT remain chronological.
        assert app._history_starts == sorted(app._history_starts)
        saved = h.saved_text()
        positions = [saved.index(f"EN: {b.en_text}\n") for b in blocks]
        assert positions == sorted(positions)
        assert app.history_text is widget
        assert (app.live_text.winfo_y(), app.live_text.winfo_height()) == live_bounds

    if not coalesced:
        app.pump()
        assert_order()
    # Revisit the last two paragraphs; older paragraphs remain below them.
    b = h.reconstructions.plan(continuation)
    assert b.en_text == "Middle thought. Newest thought. Continuation 🚀."
    complete_revision(h, b, "最新の考えと続き。")
    app.pump()
    assert_order()
    h.update_translation(second.unit_id, "completed", text="古い単独訳")
    make_unit(h, event("Newest standalone.", 4), "s")
    h.set_partial("Still live", "S1")
    app.pump()
    assert_order()
    assert "古い単独訳" not in widget.get("1.0", "end")


def test_monitor_dpi_updates_pixel_fonts_and_layout_without_recreating_widgets(app, monkeypatch):
    dpi = [168]
    monkeypatch.setattr("realtime_subtitles.ui.window_dpi", lambda root: dpi[0])
    app.live_en_size.set(20)
    app.live_ja_size.set(14)
    app._font_changed()
    widgets = dict(app.caption_widgets)
    history = app.history_text
    app._refresh_dpi()
    app.pump()
    assert app.en_font.cget("size") == -35
    for value, en, ja in [(240, -50, -35), (168, -35, -24), (240, -50, -35)]:
        dpi[0] = value
        app.pump()
        assert app.scale == value / 96
        assert app.en_font.cget("size") == en
        assert app.live_ja_font.cget("size") == ja
        assert app.live_text.winfo_height() == app.en_font.metrics("linespace") * 2
        assert app.live_text.winfo_y() == app.live_ja_font.metrics("linespace") * 2 + round(
            4 * app.scale
        )
        assert app.live_text.winfo_x() == 0
        assert app.live_text.winfo_width() == app.captions.winfo_width()
        assert app.history_frame.winfo_width() == app.captions.winfo_width()
        assert app.live_en_size.get() == 20 and app.live_ja_size.get() == 14
        assert dict(app.caption_widgets) == widgets and app.history_text is history
    # Half-written settings must not be normalized/overwritten by a monitor move.
    app.live_en_size.set("")
    dpi[0] = 168
    app.pump()
    assert app.en_font.cget("size") == -35
    assert app.live_en_size._tk.globalgetvar(app.live_en_size._name) == ""


def native_monitor_origins():
    import ctypes
    from ctypes import wintypes

    rectangles = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def collect(handle, dc, rect, data):
        rectangles.append((rect.contents.left, rect.contents.top))
        return True

    callback = callback_type(collect)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    if len(rectangles) < 2:
        pytest.skip("Requires two native Windows displays")
    return rectangles


def test_native_monitor_roundtrip_keeps_logical_geometry(app):
    from realtime_subtitles.ui import window_dpi

    rectangles = native_monitor_origins()
    states = []
    app.live_en_size.set(20)
    app._font_changed()
    for left, top in [rectangles[0], rectangles[1], rectangles[0]]:
        app.root.geometry(f"+{left + 100}+{top + 100}")
        app.pump(0.4)
        dpi = window_dpi(app.root)
        assert app.scale == dpi / 96
        assert app.en_font.cget("size") == -round(20 * dpi / 96)
        assert app.live_text.winfo_width() == app.captions.winfo_width()
        states.append((dpi, app.root.winfo_width(), app.root.winfo_height()))
    for dpi, width, height in states[1:]:
        assert abs(width / dpi - states[0][1] / states[0][0]) < 0.02
        assert abs(height / dpi - states[0][2] / states[0][0]) < 0.02
    app._save_settings()
    assert app.settings.geometry_dpi == states[-1][0]


def test_settings_dpi_is_independent_and_padding_does_not_accumulate(app, monkeypatch):
    from tkinter import ttk

    settings_dpi = [240]
    monkeypatch.setattr(
        "realtime_subtitles.ui.window_dpi",
        lambda w: settings_dpi[0] if w is app.settings_window else 168,
    )
    monkeypatch.setattr("realtime_subtitles.ui.resize_client_for_dpi", lambda *a: None)
    app.show_settings()
    app.pump()
    main_font_size = app.en_font.cget("size")
    main_geometry = app.root.geometry()
    original_widgets = tuple(app._settings_layout)
    for dpi in (240, 168, 240, 168, 240):
        settings_dpi[0] = dpi
        app.pump()
        assert app.settings_scale == dpi / 96
        assert app.settings_font.cget("size") == -round(12 * dpi / 96)
        assert app.en_font.cget("size") == main_font_size
        assert app.root.geometry() == main_geometry
        assert tuple(app._settings_layout) == original_widgets
        assert str(app.microphone.cget("font")) == str(app.settings_font)
        assert str(ttk.Style(app.root).lookup("Settings.TButton", "font")) == str(app.settings_font)
        padding = app.mic_panel.cget("padding")
        assert tuple(app.root.winfo_pixels(v) for v in padding) == tuple(
            round(v * dpi / 96) for v in (12, 0, 12, 8)
        )
    app._settings_dropdown(app.microphone)
    popup = app.root.tk.call("ttk::combobox::PopdownWindow", str(app.microphone))
    assert str(app.root.tk.call(f"{popup}.f.l", "cget", "-font")) == str(app.settings_font)
    app.source.set("audio_file")
    app._source_changed()
    app.pump()
    assert app.file_info_label.pack_info()["padx"] == 30
    assert app.monitor_check.pack_info()["padx"] == 30
    app.settings_window.withdraw()
    app.pump()
    settings_dpi[0] = 168
    app.show_settings()
    app.pump()
    assert app.settings_font.cget("size") == -21
    assert app.file_info_label.pack_info()["padx"] == 21
    assert app.en_font.cget("size") == main_font_size


def test_native_settings_and_subtitles_on_separate_displays(app):
    from realtime_subtitles.ui import window_dpi

    first, second = native_monitor_origins()[:2]
    app.live_en_size.set(20)
    app._font_changed()
    for main, dialog in [(first, second), (second, first), (first, second)]:
        app.root.geometry(f"+{main[0] + 100}+{main[1] + 100}")
        app.show_settings()
        settings = app.settings_window
        settings.geometry(f"+{dialog[0] + 100}+{dialog[1] + 100}")
        app.pump(0.4)
        assert app.en_font.cget("size") == -round(20 * window_dpi(app.root) / 96)
        assert app.settings_font.cget("size") == -round(12 * window_dpi(settings) / 96)
        assert settings.winfo_width() == settings.winfo_reqwidth()
        assert settings.winfo_height() == settings.winfo_reqheight()
        for widget in app._settings_layout:
            if widget.winfo_viewable():
                x = widget.winfo_rootx() - settings.winfo_rootx()
                y = widget.winfo_rooty() - settings.winfo_rooty()
                assert 0 <= x <= settings.winfo_width() - widget.winfo_width()
                assert 0 <= y <= settings.winfo_height() - widget.winfo_height()


def test_tail_replacement_keeps_prefix_widget_anchor_and_live_frame(app):
    from realtime_subtitles.history_reconstruction import ReconstructedParagraph

    h = app.client.history
    units = [make_unit(h, event(f"Sentence {i}.", i), "s") for i in range(15)]
    units.append(make_unit(h, event("Keep this prefix. With our", 15), "s"))
    a = h.reconstructions.plan(units[-1])
    cut = a.en_text.index("With our")
    middle = a.en_text.index("Sentence 14.")
    h.reconstructions.set_paragraphs(
        a.unit_id,
        (
            ReconstructedParagraph(0, middle - 1, "変更しない前半。"),
            ReconstructedParagraph(middle, cut - 1, "直前の考え。"),
            ReconstructedParagraph(cut, len(a.en_text), "当社の"),
        ),
    )
    h.reconstructions.update_translation(a.unit_id, "completed", text="変更しない前半。当社の")
    h.set_partial("new platform", "S1")
    app.pump()
    widget, y = app.history_text, app.live_text.winfo_y()
    prefix_mark = "prefix_probe"
    widget.mark_set(prefix_mark, app._history_english_runs[13][0][0])
    before = widget.get(prefix_mark, f"{prefix_mark} lineend")
    u = make_unit(h, event("new platform 🚀.", 16), "s")
    h.set_partial("Next live partial", "S1")
    b = h.reconstructions.plan(u)
    assert b.en_text == "Sentence 14. Keep this prefix. With our new platform 🚀."
    cut = b.en_text.index("With our")
    h.reconstructions.set_paragraphs(
        b.unit_id,
        (
            ReconstructedParagraph(0, cut - 1, "直前の考え。"),
            ReconstructedParagraph(cut, len(b.en_text), "当社の新しい基盤。"),
        ),
    )
    h.reconstructions.update_translation(b.unit_id, "completed", text="当社の新しい基盤。")
    app.pump()
    text = widget.get("1.0", "end")
    assert text.count("変更しない前半。") == 1
    assert text.count("Keep this prefix.") == 1
    assert text.count("With our") == 1
    assert "当社の新しい基盤。\nWith our new platform 🚀." in text
    assert widget.get(prefix_mark, f"{prefix_mark} lineend") == before
    assert app.history_text is widget and app.live_text.winfo_y() == y
    assert app.live_text.cget("text") == "Next live partial"
    h.update_translation(u.unit_id, "completed", text="古い単独訳")
    app.pump()
    assert "古い単独訳" not in widget.get("1.0", "end")
    assert h.saved_text().count("With our") == 1


def test_latest_wrap_keeps_a_blank_line_above_history(app):
    h = app.client.history
    make_unit(h, event("Already in history."), "s")
    app._resize_for_dpi(round(650 * app.scale), round(580 * app.scale))
    app.pump()
    history_y = app.history_frame.winfo_y()
    for text, expected_lines in [("Short.", 1), ("A long current English sentence. " * 20, 2)]:
        unit = make_unit(h, event(text, 2), "s")
        h.update_translation(unit.unit_id, "completed", text="現在の日本語字幕です。" * 30)
        app.pump()
        assert len(app.live_text.cget("text").splitlines()) == expected_lines
        assert len(app.ja_text.cget("text").splitlines()) == 2
        assert app.history_frame.winfo_y() == history_y
        bottom = app.live_text.winfo_y() + app.live_text.winfo_height()
        assert app.history_frame.winfo_y() - bottom == app.en_font.metrics("linespace")


def test_latest_and_history_four_font_sizes_are_independent(app):
    from realtime_subtitles.settings import Settings

    controls = [
        (app.live_en_size, app.en_font, 36, "live_english_size"),
        (app.live_ja_size, app.live_ja_font, 22, "live_japanese_size"),
        (app.en_size, app.confirmed_font, 25, "english_size"),
        (app.ja_size, app.ja_font, 16, "japanese_size"),
    ]
    widgets = app.live_text, app.ja_text, app.history_text
    for variable, face, value, _ in controls:
        unchanged = [
            (other, other.cget("size")) for _, other, _, _ in controls if other is not face
        ]
        variable.set(value)
        app._font_changed()
        app.pump()
        assert face.cget("size") == -round(value * app.scale)
        assert all(other.cget("size") == size for other, size in unchanged)
    assert widgets == (app.live_text, app.ja_text, app.history_text)
    app._save_settings()
    saved = Settings.load(app.settings_file)
    assert all(getattr(saved, name) == value for _, _, value, name in controls)


def test_changed_split_only_highlights_and_fades_without_moving_reader(app):
    from realtime_subtitles.history_reconstruction import ReconstructedParagraph
    from realtime_subtitles.ui import BG, HISTORY_FLASH_COLOR

    now = [100.0]
    app._animation_clock = lambda: now[0]
    h = app.client.history
    for i in range(15):
        make_unit(h, event(f"Earlier sentence {i}.", i), "s")
    unchanged = make_unit(h, event("Keep this paragraph.", 16), "s")
    last = make_unit(h, event("First idea. Second idea.", 17), "s")
    target = h.reconstructions.plan(last)
    h.set_partial("Live words", "S1")
    app.pump()
    text = app.history_text
    text.yview("pair_8_en")
    app.pump()
    anchor = text.get("@0,0", "@0,0 lineend")
    bounds = app.live_text.winfo_y(), app.history_frame.winfo_y(), app.root.winfo_height()
    parts = []
    for en, ja in [
        (unchanged.en_text, "この段落を維持。"),
        ("First idea.", "一つ目。"),
        ("Second idea.", "二つ目。"),
    ]:
        offset = target.en_text.index(en)
        parts.append(ReconstructedParagraph(offset, offset + len(en), ja))
    h.reconstructions.set_paragraphs(target.unit_id, tuple(parts))
    h.reconstructions.update_translation(target.unit_id, "completed", text="再翻訳済み")
    app.pump()
    blocks = [b for b in app._history_blocks if b.revision_id == target.unit_id]
    assert len(app._history_flashes) == 2
    assert (blocks[0].start, blocks[0].end) not in app._history_flashes
    assert not any(
        t.startswith("revision_flash_") for t in text.tag_names(f"block_{blocks[0].key}_en")
    )
    for block in blocks[1:]:
        tag, _ = app._history_flashes[block.start, block.end]
        assert text.tag_cget(tag, "background") == HISTORY_FLASH_COLOR
        assert tag in text.tag_names(f"block_{block.key}_ja")
        assert tag in text.tag_names(f"block_{block.key}_en")
    flashes = dict(app._history_flashes)
    now[0] += 0.55
    app.pump()
    assert all(
        text.tag_cget(tag, "background") not in (BG, HISTORY_FLASH_COLOR)
        for tag, _ in flashes.values()
    )
    # Same EN ranges with a different JA/revision notification must not flash again.
    h.reconstructions.update_translation(target.unit_id, "completed", text="再通知")
    app.pump()
    assert app._history_flashes == flashes
    now[0] += 0.5
    app.pump()
    assert not app._history_flashes and app._flash_after_id is None
    assert not any(t.startswith("revision_flash_") for t in text.tag_names())
    assert text.get("@0,0", "@0,0 lineend") == anchor
    assert (app.live_text.winfo_y(), app.history_frame.winfo_y(), app.root.winfo_height()) == bounds


def test_clear_cancels_in_progress_history_fade(app):
    h = app.client.history
    make_unit(h, event("The landscape is"), "s")
    last = make_unit(h, event("changing.", 2), "s")
    target = h.reconstructions.plan(last)
    h.set_partial("Next words", "S1")
    complete_revision(h, target, "状況は変わっています。")
    app.pump()
    assert app._history_flashes and app._flash_after_id is not None
    h.clear_display()
    app.pump()
    assert not app._history_flashes and app._flash_after_id is None
    assert not any(t.startswith("revision_flash_") for t in app.history_text.tag_names())
