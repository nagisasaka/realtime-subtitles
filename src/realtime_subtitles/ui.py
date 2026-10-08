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
from tkinter import filedialog, font, ttk

from .audio import list_microphones
from .audio_file import inspect_wav
from .autosave import TranscriptAutosave
from .live_client import LiveClient
from .realtime_api import State
from .settings import Settings
from .subtitle_view import translation_caption, wrap_subtitle

BG = "#111318"


def configure_dark_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    surface, hover, pressed, text = "#242b35", "#354150", "#46566a", "#d9e0ea"
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground="#bec6d3")
    style.configure("TCheckbutton", background=BG, foreground=text)
    style.configure("TRadiobutton", background=BG, foreground=text)
    style.map("TRadiobutton", background=[("active", BG)])
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


def window_dpi(window):
    """Read this HWND's effective DPI, not Tk's process-wide screen DPI."""
    if sys.platform == "win32":
        try:
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user.GetAncestor.restype = ctypes.c_void_p
            user.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            user.GetDpiForWindow.restype = ctypes.c_uint
            hwnd = user.GetAncestor(window.winfo_id(), 2)
            dpi = user.GetDpiForWindow(hwnd)
            if dpi:
                return dpi
        except (AttributeError, OSError):
            pass
    return max(96, round(window.winfo_fpixels("1i")))


