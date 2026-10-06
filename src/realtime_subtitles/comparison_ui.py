"""Separate experimental view. The stable subtitle overlay is untouched."""

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from .audio import list_microphones
from .autosave import render_segments
from .ui import BG, configure_dark_style, enable_dpi_awareness, style_window_frame


class ComparisonApp:
    def __init__(self, root, experiment, device=None):
        self.root, self.experiment = root, experiment
        experiment.ensure_autosave()
        self.devices = []
        self.requested_device = device
        self.closing = False
        self._loaded = False
        self.texts = {}
        self.previous = {}
        self.final_lengths = {}
        self.follow = {}
        self.revisions = {}
        self.en_size = tk.IntVar(value=18)
        self.ja_size = tk.IntVar(value=24)
        self.topmost = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="STOPPED")
        self.error = tk.StringVar(value="")
        self.devices_box = None
        root.title(
            "字幕比較 · OpenAI / Speechmatics" if experiment.compare else "字幕実験 · Speechmatics"
        )
        root.configure(bg=BG)
        configure_dark_style(root)
        style_window_frame(root)
        root.attributes("-topmost", True)
        root.geometry(f"{min(1400, root.winfo_screenwidth() - 80)}x650+40+40")
        root.minsize(650, 380)
        controls = ttk.Frame(root, padding=10)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=experiment.stop)
        self.stop_button.pack(side="left", padx=5)
        self.devices_box = ttk.Combobox(controls, state="readonly", width=42)
        self.devices_box.pack(side="left", padx=8)
        ttk.Button(controls, text="Save finals", command=self.save).pack(side="right")
        ttk.Button(controls, text="↓ 最新", command=self.latest).pack(side="right", padx=5)
        options = ttk.Frame(root, padding=(10, 0, 10, 8))
        options.pack(fill="x")
        for title, variable in [("EN px", self.en_size), ("JA px", self.ja_size)]:
            ttk.Label(options, text=title).pack(side="left", padx=(0, 4))
            spin = ttk.Spinbox(
                options, from_=8, to=64, width=4, textvariable=variable, command=self.fonts
            )
            spin.pack(side="left", padx=(0, 12))
            spin.bind("<Return>", lambda event: self.fonts())
            spin.bind("<FocusOut>", lambda event: self.fonts())
        ttk.Checkbutton(
            options,
            text="最前面",
            variable=self.topmost,
            command=lambda: root.attributes("-topmost", self.topmost.get()),
        ).pack(side="left")
        ttk.Label(options, text="薄い文字 = 未確定（置換されます）／空行 = 話者交代").pack(
            side="right"
        )
        ttk.Label(root, textvariable=self.status, wraplength=1300).pack(fill="x", padx=10)
        ttk.Label(root, textvariable=self.error, foreground="#ff9b9b", wraplength=1300).pack(
            fill="x", padx=10
        )
        ttk.Label(root, text=f"自動保存先: {experiment.autosave.directory}").pack(fill="x", padx=10)
        panels = ttk.Frame(root, padding=10)
        panels.pack(fill="both", expand=True)
        providers = ["openai", "speechmatics"] if experiment.compare else ["speechmatics"]
        for column, provider in enumerate(providers):
            panels.columnconfigure(column, weight=1, uniform="providers")
            group = ttk.Frame(panels)
            group.grid(row=0, column=column, sticky="nsew", padx=4)
            group.columnconfigure(0, weight=1)
            ttk.Label(
                group,
                text="OpenAI（安定版経路）" if provider == "openai" else "Speechmatics · enhanced",
            ).grid(row=0, column=0, sticky="w")
            for row, language in enumerate(["en", "ja"]):
                ttk.Label(group, text=language.upper()).grid(
                    row=row * 2 + 1, column=0, sticky="w", pady=(6, 2)
                )
                frame = ttk.Frame(group)
                frame.grid(row=row * 2 + 2, column=0, sticky="nsew")
                group.rowconfigure(row * 2 + 2, weight=1, uniform="languages")
                text = tk.Text(
                    frame,
                    bg=BG,
                    fg="#ededed",
                    wrap="word",
                    height=1,
                    borderwidth=0,
                    state="disabled",
                    insertwidth=0,
                    selectbackground="#3d536d",
                )
                key = (provider, language)
                self.texts[key] = text
                self.previous[key] = ""
                self.follow[key] = True
                scroll = ttk.Scrollbar(frame, command=lambda *args, k=key: self.scroll(k, *args))
                text.configure(yscrollcommand=scroll.set)
                scroll.pack(side="right", fill="y")
                text.pack(fill="both", expand=True)
                text.tag_configure("partial", foreground="#8b99ac")
                text.bind("<MouseWheel>", lambda e, k=key: self.wheel(e, k))
                text.bind("<Prior>", lambda e, k=key: self.scroll(k, "scroll", -1, "pages"))
                text.bind("<Next>", lambda e, k=key: self.scroll(k, "scroll", 1, "pages"))
                text.bind("<End>", lambda e: self.latest())
        panels.rowconfigure(0, weight=1)
        self.fonts()
        self._device_result = None

        def discover():
            try:
                self._device_result = (list_microphones(), "")
            except Exception as exc:
                self._device_result = ([], type(exc).__name__)

        threading.Thread(target=discover, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def fonts(self):
        try:
            en, ja = self.en_size.get(), self.ja_size.get()
        except tk.TclError:
            return
        for (_, language), widget in self.texts.items():
            size = max(8, min(64, en if language == "en" else ja))
            scale = max(1, self.root.winfo_fpixels("1i") / 96)
            widget.configure(font=("Yu Gothic UI", -round(size * scale), "normal"))

    def scroll(self, key, *args):
        widget = self.texts[key]
        widget.yview(*args)
        self.follow[key] = widget.yview()[1] >= 0.999
        return "break"

    def wheel(self, event, key):
        return self.scroll(key, "scroll", (-1 if event.delta > 0 else 1) * 3, "units")

    def latest(self):
        for key, widget in self.texts.items():
            self.follow[key] = True
            widget.see("end-1c")
        return "break"

    def update_text(self, key, final, partial=""):
        widget = self.texts[key]
        text = final + partial
        old = self.previous[key]
        if text == old and self.final_lengths.get(key) == len(final):
            return
        # Edit only the changed suffix, so revised partials do not accumulate or flicker.
        prefix = 0
        for left, right in zip(old, text, strict=False):
            if left != right:
                break
            prefix += 1
        index = f"1.0+{widget.tk.call('string', 'length', text[:prefix])}c"
        widget.mark_set("viewport", "@0,0")
        widget.mark_gravity("viewport", "right")
        widget.configure(state="normal")
        widget.delete(index, "end-1c")
        widget.insert("end-1c", text[prefix:], ())
        widget.tag_remove("partial", "1.0", "end")
        if partial:
            start = f"1.0+{widget.tk.call('string', 'length', final)}c"
            widget.tag_add("partial", start, "end-1c")
        widget.configure(state="disabled")
        if self.follow[key]:
            widget.see("end-1c")
        else:
            widget.yview("viewport")
        self.previous[key] = text
        self.final_lengths[key] = len(final)

    def start(self):
        if not self._loaded:
            return
        selected = self.devices_box.current()
        device = self.devices[selected - 1].index if selected > 0 else None
        self.latest()
        self.experiment.start(device)

    def save(self):
        # Fixed unique destination avoids the native file dialog that previously hung Tk.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        directory = Path.home() / "RealtimeSubtitles" / "Comparisons"

        def work():
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.experiment.speechmatics.history.save(directory / f"{stamp}-speechmatics.jsonl")
                if self.experiment.openai:
                    self.experiment.openai.history.save(
                        directory / f"{stamp}-openai.jsonl", overwrite=False
                    )
                self._save_result = f"保存: {directory} ({stamp})"
            except Exception as exc:
                self._save_result = f"保存エラー: {type(exc).__name__}"

        self._save_result = "保存中…"
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        if self._device_result is not None and not self._loaded:
            self.devices, error = self._device_result
            self._loaded = True
            self.devices_box.configure(values=["Windows default"] + [d.label for d in self.devices])
            selected = next(
                (i + 1 for i, d in enumerate(self.devices) if d.index == self.requested_device), 0
            )
            self.devices_box.current(selected)
            if error:
                self.error.set(error)
        snapshot = self.experiment.snapshot()
        sm = snapshot["speechmatics"]
        oa = snapshot["openai"]
        audio = snapshot["audio"]
        self.status.set(
            f"Mic {audio.get('dbfs', -120):.0f} dBFS | "
            f"Speechmatics: {sm['state']} / EN {sm['events']['AddTranscript']} "
            f"JA {sm['events']['AddTranslation']} finals / drop {sm['dropped_frames']}"
            + (f" | OpenAI: {oa['state']} / EN {oa.get('english_connection', '—')}" if oa else "")
        )
        self.error.set(
            self.experiment.autosave.error
            or snapshot["error"]
            or sm["error"]
            or sm["warning"]
            or (oa["error"] or oa.get("english_error", "") if oa else "")
            or getattr(self, "_save_result", "")
        )
        busy = self.experiment.active or self.closing
        self.start_button.configure(state="disabled" if busy or not self._loaded else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.devices_box.configure(state="disabled" if busy else "readonly")
        history = self.experiment.speechmatics.history
        revision, finals, partials = history.snapshot()
        if revision != self.revisions.get("speechmatics"):
            for lang in ["en", "ja"]:
                final = render_segments([s for s in finals if s.language == lang])
                partial = "".join(s.text for s in partials[lang])
                self.update_text(("speechmatics", lang), final, partial)
            self.revisions["speechmatics"] = revision
        if self.experiment.openai:
            h = self.experiment.openai.history
            revision = (h.revision, h.speaker_boundaries.revision)
            if revision != self.revisions.get("openai"):
                for lang in ["en", "ja"]:
                    self.update_text(("openai", lang), h.rendered_text(lang))
                self.revisions["openai"] = revision
        if self.closing and not self.experiment.active:
            self.experiment.autosave.request_close()
            if self.experiment.autosave.active:
                self.root.after(75, self.poll)
                return
            self.root.destroy()
            return
        self.root.after(75, self.poll)

    def close(self):
        self.closing = True
        self.experiment.stop()


def run_gui(experiment, device=None):
    enable_dpi_awareness()
    root = tk.Tk()
    ComparisonApp(root, experiment, device)
    root.mainloop()
