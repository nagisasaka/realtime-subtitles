import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Tk test")


def test_pixel_wrap_recent_lines():
    from realtime_subtitles.ui import wrap_subtitle

    wrapped = wrap_subtitle("one two three four five six seven", len, 10, 3)
    assert len(wrapped.splitlines()) <= 3
    assert all(len(line) <= 10 for line in wrapped.splitlines())
    assert wrapped.endswith("seven")
    japanese = wrap_subtitle("日本語字幕を表示します。" * 10, len, 12, 4)
    assert len(japanese.splitlines()) == 4
    assert all(len(line) <= 12 for line in japanese.splitlines())
    punctuation = wrap_subtitle("あいうえお、かきくけこ。", len, 5, 4)
    assert all(not line.startswith(("、", "。")) for line in punctuation.splitlines())
    assert all(len(line) <= 5 for line in punctuation.splitlines())


def test_native_tk_render_clear_settings_and_error(tmp_path, monkeypatch):
    import tkinter as tk

    from realtime_subtitles.audio import InputDevice
    from realtime_subtitles.settings import Settings
    from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    enable_dpi_awareness()
    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    app = SubtitleApp(
        root,
        settings_file=tmp_path / "settings.json",
        device_loader=lambda: [InputDevice(1, "Test microphone", "Windows WASAPI", 48000, 2, True)],
    )

    def pump(seconds=0.15):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)

    try:
        pump()
        assert app.microphone.current() == 0
        assert "Test microphone" in app.microphone.get()
        thread = threading.Thread(
            target=lambda: [
                app.client.history.append(
                    "en", "A practical realtime subtitle application. " * 20, 200
                ),
                app.client.history.append("ja", "英語音声を日本語字幕で確認できます。" * 20, 200),
            ]
        )
        thread.start()
        thread.join()
        pump()
        assert app.en_text.get("1.0", "end-1c")
        assert app.ja_text.get("1.0", "end-1c")
        assert app.en_text.get("1.0", "end-1c") == app.client.history.full_text("en")
        assert app.ja_text.get("1.0", "end-1c") == app.client.history.full_text("ja")
        assert app.settings_window.state() == "withdrawn"
        assert not app.start_button.winfo_viewable()
        assert app.settings_button.winfo_viewable()
        app.settings_button.invoke()
        pump()
        assert app.start_button.winfo_viewable()
        app.settings_window.withdraw()
        app._scroll_caption("en", "moveto", 0)
        pump()
        assert not app.follow_latest["en"]
        top = app.en_text.index("@0,0")
        app.client.history.append("en", " New speech." * 100)
        pump()
        assert app.en_text.index("@0,0") == top
        assert app.en_text.yview()[1] < 0.99
        app._scroll_caption("en", "moveto", 1)
        app.client.history.append("en", " Latest caption.")
        pump()
        assert app.follow_latest["en"] and app.en_text.yview()[1] > 0.999
        # Incoming wrapped lines must remain fully visible in both panes.
        for _ in range(3):
            app.client.history.append("en", " Continuing speech with wrapped lines." * 30)
            app.client.history.append("ja", "新しい字幕を最新の行まで表示します。" * 30)
            pump()
            for widget in app.caption_widgets.values():
                assert widget.yview()[1] > 0.999
        for language in ("en", "ja"):
            app._scroll_caption(language, "moveto", 0)
        pump()
        assert app.latest_button.winfo_viewable()
        app.latest_button.invoke()
        pump()
        assert all(app.follow_latest.values())
        assert not app.latest_button.winfo_viewable()
        # Start resumes following even if the previous session was scrolled back.
        app._scroll_caption("en", "moveto", 0)
        app._scroll_caption("ja", "moveto", 0)
        app.start()
        pump()
        assert all(app.follow_latest.values())
        assert all(widget.yview()[1] > 0.999 for widget in app.caption_widgets.values())
        assert abs(app.ja_font.cget("size")) > abs(app.en_font.cget("size"))
        app.show_diagnostics()
        app.en_size.set(28)
        app.ja_size.set(24)
        app.en_weight.set("bold")
        app.ja_weight.set("normal")
        app._font_changed()
        assert app.en_font.cget("weight") == "bold"
        assert app.ja_font.cget("weight") == "normal"
        assert app.en_size.get() == 28 and app.ja_size.get() == 24
        app.transparency.set(40)
        app._transparency_changed(40)
        assert abs(root.attributes("-alpha") - 0.6) < 0.01
        app.client.history.clear_display()
        pump()
        assert app.en_text.get("1.0", "end-1c") == app.ja_text.get("1.0", "end-1c") == ""
        assert app.client.history.full_text("en")
        app.start()
        pump()
        assert "OPENAI_API_KEY" in app.error_var.get()
        app._save_settings()
        assert Settings.load(tmp_path / "settings.json").english_size == 28
        assert Settings.load(tmp_path / "settings.json").transparency == 40
        assert Settings.load(tmp_path / "settings.json").english_weight == "bold"
        assert Settings.load(tmp_path / "settings.json").japanese_weight == "normal"
        assert not errors
    finally:
        app.close()
        pump()


