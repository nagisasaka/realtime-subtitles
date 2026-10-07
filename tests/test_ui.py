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
        app.partial_text.cget("text").replace("\n", " ")
        == "I think the biggest problem is reliability in production"
    )
    assert "incorrect" not in app.partial_text.cget("text")
    app.client.history.set_partial("Very long live statement with many words. " * 100)
    u = make_unit(
        app.client.history, event("A long confirmed statement with many words. " * 100), "s"
    )
    app.client.history.update_translation(u.unit_id, "completed", text="長い日本語字幕。" * 100)
    app.pump()
    assert dict(app.caption_widgets) == widgets
    for key, w in widgets.items():
        assert len(w.cget("text").splitlines()) <= (1 if key == "confirmed" else 2)
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
    assert app.partial_text.winfo_height() == app.en_font.metrics("linespace") * 2
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
    app._save_settings()
    saved = Settings.load(app.settings_file)
    assert saved.input_source == "audio_file" and saved.english_size == 32


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
        assert app.ja_text.cget("text") == "現在の回答。"
        assert app.partial_text.cget("text") == "New live words"
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
