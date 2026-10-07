"""Tk is only accessed from the main thread; background work uses a mailbox."""

import ctypes
import json
import queue
import re
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font, ttk

from .audio import list_microphones
from .autosave import TranscriptAutosave
from .live_client import LiveClient
from .realtime_api import State
from .settings import Settings
from .subtitle_buffer import rolling_text

BG = "#111318"


def configure_dark_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    surface, hover, pressed, text = "#242b35", "#354150", "#46566a", "#d9e0ea"
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground="#bec6d3")
    style.configure("TCheckbutton", background=BG, foreground=text)
    style.map("TCheckbutton", background=[("active", BG)], foreground=[("disabled", "#6b7787")])
    style.configure(
        "TButton",
        padding=(9, 5),
        background=surface,
        foreground=text,
        bordercolor=surface,
        lightcolor=surface,
        darkcolor=surface,
        focuscolor=hover,
    )
    style.map(
        "TButton",
        background=[("pressed", pressed), ("active", hover), ("disabled", "#1b212a")],
        foreground=[("disabled", "#647184")],
        bordercolor=[("active", hover)],
        lightcolor=[("active", hover)],
        darkcolor=[("active", hover)],
    )
    style.configure(
        "Vertical.TScrollbar",
        background=surface,
        troughcolor=BG,
        bordercolor=BG,
        lightcolor=surface,
        darkcolor=surface,
        arrowcolor="#9caabe",
        arrowsize=12,
        width=14,
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("pressed", pressed), ("active", hover)],
        arrowcolor=[("active", "#eef3fa")],
        lightcolor=[("active", hover)],
        darkcolor=[("active", hover)],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background="#66d5ae",
        troughcolor="#29323d",
        bordercolor=BG,
        lightcolor="#66d5ae",
        darkcolor="#66d5ae",
    )
    for name in ["TCombobox", "TSpinbox", "TEntry"]:
        style.configure(
            name,
            background=surface,
            fieldbackground=surface,
            foreground=text,
            bordercolor="#354150",
            lightcolor=surface,
            darkcolor=surface,
            arrowcolor=text,
            insertcolor=text,
        )
        style.map(
            name,
            fieldbackground=[("disabled", "#1b212a"), ("readonly", surface)],
            foreground=[("disabled", "#647184"), ("readonly", text)],
            background=[("active", hover)],
        )
    style.configure(
        "Horizontal.TScale",
        background=surface,
        troughcolor="#29323d",
        bordercolor=BG,
        lightcolor=surface,
        darkcolor=surface,
    )
    style.map("Horizontal.TScale", background=[("active", hover)])
    root.option_add("*TCombobox*Listbox.background", surface)
    root.option_add("*TCombobox*Listbox.foreground", text)
    root.option_add("*TCombobox*Listbox.selectBackground", pressed)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")


def dark_title_bar(window):
    """Use the native Windows 11 caption; keep standard drag/resize controls."""
    if sys.platform != "win32":
        return False
    try:
        if not window.winfo_exists():
            return False
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT: Tk's decorated wrapper.
        dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        success = True
        # COLORREF is 0x00BBGGRR. Explicit colors also work with the light OS theme.
        for attribute, color in [(20, 1), (34, 0x181311), (35, 0x181311), (36, 0xFFFFFF)]:
            value = ctypes.c_uint32(color)
            result = dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), 4)
            success = success and result == 0
        return success
    except (AttributeError, OSError, tk.TclError):
        return False


