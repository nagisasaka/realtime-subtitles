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


@pytest.fixture
def app(tmp_path):
    import tkinter as tk

    from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

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
    app.en_weight.set("bold")
    app.ja_weight.set("normal")
    app._font_changed()
    app.pump()
    assert app.en_font.cget("size") == -round(32 * app.scale)
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