def test_save_panel_keeps_captions_responsive_and_recovers_errors(tmp_path, monkeypatch):
    import json
    import tkinter as tk
    from tkinter import filedialog

    from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

    # The regression must never enter the Windows shell's modal file dialog.
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kw: pytest.fail("Native dialog"))
    enable_dpi_awareness()
    root = tk.Tk()
    app = SubtitleApp(root, settings_file=tmp_path / "settings.json", device_loader=lambda: [])
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    release = threading.Event()
    started = threading.Event()
    original_save = app.client.history.save

    def pump_until(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert predicate()

    def slow_save(path, **kwargs):
        started.set()
        assert release.wait(5)
        original_save(path, **kwargs)

    try:
        app.save_transcript()
        root.update()
        assert app.save_window.winfo_viewable()
        assert root.grab_current() is None
        app.client.history.append("en", "Still receiving")
        app.client.history.append("ja", "保存画面でも更新中")
        pump_until(lambda: app.ja_text.get("1.0", "end-1c") == "保存画面でも更新中")
        target = tmp_path / "字幕履歴" / "session.jsonl"
        app.save_path.set(str(target))
        monkeypatch.setattr(app.client.history, "save", slow_save)
        app.save_confirm.invoke()
        pump_until(started.is_set)
        assert app.saving
        app.client.history.append("en", " while saving.")
        pump_until(lambda: app.en_text.get("1.0", "end-1c").endswith("while saving."))
        release.set()
        pump_until(lambda: not app.saving)
        records = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        assert "".join(r["delta"] for r in records if r["language"] == "ja") == "保存画面でも更新中"
        assert "保存しました" in app.save_result.get()
        saved = target.read_bytes()
        app.save_confirm.invoke()
        pump_until(lambda: not app.saving)
        assert "同名" in app.save_result.get()
        assert target.read_bytes() == saved
        # Invalid destination errors remain in the panel; another save can succeed.
        app.save_path.set(str(target / "child.jsonl"))
        app.save_confirm.invoke()
        pump_until(lambda: not app.saving)
        assert "保存エラー" in app.save_result.get()
        second = tmp_path / "second.jsonl"
        app.save_path.set(str(second))
        app.save_confirm.invoke()
        pump_until(lambda: not app.saving)
        assert second.exists()
        app.save_window.withdraw()
        app.save_transcript()
        root.update()
        assert app.save_window.winfo_viewable()
        assert not errors
    finally:
        release.set()
        destroyed = []
        root.bind(
            "<Destroy>", lambda event: destroyed.append(True) if event.widget is root else None
        )
        app.close()
        pump_until(lambda: bool(destroyed))


def test_delayed_speaker_breaks_update_only_metadata_and_keep_scroll(tmp_path):
    import tkinter as tk
    from dataclasses import replace

    from realtime_subtitles.speaker_timeline import Anchor, SpeakerBoundary
    from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

    enable_dpi_awareness()
    root = tk.Tk()
    app = SubtitleApp(root, settings_file=tmp_path / "settings.json", device_loader=lambda: [])

    def pump():
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)

    try:
        en = "First speaker talks about deployment. Second speaker discusses reliability. "
        ja = "最初の話者です。次の話者の発言です。"
        app.client.history.append("en", en)
        app.client.history.append("ja", ja)
        for _ in range(30):
            app.client.history.append("en", "\nMore lines for reading past captions.")
            app.client.history.append("ja", "\n過去の字幕をスクロールで確認します。")
        pump()
        app._scroll_caption("en", "moveto", 0.5)
        app.en_text.mark_set("test_view", "@0,0")
        visible = app.en_text.get("@0,0", "@0,0 lineend")
        pos = en.index("Second")
        boundary = SpeakerBoundary(
            "change",
            10000,
            "A",
            "B",
            0.95,
            pos,
            positions={
                "en": Anchor(0, pos, pos, pos + 10, 0.95),
                "ja": Anchor(1, ja.index("次"), ja.index("次"), 20, 0.95),
            },
        )
        app.client.history.speaker_boundaries.merge(boundary)
        pump()
        assert app.en_text.get("1.0", "end-1c").startswith(en[:pos] + "\n\n" + en[pos:])
        assert app.ja_text.get("1.0", "end-1c").startswith(
            ja[: ja.index("次")] + "\n\n" + ja[ja.index("次") :]
        )
        assert app.en_text.get("speaker_break.last", "speaker_break.last+6c") == "Second"
        assert app.ja_text.get("speaker_break.last", "speaker_break.last+1c") == "次"
        assert app.en_text.get("@0,0", "@0,0 lineend") == visible
        assert app.ja_text.yview()[1] > 0.999
        assert app.client.history.records()[0]["delta"] == en
        # A later estimate moves a boundary without rewriting raw text or duplicating it.
        newer = replace(boundary, positions={"en": Anchor(0, en.index("reliability"), 0, 0, 0.98)})
        timeline = app.client.history.speaker_boundaries
        with timeline._lock:
            timeline._items[boundary.id] = newer
            timeline.revision += 1
        pump()
        assert app.en_text.get("speaker_break.last", "speaker_break.last+11c") == "reliability"
        assert len(app.en_text.tag_ranges("speaker_break")) == 2
        assert app.en_text.get("speaker_break.first", "speaker_break.last") == "\n\n"
        assert not app.ja_text.tag_ranges("speaker_break")
        assert app.client.history.full_text("en").startswith(en)
        app.client.history.clear_display()
        pump()
        assert app.en_text.get("1.0", "end-1c") == ""
    finally:
        app.close()
        pump()
