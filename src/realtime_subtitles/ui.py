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
from .subtitle_view import wrap_subtitle

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
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
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
        width, height = min(round(1080 * self.scale), screen_w), round(330 * self.scale)
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
            text="確定EN 1行・ライブEN 2行・JA 2行。履歴は自動保存ファイルで確認できます。",
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

        status = ttk.Frame(self.root, padding=(16, 6))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="STOPPED")
        ttk.Label(status, textvariable=self.status_var).pack(side="left", padx=(0, 12))
        self.level = ttk.Progressbar(status, maximum=60, length=90)
        self.level.pack(side="left", padx=(0, 8))
        self.level_text = tk.StringVar()
        ttk.Label(status, textvariable=self.level_text).pack(side="left")
        self.settings_button = ttk.Button(status, text="設定", command=self.show_settings, width=5)
        self.settings_button.pack(side="right")
        self.speaker_var = tk.StringVar()
        ttk.Label(status, textvariable=self.speaker_var).pack(side="right", padx=16)
        self.progress = ttk.Progressbar(status, maximum=100, length=100)
        for widget in [status, *status.winfo_children()]:
            if widget is not self.settings_button:
                widget.bind("<ButtonPress-1>", self._drag_start)
                widget.bind("<B1-Motion>", self._drag_move)

    def _source_changed(self):
        if self.client.active:
            return
        self.mic_panel.pack_forget()
        self.file_panel.pack_forget()
        self.file_info_label.pack_forget()
        if self.source.get() == "audio_file":
            self.file_panel.pack(fill="x", before=self.options_panel)
            self.file_info_label.pack(fill="x", padx=12, before=self.options_panel)
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
            self.file_info.set("PCM16 WAV / mono・stereo。音声モニター OFF（スピーカー再生なし）")
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
            ("confirmed", self.confirmed_font, "#bdc5d2"),
            ("partial", self.en_font, "#ffffff"),
            ("ja", self.ja_font, "#e9eef6"),
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
        self.en_text = self.caption_widgets["confirmed"]
        self.partial_text = self.caption_widgets["partial"]
        self.ja_text = self.caption_widgets["ja"]
        self.ja_hint = tk.Label(
            self.captions,
            text="確定ENの訳",
            font=("Yu Gothic UI", -round(12 * self.scale)),
            bg=BG,
            fg="#8693a8",
            anchor="w",
            padx=0,
        )
        self.captions.bind("<Configure>", lambda _: self._layout_captions())
        self._layout_captions()

    def _layout_captions(self):
        width = min(max(1, self.captions.winfo_width()), round(1100 * self.scale))
        x = max(0, (self.captions.winfo_width() - width) // 2)
        gap = round(12 * self.scale)
        confirmed = self.confirmed_font.metrics("linespace")
        live = self.en_font.metrics("linespace") * 2
        ja = self.ja_font.metrics("linespace") * 2
        hint = round(20 * self.scale)
        self.en_text.place(x=x, y=0, width=width, height=confirmed)
        self.partial_text.place(x=x, y=confirmed + gap, width=width, height=live)
        self.ja_hint.place(x=x, y=confirmed + live + 2 * gap, width=width, height=hint)
        self.ja_text.place(x=x, y=confirmed + live + 2 * gap + hint, width=width, height=ja)
        self.root.minsize(
            round(480 * self.scale), confirmed + live + ja + hint + 2 * gap + round(86 * self.scale)
        )
        self._render_key = None
        self._caption_layout.clear()

    def show_settings(self, event=None):
        self.settings_window.deiconify()
        self.settings_window.lift()
        return "break"

    def _render_history(self):
        view = self.client.history.subtitle_view()
        width = max(1, self.en_text.winfo_width())
        key = (view, width)
        if key == self._render_key:
            return
        self._render_key = key
        self._render_count += 1
        values = [
            (self.en_text, view.confirmed_en, self.confirmed_font, 1),
            (self.partial_text, view.live_en_partial, self.en_font, 2),
            (self.ja_text, view.ja_text, self.ja_font, 2),
        ]
        if not view.ja_text and view.confirmed_unit_id is not None:
            status = view.translation_status
            message = (
                "翻訳待ち…"
                if status in {"pending", "translating", "retrying"}
                else "翻訳検証エラー"
                if status == "validation_failed"
                else "未翻訳"
            )
            values[-1] = (self.ja_text, message, self.ja_font, 2)
        for widget, text, face, lines in values:
            layout_key = (text, width, lines)
            if self._caption_layout.get(widget) == layout_key:
                continue
            value = wrap_subtitle(text, face.measure, width, lines)
            if widget.cget("text") != value:
                widget.configure(text=value)
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
            hwnd, -20, (style | 0x20) if self.click_through.get() else (style & ~0x20)
        )
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
        self.en_font.configure(size=-round(en * self.scale), weight=self.en_weight.get())
        self.ja_font.configure(size=-round(ja * self.scale), weight=self.ja_weight.get())
        self.confirmed_font.configure(size=-round(en * 0.9 * self.scale))
        self._layout_captions()
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
        self.settings.input_source = self.source.get()
        self.settings.audio_file = self.file_path.get()
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
            device, audio_file=self.file_path.get() if self.source.get() == "audio_file" else None
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
                            f"{info['audio_channels']} ch · PCM16 · モニター OFF"
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