def style_window_frame(window):
    window.bind("<Map>", lambda e: dark_title_bar(window) if e.widget is window else None, add="+")
    window.after_idle(lambda: dark_title_bar(window))


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def wrap_subtitle(text, measure, width, max_lines=3):
    """Wrap by actual glyph widths, preferring spaces, then keep the latest lines."""
    if width <= 0:
        return ""
    # Bound layout work independently of the full, lossless transcript history.
    text = rolling_text(text, max_chars=max(80, int(width / max(1, measure("M"))) * 10))
    lines = []
    current = ""
    current_width = 0
    for char in text:
        if char == "\n":
            lines.append(current)
            current, current_width = "", 0
            continue
        char_width = measure(char)
        if current and current_width + char_width > width:
            split = current.rfind(" ")
            if split > len(current) // 3:
                lines.append(current[:split])
                current = current[split + 1 :]
                current_width = sum(measure(c) for c in current)
            elif char in "、。，．！？）」』】〉》" and len(current) > 1:
                # Keep Japanese closing punctuation away from the beginning of a line.
                lines.append(current[:-1])
                current = current[-1]
                current_width = measure(current)
            else:
                lines.append(current)
                current, current_width = "", 0
        current += char
        current_width += char_width
    if current:
        lines.append(current)
    return "\n".join(lines[-max_lines:])