def resize_client_for_dpi(window, width, height):
    """Correct Tk's system-DPI frame calculation with the current native frame."""
    if sys.platform != "win32":
        return
    from ctypes import wintypes

    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user.GetAncestor.restype = ctypes.c_void_p
    user.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user.GetWindowLongW.restype = ctypes.c_long
    user.AdjustWindowRectExForDpi.argtypes = [
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_uint,
        ctypes.c_bool,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    user.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    hwnd = user.GetAncestor(window.winfo_id(), 2)
    rect = wintypes.RECT(0, 0, width, height)
    if user.AdjustWindowRectExForDpi(
        ctypes.byref(rect),
        user.GetWindowLongW(hwnd, -16),
        False,
        user.GetWindowLongW(hwnd, -20),
        window_dpi(window),
    ):
        user.SetWindowPos(
            hwnd, None, 0, 0, rect.right - rect.left, rect.bottom - rect.top, 0x16
        )  # NOMOVE | NOZORDER | NOACTIVATE


class SubtitleApp:
    def __init__(
        self,
        root,
        *,
        client=None,
        settings_file=None,
        device_loader=list_microphones,
        audio_file=None,
    ):
        self.root = root
        self.client = client if client is not None else LiveClient()
        self.autosave = TranscriptAutosave({"subtitles": self.client.history})
        self._render_key = None
        self._render_count = 0
        self._caption_layout = {}
        self._history_cursor = 0
        self._history_display_start = 0
        self._history_pending = {}
        self._history_rendered = set()
        self._history_revision_pending = {}
        self._history_group_for_unit = {}
        self._history_english_runs = {}
        self.settings_file = settings_file
        self.settings = Settings.load(settings_file)
        if audio_file is not None:
            self.settings.input_source, self.settings.audio_file = "audio_file", audio_file
        self.device_loader = device_loader
        self.mailbox = queue.Queue()
        self.devices = []
        self.refreshing = False
        self.closing = False
        self.saving = False
        self.save_window = None
        self.local_error = ""
        self._drag = None
        self._poll_id = None
        self._save_id = None
        self.diagnostic_window = None
        self._last_client_size = None
        self._dpi_resize_id = None
        self.scale = window_dpi(root) / 96.0
        root.title("Realtime Subtitles · English / 日本語")
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
        self._source_changed()
        self._poll()

    def _place_window(self):
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(round(1080 * self.scale), screen_w), round(520 * self.scale)
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
        self._initial_client_size = width, height
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
        controls = ttk.Frame(self.settings_window, padding=12)
        controls.pack(fill="x")
        self.source = tk.StringVar(value=self.settings.input_source)
        ttk.Label(controls, text="Input Source").pack(side="left", padx=(0, 12))
        self.source_buttons = []
        for label, value in [("Microphone", "microphone"), ("Audio File", "audio_file")]:
            button = ttk.Radiobutton(
                controls,
                text=label,
                value=value,
                variable=self.source,
                command=self._source_changed,
            )
            button.pack(side="left", padx=5)
            self.source_buttons.append(button)
        self.mic_panel = ttk.Frame(self.settings_window, padding=(12, 0, 12, 8))
        self.microphone = ttk.Combobox(self.mic_panel, state="readonly", width=65)
        self.microphone.pack(side="left", fill="x", expand=True)
        self.microphone.bind("<<ComboboxSelected>>", lambda _: self._schedule_save())
        self.refresh_button = ttk.Button(
            self.mic_panel, text="Refresh", command=self.refresh_devices
        )
        self.refresh_button.pack(side="left", padx=5)
        self.file_panel = ttk.Frame(self.settings_window, padding=(12, 0, 12, 8))
        self.file_path = tk.StringVar(value=self.settings.audio_file)
        self.file_entry = ttk.Entry(self.file_panel, textvariable=self.file_path, width=68)
        self.file_entry.pack(side="left", fill="x", expand=True)
        self.file_entry.bind("<Return>", lambda _: self._inspect_file())
        self.file_entry.bind("<FocusOut>", lambda _: self._inspect_file())
        self.browse_button = ttk.Button(self.file_panel, text="Browse…", command=self._browse_file)
        self.browse_button.pack(side="left", padx=5)
        self.monitor_enabled = tk.BooleanVar(value=self.settings.audio_monitor)
        self.monitor_check = ttk.Checkbutton(
            self.settings_window,
            text="音声モニター：Windowsの既定出力で再生（開始前に選択）",
            variable=self.monitor_enabled,
            command=self._schedule_save,
        )
        self.file_info = tk.StringVar()
        self.file_info_label = ttk.Label(self.settings_window, textvariable=self.file_info)

        options = ttk.Frame(self.settings_window, padding=12)
        options.pack(fill="x")
        self.options_panel = options
        self.start_button = ttk.Button(options, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(options, text="Stop", command=self.client.stop)
        self.stop_button.pack(side="left", padx=6)
        self.topmost = tk.BooleanVar(value=self.settings.always_on_top)
        ttk.Checkbutton(
            options, text="Always on Top", variable=self.topmost, command=self._toggle_topmost
        ).pack(side="left", padx=6)
        self.click_through = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="Click-through (Ctrl+Alt+F10で解除)",
            variable=self.click_through,
            command=self._toggle_click_through,
        ).pack(side="left")
        self._hotkey = False
        ttk.Button(options, text="Clear", command=self.client.history.clear_display).pack(
            side="right"
        )
        ttk.Button(options, text="Save", command=self.save_transcript).pack(side="right", padx=6)
        appearance = ttk.Frame(self.settings_window, padding=(12, 0, 12, 8))
        appearance.pack(fill="x")
        self.en_size = tk.IntVar(value=self.settings.english_size)
        self.ja_size = tk.IntVar(value=self.settings.japanese_size)
        self.en_weight = tk.StringVar(value=self.settings.english_weight)
        self.ja_weight = tk.StringVar(value=self.settings.japanese_weight)
        for label, var, weight in [
            ("英語 px", self.en_size, self.en_weight),
            ("日本語 px", self.ja_size, self.ja_weight),
        ]:
            ttk.Label(appearance, text=label).pack(side="left", padx=(0, 4))
            box = ttk.Spinbox(
                appearance, from_=8, to=64, width=4, textvariable=var, command=self._font_changed
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
        ttk.Label(appearance, text="透明度").pack(side="left")
        self.transparency = tk.DoubleVar(value=self.settings.transparency)
        self.transparency_text = tk.StringVar(value=f"{self.settings.transparency}%")
        ttk.Label(appearance, textvariable=self.transparency_text, width=5).pack(side="right")
        ttk.Scale(
            appearance,
            from_=0,
            to=70,
            variable=self.transparency,
            command=self._transparency_changed,
        ).pack(side="left", fill="x", expand=True)
        details = ttk.Frame(self.settings_window, padding=12)
        details.pack(fill="x")
        ttk.Label(
            details,
            text="最新の英日字幕は上部に固定。下の履歴は新しい順に並び、スクロールで読み返せます。",
            wraplength=640,
        ).pack(anchor="w")
        ttk.Label(details, text=f"自動保存先: {self.autosave.directory}", wraplength=600).pack(
            anchor="w"
        )
        ttk.Button(details, text="未翻訳を再試行", command=self.client.retry_translations).pack(
            anchor="e"
        )
        ttk.Button(details, text="Diagnostics", command=self.show_diagnostics).pack(anchor="e")
        self.transcript_status = tk.StringVar()
        ttk.Label(details, textvariable=self.transcript_status).pack(anchor="w")
        self.error_var = tk.StringVar()
        self.error_label = ttk.Label(
            details, textvariable=self.error_var, foreground="#ffb4a9", wraplength=640
        )
        self.error_label.pack(fill="x")

        self.status_frame = status = ttk.Frame(self.root, padding=(16, 6))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="STOPPED")
        ttk.Label(status, textvariable=self.status_var).pack(side="left", padx=(0, 12))
        self.level = ttk.Progressbar(status, maximum=60, length=90)
        self.level.pack(side="left", padx=(0, 8))
        self.level_text = tk.StringVar()
        ttk.Label(status, textvariable=self.level_text).pack(side="left")
        self.settings_button = ttk.Button(
            status, text="設定", command=self.show_settings, width=5, style="Subtitle.TButton"
        )
        self.settings_button.pack(side="right")
        self.latest_button = ttk.Button(
            status,
            text="最新へ ↑",
            command=lambda: self.history_text.yview_moveto(0),
            style="Subtitle.TButton",
        )
        self.latest_button.pack(side="right", padx=(0, 6))
        self.speaker_var = tk.StringVar()
        ttk.Label(status, textvariable=self.speaker_var).pack(side="right", padx=16)
        self.progress = ttk.Progressbar(status, maximum=100, length=100)
        for widget in [status, *status.winfo_children()]:
            if widget not in (self.settings_button, self.latest_button):
                widget.bind("<ButtonPress-1>", self._drag_start)
                widget.bind("<B1-Motion>", self._drag_move)

    def _source_changed(self):
        if self.client.active:
            return
        self.mic_panel.pack_forget()
        self.file_panel.pack_forget()
        self.file_info_label.pack_forget()
        self.monitor_check.pack_forget()
        if self.source.get() == "audio_file":
            self.file_panel.pack(fill="x", before=self.options_panel)
            self.file_info_label.pack(fill="x", padx=12, before=self.options_panel)
            self.monitor_check.pack(fill="x", padx=12, before=self.options_panel)
            self.progress.pack(side="left", padx=10)
            self._inspect_file()
        else:
            self.mic_panel.pack(fill="x", before=self.options_panel)
            self.progress.pack_forget()
            self.refresh_devices()
        self._schedule_save()

    def _browse_file(self):
        if self.client.active:
            return
        selected = filedialog.askopenfilename(
            parent=self.settings_window, title="PCM16 WAVを選択", filetypes=[("PCM16 WAV", "*.wav")]
        )
        if selected:
            self.file_path.set(selected)
            self._inspect_file()

    def _inspect_file(self):
        path = self.file_path.get()
        if not path:
            self.file_info.set(
                "PCM16 WAV / mono・stereo。音声モニターは下のチェックで選択できます。"
            )
            return

        def work():
            try:
                info = inspect_wav(path)
                self.mailbox.put(("file_info", (path, info)))
            except Exception as exc:
                self.mailbox.put(("file_info", (path, str(exc))))

        threading.Thread(target=work, name="wav-inspect", daemon=True).start()
        self._schedule_save()

    def _build_captions(self):
        self.captions = tk.Frame(self.root, bg=BG)
        self.captions.pack(fill="both", expand=True, padx=24, pady=(8, 16))
        self._en_logical_size = self.en_size.get()
        self._ja_logical_size = self.ja_size.get()
        self.status_font = font.Font(family="Segoe UI", size=-round(12 * self.scale))
        self.en_font = font.Font(
            family="Segoe UI",
            size=-round(self.en_size.get() * self.scale),
            weight=self.en_weight.get(),
        )
        self.confirmed_font = font.Font(
            family="Segoe UI", size=-round(self.en_size.get() * 0.9 * self.scale), weight="normal"
        )
        self.ja_font = font.Font(
            family="Yu Gothic UI",
            size=-round(self.ja_size.get() * self.scale),
            weight=self.ja_weight.get(),
        )
        self.caption_widgets = {}
        for name, face, color in [
            ("ja", self.ja_font, "#aeb9cc"),
            ("live", self.en_font, "#ffffff"),
        ]:
            widget = tk.Label(
                self.captions,
                bg=BG,
                fg=color,
                font=face,
                anchor="nw",
                justify="left",
                bd=0,
                padx=0,
                pady=0,
            )
            self.caption_widgets[name] = widget
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        self.live_text = self.caption_widgets["live"]
        self.ja_text = self.caption_widgets["ja"]
        self.history_frame = tk.Frame(self.captions, bg=BG)
        self.history_text = tk.Text(
            self.history_frame,
            bg=BG,
            fg="#dbe2ec",
            bd=0,
            highlightthickness=0,
            wrap="word",
            state="disabled",
            cursor="arrow",
            padx=0,
            pady=0,
            font=self.confirmed_font,
            selectbackground="#394556",
            takefocus=True,
        )
        scroll = ttk.Scrollbar(
            self.history_frame,
            command=self.history_text.yview,
            style="Subtitle.Vertical.TScrollbar",
        )
        self.history_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.history_text.pack(side="left", fill="both", expand=True)
        self.history_text.tag_configure("ja", font=self.ja_font, foreground="#aeb9cc")
        self.history_text.tag_configure("en", font=self.confirmed_font, spacing3=12)
        self.history_text.tag_configure("speaker", foreground="#8793a6", font=self.status_font)
        self.captions.bind("<Configure>", lambda _: self._layout_captions())
        self._apply_display_scale()

    def _layout_captions(self):
        width = max(1, self.captions.winfo_width())
        x = 0
        gap = round(4 * self.scale)
        live = self.en_font.metrics("linespace") * 2
        ja = self.ja_font.metrics("linespace") * 2
        # Translation arrival never changes the live English's screen position.
        self.ja_text.place(x=x, y=0, width=width, height=ja)
        self.live_text.place(x=x, y=ja + gap, width=width, height=live)
        history_y = ja + live + 2 * gap
        self.history_frame.place(
            x=x,
            y=history_y,
            width=width,
            height=max(1, self.captions.winfo_height() - history_y),
        )
        self.root.minsize(round(480 * self.scale), history_y + round(160 * self.scale))
        self._render_key = None
        self._caption_layout.clear()

    def show_settings(self, event=None):
        self.settings_window.deiconify()
        self.settings_window.lift()
        return "break"

    def _update_history_pairs(self, view, changed, display_start, revisions=()):
        text = self.history_text
        reset = self._history_display_start != display_start
        if reset:
            self._history_display_start = display_start
            self._history_pending.clear()
            self._history_rendered.clear()
            self._history_revision_pending.clear()
            self._history_group_for_unit.clear()
            self._history_english_runs.clear()
        for revision in revisions:
            if (
                revision.translation.translation_status == "completed"
                and revision.paragraphs
                and revision.translation.source_segment_ids[0] >= display_start
            ):
                self._history_revision_pending[revision.revision_id] = revision
        self._history_pending.update((u.unit_id, u) for u in changed)
        ready = [u for i, u in self._history_pending.items() if i <= view.history_latest_id]
        applicable = [
            r
            for r in self._history_revision_pending.values()
            if max(r.unit_ids) <= view.history_latest_id
        ]
        if not ready and not reset and not applicable:
            return
        # A mark follows inserts above the reader, including late translations.
        at_top = text.yview()[0] < 0.00001
        text.mark_set("reading_position", "@0,0")
        text.mark_gravity("reading_position", "right")
        text.configure(state="normal")
        if reset:
            text.delete("1.0", "end")
            for mark in text.mark_names():
                if mark.startswith("pair_"):
                    text.mark_unset(mark)
            at_top = True
        for unit in sorted(ready, key=lambda u: u.unit_id):
            identity = unit.unit_id
            if identity in self._history_group_for_unit:
                del self._history_pending[identity]
                continue  # Late original JA must not replace the reconstructed group.
            begin = f"pair_{identity}_start"
            ja_start, en_start = f"pair_{identity}_ja", f"pair_{identity}_en"
            speaker = f"↳ {unit.speaker or ''}\n" if unit.break_before else ""
            ja = (
                translation_caption(
                    unit.ja_text if unit.translation_status == "completed" else "",
                    unit.translation_status,
                )
                + "\n"
            )
            if identity in self._history_rendered:
                # Leave the authoritative English (and any reading mark inside it)
                # untouched when the pending Japanese becomes available.
                begin_position, position = text.index(begin), text.index(ja_start)
                reading_ja = text.compare("reading_position", ">=", ja_start) and text.compare(
                    "reading_position", "<", en_start
                )
                text.delete(ja_start, en_start)
                text.insert(position, ja, "ja")
                text.mark_set(begin, begin_position)
                text.mark_set(ja_start, position)
                if reading_ja:
                    text.mark_set("reading_position", ja_start)
            else:
                # One insertion; reuse the same widget for the entire session.
                text.insert("1.0", speaker, "speaker", ja, "ja", unit.en_text + "\n", "en")
                text.mark_set(begin, "1.0")
                for mark, prefix in [(ja_start, speaker), (en_start, speaker + ja)]:
                    length = text.tk.call("string", "length", prefix)
                    text.mark_set(mark, f"1.0+{length}c")
                for mark in (begin, ja_start, en_start):
                    text.mark_gravity(mark, "right")
                end = f"pair_{identity}_end"
                length = text.tk.call("string", "length", speaker + ja + unit.en_text + "\n")
                text.mark_set(end, f"1.0+{length}c")
                text.mark_gravity(end, "left")
                self._history_english_runs[identity] = [
                    (en_start, 0, text.tk.call("string", "length", unit.en_text))
                ]
            self._history_rendered.add(identity)
            del self._history_pending[identity]
        for revision in sorted(applicable, key=lambda r: r.revision_id):
            del self._history_revision_pending[revision.revision_id]
            if any(
                self._history_group_for_unit.get(i, -1) > revision.revision_id
                for i in revision.unit_ids
            ):
                continue  # A slower old response cannot undo a newer grouping.
            if all(i in self._history_rendered for i in revision.unit_ids):
                self._replace_history_group(revision)
        text.configure(state="disabled")
        if at_top:
            text.yview_moveto(0)
        else:
            text.yview("reading_position")

    def _replace_history_group(self, revision):
        text = self.history_text
        unit, ids = revision.translation, revision.unit_ids
        start, end = f"pair_{max(ids)}_start", f"pair_{min(ids)}_end"
        position = text.index(start)
        within = text.compare("reading_position", ">=", start) and text.compare(
            "reading_position", "<", end
        )
        # Preserve an original-English character anchor across semantic splits.
        # Japanese can now be inserted INSIDE an old unit, so keep individual runs.
        anchor = None
        for identity in ids:
            for mark, local_offset, size in self._history_english_runs.get(identity, ()):
                if (
                    within
                    and text.compare("reading_position", ">=", mark)
                    and text.compare("reading_position", "<", f"{mark}+{size}c")
                ):
                    count = text.count(mark, "reading_position", "chars")
                    anchor = (identity, local_offset + (count[0] if count else 0))
        with self.client.history._lock:
            originals = [self.client.history._segments[i] for i in ids]
        text.delete(start, end)
        speaker = f"↳ {unit.speaker or ''}\n" if unit.break_before else ""
        chunks = [speaker, "speaker"]
        rendered = speaker
        english_runs = []

        def size(value):
            return int(text.tk.call("string", "length", value))

        for paragraph in revision.paragraphs:
            ja = paragraph.ja_text + "\n"
            en = unit.en_text[paragraph.en_start : paragraph.en_end]
            english_runs.append((paragraph, size(rendered + ja)))
            chunks.extend((ja, "ja", en + "\n", "en"))
            rendered += ja + en + "\n"
        text.insert(position, *chunks)
        prefix = 0
        for original in originals:
            identity = original.unit_id
            raw = original.en_text.strip()
            leading = size(
                original.en_text[: len(original.en_text) - len(original.en_text.lstrip())]
            )
            for mark, _, _ in self._history_english_runs.get(identity, ()):
                if "_run_" in mark:
                    text.mark_unset(mark)
            runs = []
            for paragraph, rendered_start in english_runs:
                lo, hi = max(prefix, paragraph.en_start), min(prefix + len(raw), paragraph.en_end)
                if lo >= hi:
                    continue
                mark = f"pair_{identity}_run_{len(runs)}"
                offset = rendered_start + size(unit.en_text[paragraph.en_start : lo])
                text.mark_set(mark, f"{position}+{offset}c")
                text.mark_gravity(mark, "right")
                runs.append(
                    (mark, leading + size(raw[: lo - prefix]), size(raw[lo - prefix : hi - prefix]))
                )
            self._history_english_runs[identity] = runs
            marks = {
                "start": position,
                "ja": f"{position}+{size(speaker)}c",
                "en": runs[0][0],
                "end": f"{position}+{size(rendered)}c",
            }
            for suffix, index in marks.items():
                mark = f"pair_{identity}_{suffix}"
                text.mark_set(mark, index)
                text.mark_gravity(mark, "left" if suffix == "end" else "right")
            self._history_group_for_unit[identity] = revision.revision_id
            prefix += len(raw) + 1
        if within:
            text.mark_set("reading_position", position)
            if anchor:
                identity, offset = anchor
                for mark, local, length in self._history_english_runs[identity]:
                    if local <= offset < local + length:
                        text.mark_set("reading_position", f"{mark}+{offset - local}c")
                        break

    def _render_history(self):
        previous_cursor = self._history_cursor
        view, changed, self._history_cursor, display_start = self.client.history.subtitle_frame(
            previous_cursor
        )
        revisions = self.client.history.reconstructions.changes(
            previous_cursor, self._history_cursor
        )
        self._update_history_pairs(view, changed, display_start, revisions)
        width = max(1, self.live_text.winfo_width())
        key = (view, width)
        if key == self._render_key:
            return
        self._render_key = key
        self._render_count += 1
        values = [
            (self.live_text, view.en_text, self.en_font, 2),
            (
                self.ja_text,
                translation_caption(view.ja_text, view.translation_status),
                self.ja_font,
                2,
            ),
        ]
        for widget, value, face, lines in values:
            layout_key = (value, width, lines)
            if self._caption_layout.get(widget) == layout_key:
                continue
            rendered = wrap_subtitle(value, face.measure, width, lines)
            if widget.cget("text") != rendered:
                widget.configure(text=rendered)
            self._caption_layout[widget] = layout_key
        self.speaker_var.set(("↳ " if view.speaker_changed else "") + (view.speaker or ""))

    def _toggle_click_through(self):
        if sys.platform != "win32":
            self.click_through.set(False)
            return
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetAncestor.restype = ctypes.c_void_p
        user.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        hwnd = user.GetAncestor(self.root.winfo_id(), 2)
        user.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user.SetWindowLongPtrW.restype = ctypes.c_void_p
        user.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user.CallWindowProcW.restype = ctypes.c_ssize_t
        if self.click_through.get() and not self._hotkey:
            self._hotkey = bool(user.RegisterHotKey(hwnd, 0x5342, 0x4003, 0x79))
            if self._hotkey:
                callback = ctypes.WINFUNCTYPE(
                    ctypes.c_ssize_t,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_size_t,
                    ctypes.c_ssize_t,
                )

                def receive(window, message, wparam, lparam):
                    if message == 0x312 and wparam == 0x5342:
                        self.mailbox.put(("hotkey", None))
                        return 0
                    return user.CallWindowProcW(
                        self._original_proc, window, message, wparam, lparam
                    )

                self._window_proc = callback(receive)
                self._original_proc = user.SetWindowLongPtrW(hwnd, -4, self._window_proc)
            else:
                self.click_through.set(False)
                self.local_error = "Ctrl+Alt+F10を登録できないためClick-throughを有効にできません。"
        style = user.GetWindowLongW(hwnd, -20)
        user.SetWindowLongW(
            hwnd, -20, (style | 0x80020) if self.click_through.get() else (style & ~0x20)
        )
        if self.click_through.get():
            # WS_EX_TRANSPARENT requires a layered window for hit-test passthrough,
            # including when the user selected 100% opacity.
            user.SetLayeredWindowAttributes.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_ubyte,
                ctypes.c_uint,
            ]
            user.SetLayeredWindowAttributes(hwnd, 0, round(255 * self.root.attributes("-alpha")), 2)
        if not self.click_through.get() and self._hotkey:
            user.UnregisterHotKey(hwnd, 0x5342)
            user.SetWindowLongPtrW(hwnd, -4, self._original_proc)
            self._hotkey = False

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
        self._en_logical_size, self._ja_logical_size = en, ja
        self._apply_display_scale()
        self._schedule_save()

    def _refresh_dpi(self):
        if not self.root.winfo_ismapped():
            return
        scale = window_dpi(self.root) / 96.0
        old_scale = self.scale
        initial = self._last_client_size is None
        previous_size = self._last_client_size or self._initial_client_size
        if scale != old_scale:
            self.scale = scale
            self._apply_display_scale()
        if initial or scale != old_scale:
            # Restore saved client dimensions after mapping on the target monitor;
            # on moves preserve logical size. Let Windows manage maximized windows.
            saved_dpi = self.settings.geometry_dpi if self.settings.geometry else 0
            ratio = (scale * 96 / saved_dpi if saved_dpi else 1) if initial else scale / old_scale
            size = tuple(round(v * ratio) for v in previous_size)
            self._last_client_size = size
            if self.root.state() == "normal":
                self._resize_for_dpi(*size)
        elif self._dpi_resize_id is None:
            self._last_client_size = self.root.winfo_width(), self.root.winfo_height()

    def _resize_for_dpi(self, width, height):
        if self._dpi_resize_id is not None:
            self.root.after_cancel(self._dpi_resize_id)
        minimum = self.root.minsize()
        width, height = max(width, minimum[0]), max(height, minimum[1])
        self.root.geometry(f"{width}x{height}")

        def correct_native_frame():
            self._dpi_resize_id = None
            if self.closing or self.root.state() != "normal":
                return
            resize_client_for_dpi(self.root, width, height)
            self._last_client_size = width, height

        # Run after Tk applies geometry, so its old frame calculation cannot undo
        # the correction. No sleep or nested event loop on the Tk thread.
        self._dpi_resize_id = self.root.after_idle(correct_native_frame)

    def _apply_display_scale(self):
        # Named fonts/widgets are reused. Do not change global `tk scaling`:
        # a separate settings window may be on another monitor.
        self.en_font.configure(
            size=-round(self._en_logical_size * self.scale), weight=self.en_weight.get()
        )
        self.ja_font.configure(
            size=-round(self._ja_logical_size * self.scale), weight=self.ja_weight.get()
        )
        self.confirmed_font.configure(size=-round(self._en_logical_size * 0.9 * self.scale))
        self.status_font.configure(size=-round(12 * self.scale))
        style = ttk.Style(self.root)
        style.configure(
            "Subtitle.TButton",
            font=self.status_font,
            padding=(round(9 * self.scale), round(5 * self.scale)),
        )
        style.configure(
            "Subtitle.Vertical.TScrollbar",
            width=round(14 * self.scale),
            arrowsize=round(12 * self.scale),
        )
        for widget in self.status_frame.winfo_children():
            if isinstance(widget, ttk.Label):
                widget.configure(font=self.status_font)
        self.status_frame.configure(padding=(round(16 * self.scale), round(6 * self.scale)))
        self.level.configure(length=round(90 * self.scale))
        self.progress.configure(length=round(100 * self.scale))
        self.captions.pack_configure(
            padx=round(24 * self.scale), pady=(round(8 * self.scale), round(16 * self.scale))
        )
        self.history_text.tag_configure("en", spacing3=round(12 * self.scale))
        self._layout_captions()

    def _transparency_changed(self, value):
        transparency = max(0, min(70, round(float(value))))
        self.transparency_text.set(f"{transparency}%")
        self.root.attributes("-alpha", 1 - transparency / 100)
        self._schedule_save()

    def _configure(self, event):
        if event.widget is self.root:
            self._refresh_dpi()
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
        self.settings.geometry_dpi = round(self.scale * 96)
        try:
            self.settings.english_size = self.en_size.get()
            self.settings.japanese_size = self.ja_size.get()
        except tk.TclError:
            pass
        self.settings.always_on_top = self.topmost.get()
        self.settings.english_weight = self.en_weight.get()
        self.settings.japanese_weight = self.ja_weight.get()
        self.settings.input_source = self.source.get()
        self.settings.audio_file = self.file_path.get()
        self.settings.audio_monitor = self.monitor_enabled.get()
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
        if self.client.active or (self.refreshing and self.source.get() == "microphone"):
            return
        self.local_error = ""
        index = self.microphone.current()
        device = self.devices[index - 1].index if index > 0 else None
        self._save_settings()
        self.client.start(
            device,
            audio_file=self.file_path.get() if self.source.get() == "audio_file" else None,
            audio_monitor=self.monitor_enabled.get(),
        )

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
                if kind == "hotkey":
                    self.click_through.set(False)
                    self._toggle_click_through()
                    self.show_settings()
                elif kind == "devices":
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
                elif kind == "file_info":
                    path, info = value
                    if path == self.file_path.get():
                        self.file_info.set(
                            info
                            if isinstance(info, str)
                            else f"{info['audio_duration_ms'] / 1000:.1f}s · WAV / "
                            f"{info['audio_sample_rate'] / 1000:g} kHz / "
                            f"{info['audio_channels']} ch · PCM16"
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
        self._refresh_dpi()  # Includes initial map and DPI changes without a resize.
        snapshot = self.client.snapshot()
        snapshot["display"] = {"dpi": round(self.scale * 96), "scale": self.scale}
        is_file = self.source.get() == "audio_file"
        self.status_var.set(
            snapshot.get("playback_state", "Idle") if is_file else snapshot["state"]
        )
        if is_file and snapshot.get("playback_state") == "Finished":
            if (
                self.autosave.flushed_cursor.get("subtitles", 0)
                < self.client.history.journal_cursor
            ):
                self.status_var.set("Draining / 保存中")
                self.autosave.request_flush()
        snapshot["autosave"] = self.autosave.snapshot()
        self.error_var.set(
            self.autosave.error
            or snapshot.get("audio_monitor", {}).get("error")
            or snapshot.get("recording", {}).get("error")
            or snapshot.get("translation_error")
            or snapshot["error"]
            or self.local_error
        )
        busy = self.client.active or self.closing
        self.start_button.configure(
            state="disabled" if busy or (self.refreshing and not is_file) else "normal"
        )
        self.stop_button.configure(state="normal" if self.client.active else "disabled")
        self.microphone.configure(state="disabled" if busy else "readonly")
        for control in [
            self.monitor_check,
            self.refresh_button,
            self.browse_button,
            self.file_entry,
            *self.source_buttons,
        ]:
            control.configure(state="disabled" if busy else "normal")
        dbfs = snapshot.get("dbfs", -120) if self.client.state == State.RUNNING else -120
        self.level.configure(value=max(0, min(60, dbfs + 60)))
        if is_file:
            position, duration = (
                snapshot.get("position_ms", 0),
                snapshot.get("audio_duration_ms", 0),
            )

            def stamp(ms):
                seconds = max(0, ms) // 1000
                return f"{seconds // 60:02d}:{seconds % 60:02d}"

            self.level_text.set(
                f"{Path(self.file_path.get()).name}  {stamp(position)} / {stamp(duration)}"
            )
            self.progress.configure(value=position * 100 / max(1, duration))
        else:
            self.level_text.set(f"Mic: {dbfs:.0f} dBFS")
        counts = snapshot.get("translation_status", {})
        pending = sum(counts.get(k, 0) for k in ("pending", "translating", "retrying"))
        missing = sum(
            counts.get(k, 0) for k in ("failed", "skipped", "cancelled", "validation_failed")
        )
        self.transcript_status.set(
            f"翻訳待ち {pending} / 未翻訳 {missing}" if pending or missing else ""
        )

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
                self._poll_id = self.root.after(33, self._poll)
                return
            self.root.destroy()
            return
        self._poll_id = self.root.after(33, self._poll)

    def close(self):
        if self.closing:
            return
        if self._save_id:
            self.root.after_cancel(self._save_id)
            self._save_id = None
        self._save_settings()
        self.closing = True
        self.click_through.set(False)
        self._toggle_click_through()
        self.client.stop()


def run_gui(*, client=None, audio_file=None):
    enable_dpi_awareness()
    root = tk.Tk()
    SubtitleApp(root, client=client, audio_file=audio_file)
    root.mainloop()
    return 0