class SubtitleApp:
    def __init__(self, root, *, client=None, settings_file=None, device_loader=list_microphones):
        self.root = root
        self.client = client if client is not None else LiveClient()
        self.is_live = hasattr(self.client.history, "autosave_updates")
        self.autosave = TranscriptAutosave(
            {"subtitles" if self.is_live else "openai": self.client.history}
        )
        self._live_render_revision = -1
        self._live_rendered = {"en": "", "ja": ""}
        self.settings_file = settings_file
        self.settings = Settings.load(settings_file)
        self.device_loader = device_loader
        self.mailbox = queue.Queue()
        self.devices = []
        self.refreshing = False
        self.closing = False
        self.saving = False
        self.save_window = None
        self.local_error = ""
        self._history_cursor = 0
        self._display_epoch = -1
        self._rendered_records = {}
        self._speaker_signature = None
        self._drag = None
        self._poll_id = None
        self._save_id = None
        self.diagnostic_window = None
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.title(
            "Realtime Subtitles · Speechmatics Agent STT + Luna"
            if self.is_live
            else "Realtime Subtitles · EN → 日本語"
        )
        root.configure(bg=BG)
        root.attributes("-alpha", 1 - self.settings.transparency / 100)
        root.attributes("-topmost", self.settings.always_on_top)
        style_window_frame(root)
        root.minsize(round(480 * self.scale), round(180 * self.scale))
        self._place_window()
        self._build_controls()
        self._build_captions()
        root.bind("<Configure>", self._configure)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _: self.client.stop())
        root.bind("<Button-3>", self.show_settings)
        root.bind("<Control-comma>", self.show_settings)
        self.refresh_devices()
        self._poll()

    def _place_window(self):
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(round(1080 * self.scale), screen_w), round(280 * self.scale)
        x, y = max(0, (screen_w - width) // 2), max(0, screen_h - height - 80)
        if self.settings.geometry:
            values = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", self.settings.geometry)
            if values:
                w, h, saved_x, saved_y = map(int, values.groups())
                left, top, total_w, total_h = 0, 0, screen_w, screen_h
                if sys.platform == "win32":
                    metrics = ctypes.windll.user32.GetSystemMetrics
                    left, top, total_w, total_h = (metrics(i) for i in (76, 77, 78, 79))
                width, height = min(w, total_w), min(h, total_h)
                x = max(left, min(saved_x, left + total_w - width))
                y = max(top, min(saved_y, top + total_h - height))
        # + followed by a negative coordinate is intentional: Tk absolute screen coordinates.
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_controls(self):
        configure_dark_style(self.root)
        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("字幕の設定")
        self.settings_window.configure(bg=BG)
        style_window_frame(self.settings_window)
        self.settings_window.withdraw()
        self.settings_window.transient(self.root)
        self.settings_window.protocol("WM_DELETE_WINDOW", self.settings_window.withdraw)
        self.settings_window.bind("<Escape>", lambda _: self.settings_window.withdraw())
        controls = ttk.Frame(self.settings_window, padding=(12, 9))
        controls.pack(fill="x")
        controls.columnconfigure(3, weight=1)
        self.start_button = ttk.Button(controls, text="Start", command=self.start)
        self.start_button.grid(row=0, column=0, padx=(0, 5))
        self.stop_button = ttk.Button(controls, text="Stop", command=self.client.stop)
        self.stop_button.grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, text="Microphone").grid(row=0, column=2, padx=(0, 6))
        self.microphone = ttk.Combobox(controls, state="readonly", width=34)
        self.microphone.grid(row=0, column=3, sticky="ew")
        self.microphone.bind("<<ComboboxSelected>>", lambda _: self._schedule_save())
        self.refresh_button = ttk.Button(controls, text="Refresh", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=4, padx=(5, 0))

        options = ttk.Frame(self.settings_window, padding=(12, 0, 12, 7))
        options.pack(fill="x")
        noise_label = ttk.Label(options, text="Noise")
        if not self.is_live:
            noise_label.pack(side="left")
        self.noise = ttk.Combobox(
            options, values=["far_field", "near_field"], state="readonly", width=10
        )
        self.noise.set(self.settings.noise_reduction)
        if not self.is_live:
            self.noise.pack(side="left", padx=(5, 14))
        self.noise.bind("<<ComboboxSelected>>", lambda _: self._schedule_save())
        self.topmost = tk.BooleanVar(value=self.settings.always_on_top)
        ttk.Checkbutton(
            options, text="Always on Top", variable=self.topmost, command=self._toggle_topmost
        ).pack(side="left")
        ttk.Button(options, text="Clear", command=self.client.history.clear_display).pack(
            side="right", padx=(5, 0)
        )
        ttk.Button(options, text="Save", command=self.save_transcript).pack(side="right")

        appearance = ttk.Frame(self.settings_window, padding=(12, 0, 12, 7))
        appearance.pack(fill="x")
        self.en_size = tk.IntVar(value=self.settings.english_size)
        self.ja_size = tk.IntVar(value=self.settings.japanese_size)
        self.en_weight = tk.StringVar(value=self.settings.english_weight)
        self.ja_weight = tk.StringVar(value=self.settings.japanese_weight)
        for label, var, weight, low, high in [
            ("英語 px", self.en_size, self.en_weight, 8, 64),
            ("日本語 px", self.ja_size, self.ja_weight, 8, 64),
        ]:
            ttk.Label(appearance, text=label).pack(side="left", padx=(0, 4))
            box = ttk.Spinbox(
                appearance,
                from_=low,
                to=high,
                width=4,
                textvariable=var,
                command=self._font_changed,
            )
            box.pack(side="left", padx=(0, 4))
            box.bind("<Return>", lambda _: self._font_changed())
            box.bind("<FocusOut>", lambda _: self._font_changed())
            weight_box = ttk.Combobox(
                appearance,
                textvariable=weight,
                values=["normal", "bold"],
                state="readonly",
                width=7,
            )
            weight_box.pack(side="left", padx=(0, 14))
            weight_box.bind("<<ComboboxSelected>>", lambda _: self._font_changed())
        ttk.Label(appearance, text="透明度").pack(side="left", padx=(0, 6))
        self.transparency = tk.DoubleVar(value=self.settings.transparency)
        self.transparency_text = tk.StringVar(value=f"{self.settings.transparency}%")
        ttk.Label(appearance, textvariable=self.transparency_text, width=5).pack(side="right")
        ttk.Scale(
            appearance,
            from_=0,
            to=70,
            variable=self.transparency,
            command=self._transparency_changed,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        details = ttk.Frame(self.settings_window, padding=12)
        details.pack(fill="x")
        ttk.Label(details, text="字幕上でホイール：過去ログ ／ End：最新へ戻る").pack(anchor="w")
        ttk.Label(
            details,
            text=(
                "Speechmaticsで話者交代を検出すると、英語・日本語の両方に空行を入れます。"
                if self.is_live
                else "話者交代を検出すると、字幕の対応位置に後から空行を追加します。"
            ),
            wraplength=650,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(details, text=f"自動保存先: {self.autosave.directory}", wraplength=500).pack(
            fill="x"
        )
        if self.is_live:
            ttk.Button(details, text="未翻訳を再試行", command=self.client.retry_translations).pack(
                anchor="e"
            )
        ttk.Button(details, text="Diagnostics", command=self.show_diagnostics).pack(anchor="e")
        status = ttk.Frame(self.root, padding=(12, 7, 12, 4))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="STOPPED")
        ttk.Label(status, textvariable=self.status_var).pack(side="left", padx=(0, 12))
        self.level = ttk.Progressbar(status, maximum=60, length=110)
        self.level.pack(side="left", padx=(4, 8))
        self.level_text = tk.StringVar(value="Mic: −120 dBFS")
        ttk.Label(status, textvariable=self.level_text).pack(side="left")
        self.settings_button = ttk.Button(status, text="設定", command=self.show_settings, width=5)
        self.settings_button.pack(side="right")
        self.latest_button = ttk.Button(status, text="↓ 最新", command=self._resume_follow, width=7)
        self.transcript_status = tk.StringVar()
        ttk.Label(details, textvariable=self.transcript_status).pack(anchor="w")
        for widget in [status, *status.winfo_children()]:
            if widget in (self.settings_button, self.latest_button):
                continue
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        self.error_var = tk.StringVar()
        self.error_label = tk.Label(
            self.settings_window,
            textvariable=self.error_var,
            bg=BG,
            fg="#ffb4a9",
            anchor="w",
            justify="left",
            font=("Segoe UI", 9),
        )
        self.error_label.pack(fill="x", padx=12, pady=(0, 4))

    def _build_captions(self):
        captions = tk.Frame(self.root, bg=BG)
        captions.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        captions.columnconfigure(0, weight=1)
        captions.rowconfigure(0, weight=2)
        captions.rowconfigure(1, weight=3)
        self.en_font = font.Font(
            family="Segoe UI",
            size=-round(self.en_size.get() * self.scale),
            weight=self.en_weight.get(),
        )
        self.ja_font = font.Font(
            family="Yu Gothic UI",
            size=-round(self.ja_size.get() * self.scale),
            weight=self.ja_weight.get(),
        )
        self.caption_widgets = {}
        self.follow_latest = {"en": True, "ja": True}
        for row, language, caption_font, foreground in [
            (0, "en", self.en_font, "#bbc3cf"),
            (1, "ja", self.ja_font, "#ffffff"),
        ]:
            pane = tk.Frame(captions, bg=BG)
            pane.grid(row=row, column=0, sticky="nsew", pady=(0, 8) if row == 0 else 0)
            text = tk.Text(
                pane,
                bg=BG,
                fg=foreground,
                font=caption_font,
                height=3,
                width=1,
                wrap="word",
                state="disabled",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=0,
                pady=0,
                cursor="arrow",
                selectbackground="#34445c",
                selectforeground="#ffffff",
                takefocus=True,
            )
            bar = ttk.Scrollbar(
                pane,
                orient="vertical",
                command=lambda *args, lang=language: self._scroll_caption(lang, *args),
            )
            text.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            self.caption_widgets[language] = text
            text.bind("<MouseWheel>", lambda event, lang=language: self._wheel(event, lang))
            for key, args in [
                ("Up", ("scroll", -1, "units")),
                ("Down", ("scroll", 1, "units")),
                ("Prior", ("scroll", -1, "pages")),
                ("Next", ("scroll", 1, "pages")),
                ("Home", ("moveto", 0)),
                ("End", ("moveto", 1)),
            ]:
                text.bind(
                    f"<{key}>",
                    lambda event, lang=language, values=args: self._scroll_caption(lang, *values),
                )
        self.en_text = self.caption_widgets["en"]
        self.ja_text = self.caption_widgets["ja"]

    def show_settings(self, event=None):
        self.settings_window.deiconify()
        self.settings_window.lift()
        return "break"

    def _scroll_caption(self, language, *args):
        widget = self.caption_widgets[language]
        widget.yview(*args)
        if args[0] == "moveto" and float(args[1]) >= 1:
            self.follow_latest[language] = True
        else:
            self.follow_latest[language] = widget.yview()[1] >= 0.999
        self._update_follow_button()
        return "break"

    def _resume_follow(self):
        for language, widget in self.caption_widgets.items():
            self.follow_latest[language] = True
            widget.yview_moveto(1)
        self._update_follow_button()

    def _update_follow_button(self):
        if all(self.follow_latest.values()):
            self.latest_button.pack_forget()
        else:
            self.latest_button.pack(side="right", padx=(0, 8))

    def _wheel(self, event, language):
        if event.delta:
            direction = -1 if event.delta > 0 else 1
            self._scroll_caption(
                language, "scroll", direction * max(1, abs(event.delta) // 120) * 3, "units"
            )
        return "break"

    def _render_history(self):
        if self.is_live:
            self._render_live_history()
            return
        epoch, cursor, records = self.client.history.display_records(self._history_cursor)
        if epoch != self._display_epoch:
            for language, widget in self.caption_widgets.items():
                widget.configure(state="normal")
                widget.delete("1.0", "end")
                for mark in widget.mark_names():
                    if mark.startswith("raw_delta_"):
                        widget.mark_unset(mark)
                widget.configure(state="disabled")
                self.follow_latest[language] = True
            self._display_epoch = epoch
            self._rendered_records.clear()
            self._speaker_signature = None
            self._update_follow_button()
        for language, widget in self.caption_widgets.items():
            added = "".join(r["delta"] for r in records if r["language"] == language)
            if not added:
                if self.follow_latest[language]:
                    widget.see("end-1c")
                continue
            top = widget.index("@0,0")
            widget.configure(state="normal")
            for record in records:
                if record["language"] != language:
                    continue
                mark = f"raw_delta_{record['sequence']}"
                widget.mark_set(mark, "end-1c")
                widget.mark_gravity(mark, "left")
                widget.insert("end", record["delta"], ())
                self._rendered_records[record["sequence"]] = record
            widget.configure(state="disabled")
            if self.follow_latest[language]:
                widget.see("end-1c")
            else:
                widget.yview(top)
        self._history_cursor = cursor
        self._render_speaker_breaks()

    def _render_live_history(self):
        revision, epoch, segments, partial, partial_break = self.client.history.display_snapshot()
        if revision == self._live_render_revision:
            for lang, widget in self.caption_widgets.items():
                if self.follow_latest[lang]:
                    widget.see("end-1c")
            return
        if epoch != self._display_epoch:
            self.follow_latest = {"en": True, "ja": True}
            self._display_epoch = epoch
            self._update_follow_button()
        for lang, widget in self.caption_widgets.items():
            parts = []
            items = self.client.history.english_display() if lang == "en" else segments
            for segment in items:
                if parts:
                    parts.append("\n\n" if segment.break_before else " ")
                if lang == "en":
                    parts.append(segment.en_text)
                elif segment.ja_text is not None:
                    parts.append(segment.ja_text)
                else:
                    pending = segment.translation_status in {"pending", "translating", "retrying"}
                    parts.append(
                        "［翻訳待ち…］"
                        if pending
                        else "［翻訳検証エラー］"
                        if segment.translation_status == "validation_failed"
                        else "［未翻訳］"
                    )
            base = "".join(parts)
            suffix = (
                ("\n\n" if base and partial_break else " " if base else "") + partial
                if lang == "en" and partial
                else ""
            )
            new = base + suffix
            old = self._live_rendered[lang]
            prefix = 0
            for a, b in zip(old, new, strict=False):
                if a != b:
                    break
                prefix += 1
            tail = 0
            while tail < min(len(old), len(new)) - prefix and old[-tail - 1] == new[-tail - 1]:
                tail += 1

            def index(text, count, widget=widget):
                return f"1.0+{widget.tk.call('string', 'length', text[:count])}c"

            widget.mark_set("live_view", "@0,0")
            widget.mark_gravity("live_view", "right")
            widget.configure(state="normal")
            if old != new:
                widget.delete(index(old, prefix), index(old, len(old) - tail))
                widget.insert(index(new, prefix), new[prefix : len(new) - tail])
            widget.tag_configure("partial", foreground="#858d99")
            widget.tag_remove("partial", "1.0", "end")
            if suffix:
                widget.tag_add("partial", index(new, len(base)), "end-1c")
            widget.configure(state="disabled")
            if self.follow_latest[lang]:
                widget.see("end-1c")
            else:
                widget.yview("live_view")
            self._live_rendered[lang] = new
        self._live_render_revision = revision

    def _render_speaker_breaks(self):
        revision, boundaries = self.client.history.speaker_boundaries.snapshot()
        positions = {lang: set() for lang in self.caption_widgets}
        for boundary in boundaries:
            for language, anchor in boundary.positions.items():
                if anchor.sequence in self._rendered_records:
                    positions[language].add((anchor.sequence, anchor.offset))
        signature = (
            revision,
            tuple((lang, tuple(sorted(items))) for lang, items in positions.items()),
        )
        if signature == self._speaker_signature:
            return
        for language, widget in self.caption_widgets.items():
            ranges = widget.tag_ranges("speaker_break")
            if not ranges and not positions[language]:
                continue
            widget.mark_set("speaker_view", "@0,0")
            widget.mark_gravity("speaker_view", "right")
            widget.configure(state="normal")
            for start, end in reversed(list(zip(ranges[::2], ranges[1::2], strict=True))):
                widget.delete(start, end)
            # Only synthetic markers are edited. Raw delta marks/text stay intact.
            for sequence, offset in sorted(positions[language], reverse=True):
                prefix = self._rendered_records[sequence]["delta"][:offset]
                count = widget.tk.call("string", "length", prefix)
                index = f"raw_delta_{sequence}+{count}c"
                if widget.compare(index, ">", "1.0"):
                    widget.insert(index, "\n\n", ("speaker_break",))
            widget.configure(state="disabled")
            if self.follow_latest[language]:
                widget.see("end-1c")
            else:
                widget.yview("speaker_view")
        self._speaker_signature = signature

    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag:
            x, y = event.x_root - self._drag[0], event.y_root - self._drag[1]
            self.root.geometry(f"+{x}+{y}")

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self._schedule_save()

    def _font_changed(self):
        def read_size(variable, current_font):
            try:
                return max(8, min(64, variable.get()))
            except tk.TclError:
                return round(abs(current_font.cget("size")) / self.scale)

        en = read_size(self.en_size, self.en_font)
        ja = read_size(self.ja_size, self.ja_font)
        self.en_size.set(en)
        self.ja_size.set(ja)
        self.en_font.configure(size=-round(en * self.scale), weight=self.en_weight.get())
        self.ja_font.configure(size=-round(ja * self.scale), weight=self.ja_weight.get())
        self._schedule_save()

    def _transparency_changed(self, value):
        transparency = max(0, min(70, round(float(value))))
        self.transparency_text.set(f"{transparency}%")
        self.root.attributes("-alpha", 1 - transparency / 100)
        self._schedule_save()

    def _configure(self, event):
        if event.widget is self.root:
            self._schedule_save()

    def _schedule_save(self):
        if self.closing:
            return
        if self._save_id:
            self.root.after_cancel(self._save_id)
        self._save_id = self.root.after(500, self._save_settings)

    def _save_settings(self):
        self._save_id = None
        index = self.microphone.current()
        if index >= 0:
            self.settings.microphone = self.devices[index - 1].key if index > 0 else ""
        # Serialize signed absolute coordinates independently of Tk's geometry formatting.
        self.settings.geometry = (
            f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            f"{self.root.winfo_x():+d}{self.root.winfo_y():+d}"
        )
        try:
            self.settings.english_size = self.en_size.get()
            self.settings.japanese_size = self.ja_size.get()
        except tk.TclError:
            pass
        self.settings.always_on_top = self.topmost.get()
        self.settings.english_weight = self.en_weight.get()
        self.settings.japanese_weight = self.ja_weight.get()
        self.settings.noise_reduction = self.noise.get()
        self.settings.transparency = max(0, min(70, round(self.transparency.get())))
        try:
            self.settings.save(self.settings_file)
        except OSError as exc:
            self.local_error = f"設定を保存できません: {exc}"

    def refresh_devices(self):
        if self.refreshing or self.client.active:
            return
        self.refreshing = True

        def work():
            try:
                self.mailbox.put(("devices", self.device_loader()))
            except Exception as exc:
                self.mailbox.put(("device_error", str(exc)))

        threading.Thread(target=work, name="device-discovery", daemon=True).start()

    def start(self):
        if self.refreshing:
            return
        self._resume_follow()
        self.local_error = ""
        index = self.microphone.current()
        device = self.devices[index - 1].index if index > 0 else None
        self._save_settings()
        self.client.start(device, self.noise.get())

    def save_transcript(self):
        if self.closing:
            return
        if self.save_window and self.save_window.winfo_exists():
            self.save_window.deiconify()
            self.save_window.lift()
            return
        # Native Windows file dialogs can hang inside the shell. This nonmodal
        # Tk panel keeps the normal event loop (and caption updates) running.
        window = self.save_window = tk.Toplevel(self.root)
        window.title("字幕履歴を保存")
        window.configure(bg=BG)
        window.transient(self.root)
        style_window_frame(window)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda _: window.withdraw())
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="保存先（UTF-8 .jsonl または .txt・話者改行を反映）").pack(anchor="w")
        destination = Path.home() / "RealtimeSubtitles" / "Transcripts"
        filename = datetime.now().strftime("subtitles-%Y%m%d-%H%M%S-%f.jsonl")
        self.save_path = tk.StringVar(value=str(destination / filename))
        entry = ttk.Entry(panel, textvariable=self.save_path, width=80)
        entry.pack(fill="x", pady=(8, 10))
        self.save_result = tk.StringVar(
            value="保存中も字幕は更新されます。既存ファイルは上書きしません。"
        )
        ttk.Label(panel, textvariable=self.save_result, wraplength=620).pack(anchor="w")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="閉じる", command=window.withdraw).pack(side="right")
        self.save_confirm = ttk.Button(buttons, text="保存", command=self._begin_transcript_save)
        self.save_confirm.pack(side="right", padx=(0, 8))
        entry.focus_set()
        window.lift()

    def _begin_transcript_save(self):
        if self.saving or self.closing:
            return
        value = self.save_path.get().strip()
        if not value:
            self.save_result.set("保存先を入力してください。")
            return
        path = Path(value).expanduser()
        self.saving = True
        self.save_confirm.configure(state="disabled")
        self.save_result.set("保存中…")

        def work():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.client.history.save(path, overwrite=False)
                self.mailbox.put(("saved", str(path)))
            except FileExistsError:
                self.mailbox.put(
                    ("save_error", "同名のファイルがあります。別の名前を指定してください。")
                )
            except Exception as exc:
                self.mailbox.put(("save_error", str(exc)))

        threading.Thread(target=work, name="transcript-save", daemon=False).start()

    def show_diagnostics(self):
        if self.diagnostic_window and self.diagnostic_window.winfo_exists():
            self.diagnostic_window.lift()
            return
        self.diagnostic_window = tk.Toplevel(self.root)
        self.diagnostic_window.title("Diagnostics")
        style_window_frame(self.diagnostic_window)
        self.diagnostic_window.geometry("720x400")
        self.diagnostic_text = tk.Text(
            self.diagnostic_window,
            bg=BG,
            fg="#e5e7eb",
            font=("Consolas", 11),
            wrap="word",
            state="disabled",
        )
        self.diagnostic_text.pack(fill="both", expand=True)
        self._last_diagnostic = None

    def _poll(self):
        self._poll_id = None
        try:
            while True:
                kind, value = self.mailbox.get_nowait()
                if kind == "devices":
                    self.refreshing = False
                    self.devices = value
                    default = next((d.name for d in value if d.is_default), "未検出")
                    self.microphone.configure(
                        values=[f"Windows default: {default}"] + [d.label for d in value]
                    )
                    selected = next(
                        (i + 1 for i, d in enumerate(value) if d.key == self.settings.microphone), 0
                    )
                    self.microphone.current(selected)
                    self.local_error = (
                        "" if value else "入力マイクがありません。接続後にRefreshしてください。"
                    )
                elif kind == "device_error":
                    self.refreshing = False
                    self.local_error = f"マイク一覧取得エラー: {value}"
                elif kind in {"saved", "save_error"}:
                    self.saving = False
                    self.local_error = (
                        f"保存しました: {value}" if kind == "saved" else f"保存エラー: {value}"
                    )
                    if self.save_window and self.save_window.winfo_exists():
                        self.save_confirm.configure(state="normal")
                        self.save_result.set(self.local_error)
        except queue.Empty:
            pass
        snapshot = self.client.snapshot()
        self.status_var.set(snapshot["state"])
        english_state = snapshot.get("english_connection")
        if snapshot["state"] == State.RUNNING and english_state not in {None, "RUNNING"}:
            self.status_var.set(f"RUNNING / EN {english_state}")
        snapshot["autosave"] = self.autosave.snapshot()
        self.error_var.set(
            self.autosave.error
            or snapshot.get("recording", {}).get("error")
            or snapshot.get("translation_error")
            or snapshot["error"]
            or snapshot.get("english_error")
            or self.local_error
        )
        self.error_label.configure(wraplength=max(100, self.root.winfo_width() - 24))
        busy = self.client.active or self.refreshing or self.closing
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(
            state="normal"
            if snapshot["state"] not in {State.STOPPED, State.STOPPING}
            else "disabled"
        )
        for control in [self.microphone, self.noise]:
            control.configure(state="disabled" if busy else "readonly")
        self.refresh_button.configure(state="disabled" if busy else "normal")
        dbfs = snapshot.get("dbfs", -120) if self.client.state == State.RUNNING else -120
        self.level.configure(value=max(0, min(60, dbfs + 60)))
        self.level_text.set(f"Mic: {dbfs:.0f} dBFS")
        delayed = snapshot.get("source_delayed") and self.client.state == State.RUNNING
        if self.is_live:
            counts = snapshot.get("translation_status", {})
            pending = counts.get("pending", 0) + counts.get("translating", 0)
            missing = sum(counts.get(k, 0) for k in ("failed", "skipped", "cancelled"))
            self.transcript_status.set(
                f"翻訳待ち {pending} / 未翻訳 {missing}" if pending or missing else ""
            )
        else:
            self.transcript_status.set("英語の受信待ち（日本語は受信中）" if delayed else "")

        self._render_history()
        if self.diagnostic_window and self.diagnostic_window.winfo_exists():
            diagnostic = json.dumps(snapshot, ensure_ascii=False, indent=2)
            if diagnostic != self._last_diagnostic:
                self.diagnostic_text.configure(state="normal")
                self.diagnostic_text.delete("1.0", "end")
                self.diagnostic_text.insert("1.0", diagnostic)
                self.diagnostic_text.configure(state="disabled")
                self._last_diagnostic = diagnostic
        if self.closing and not self.client.active and not self.saving:
            self.autosave.request_close()
            if self.autosave.active:
                self._poll_id = self.root.after(50, self._poll)
                return
            self.root.destroy()
            return
        self._poll_id = self.root.after(50, self._poll)

    def close(self):
        if self.closing:
            return
        if self._save_id:
            self.root.after_cancel(self._save_id)
            self._save_id = None
        self._save_settings()
        self.closing = True
        if not self.is_live:
            self.client._diarization.close()
        self.client.stop()


def run_gui(*, client=None):
    enable_dpi_awareness()
    root = tk.Tk()
    SubtitleApp(root, client=client)
    root.mainloop()
    return 0
