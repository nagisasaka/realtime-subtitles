"""Nonmodal lecture notes. Only explicit button actions request model generation."""

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font, ttk

from .text_copy import ReadOnlyText, install_copy_tree


class SummaryWindow:
    def __init__(self, root, notes, *, on_logs_changed=lambda: None):
        # UI loads this dialog lazily; reuse the native frame/DPI helpers.
        from .ui import BG, style_window_frame, window_dpi

        self.notes = notes
        self.on_logs_changed = on_logs_changed
        self.window = window = tk.Toplevel(root)
        window.withdraw()
        window.title("話者別の要約・講演への質問")
        window.configure(bg=BG)
        window.transient(root)
        style_window_frame(window)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        window.bind("<Escape>", lambda _: window.withdraw())
        self.scale = window_dpi(root) / 96
        self.body_font = font.Font(family="Yu Gothic UI", size=-round(16 * self.scale))
        self.heading_font = font.Font(
            family="Yu Gothic UI", size=-round(18 * self.scale), weight="bold"
        )
        self.control_font = font.Font(family="Yu Gothic UI", size=-round(13 * self.scale))
        self.panel = ttk.Frame(window)
        self.panel.pack(fill="both", expand=True)
        self.controls = ttk.Frame(self.panel)
        self.controls.pack(fill="x")
        self.regenerate_button = ttk.Button(
            self.controls, text="再生成", command=self.regenerate, style="Summary.TButton"
        )
        self.regenerate_button.pack(side="left")
        self.questions_button = ttk.Button(
            self.controls,
            text="質問を生成",
            command=self.generate_questions,
            style="Summary.TButton",
        )
        self.questions_button.pack(side="left")
        self.status = tk.StringVar(window)
        self.status_label = ReadOnlyText(
            self.controls,
            textvariable=self.status,
            auto_height=True,
            bg=BG,
            fg="#bec6d3",
            font=self.control_font,
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.scope = tk.StringVar(
            window, value="「再生成」で、引き継ぎログと今回の確定字幕を要約します。"
        )
        self.scope_label = ReadOnlyText(
            self.panel,
            textvariable=self.scope,
            auto_height=True,
            bg=BG,
            fg="#bec6d3",
            font=self.control_font,
        )
        self.scope_label.pack(fill="x")
        self.log_controls = ttk.Frame(self.panel)
        self.log_controls.pack(fill="x")
        self.add_log_button = ttk.Button(
            self.log_controls,
            text="ログを追加…",
            command=self.add_logs,
            style="Summary.TButton",
        )
        self.add_log_button.pack(side="left")
        self.clear_log_button = ttk.Button(
            self.log_controls,
            text="引継ぎ解除",
            command=self.clear_logs,
            style="Summary.TButton",
        )
        self.clear_log_button.pack(side="left")
        self.log_info = tk.StringVar(window)
        self.log_label = ReadOnlyText(
            self.log_controls,
            textvariable=self.log_info,
            bg=BG,
            fg="#bec6d3",
            font=self.control_font,
            auto_height=True,
        )
        self.log_label.pack(side="left", fill="x", expand=True)
        self.notebook = ttk.Notebook(self.panel, style="Summary.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.texts = []
        for label in ("話者別の要約", "質問案"):
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=label)
            text = ReadOnlyText(
                frame,
                bg=BG,
                fg="#edf0f5",
                insertbackground="#edf0f5",
                selectbackground="#354150",
                font=self.body_font,
                wrap="word",
                state="disabled",
                borderwidth=0,
                highlightthickness=0,
            )
            scrollbar = ttk.Scrollbar(
                frame, orient="vertical", command=text.yview, style="Summary.Vertical.TScrollbar"
            )
            scrollbar.pack(side="right", fill="y")
            text.configure(yscrollcommand=scrollbar.set)
            text.pack(fill="both", expand=True)
            text.tag_configure("speaker", font=self.heading_font, foreground="#a6c8ed")
            self.texts.append(text)
        self.note = ReadOnlyText(
            self.panel,
            bg=BG,
            fg="#bec6d3",
            font=self.control_font,
            auto_height=True,
            text="話者不明の区間は直前話者に暫定割当。結果は字幕と一緒に自動保存します。",
        )
        self.note.pack(side="bottom", fill="x", before=self.notebook)
        self._summary_id = self._questions_id = None
        self._apply_scale()
        self._client_size = round(780 * self.scale), round(620 * self.scale)
        x, y = root.winfo_rootx() + 30, root.winfo_rooty() + 30
        window.geometry(f"{self._client_size[0]}x{self._client_size[1]}+{x}+{y}")
        install_copy_tree(window)

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_set()
        self.refresh()

    def regenerate(self):
        self.notes.regenerate()
        self.refresh()

    def generate_questions(self):
        self.notes.generate_questions()
        self.refresh()

    def add_logs(self):
        paths = filedialog.askopenfilenames(
            parent=self.window,
            title="要約へ引き継ぐ字幕JSONLを選択",
            initialdir=str(Path.home() / "RealtimeSubtitles" / "Autosave"),
            filetypes=[("字幕ログ", "*.jsonl")],
        )
        if paths and self.notes.set_log_paths((*self.notes.log_paths, *paths)):
            self.on_logs_changed()
            self.refresh()

    def clear_logs(self):
        if self.notes.set_log_paths(()):
            self.on_logs_changed()
            self.refresh()

    def _apply_scale(self):
        scale = self.scale
        self.body_font.configure(size=-round(16 * scale))
        self.heading_font.configure(size=-round(18 * scale))
        self.control_font.configure(size=-round(13 * scale))
        style = ttk.Style(self.window)
        for base in ("TButton", "TLabel", "TNotebook.Tab"):
            style.configure("Summary." + base, font=self.control_font)
        style.configure("Summary.TButton", padding=(round(10 * scale), round(6 * scale)))
        style.configure(
            "Summary.TNotebook",
            background="#111318",
            borderwidth=0,
            bordercolor="#111318",
            lightcolor="#111318",
            darkcolor="#111318",
        )
        style.configure(
            "Summary.TNotebook.Tab",
            padding=(round(12 * scale), round(7 * scale)),
            background="#242b35",
            foreground="#bec6d3",
            bordercolor="#111318",
            lightcolor="#242b35",
            darkcolor="#242b35",
        )
        style.map(
            "Summary.TNotebook.Tab",
            background=[("selected", "#354150"), ("active", "#2c3542")],
            foreground=[("selected", "#ffffff"), ("active", "#edf0f5")],
            lightcolor=[("selected", "#354150")],
            darkcolor=[("selected", "#354150")],
        )
        style.configure(
            "Summary.Vertical.TScrollbar", width=round(14 * scale), arrowsize=round(12 * scale)
        )
        self.panel.configure(padding=round(16 * scale))
        self.questions_button.pack_configure(padx=round(8 * scale))
        self.scope_label.pack_configure(pady=round(12 * scale))
        self.log_controls.pack_configure(pady=(0, round(12 * scale)))
        self.clear_log_button.pack_configure(padx=round(8 * scale))
        self.note.pack_configure(pady=(round(10 * scale), 0))
        for text in self.texts:
            text.configure(padx=round(12 * scale), pady=round(10 * scale))
            text.tag_configure("speaker", spacing1=round(12 * scale), spacing3=round(6 * scale))
        self.window.minsize(round(460 * scale), round(340 * scale))

    def _render(self, widget, record):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if record:
            by_key = {s["speaker_key"]: s for s in record["output"]["speakers"]}
            for speaker in record["snapshot"]["speakers"]:
                widget.insert("end", speaker["label"] + "\n", "speaker")
                value = by_key[speaker["key"]]
                if record["kind"] == "lecture_summary":
                    widget.insert("end", value["summary"].strip() + "\n\n")
                else:
                    for i, question in enumerate(value["questions"], 1):
                        widget.insert("end", f"{i}．{question['ja']}\n{question['en']}\n\n")
                    if not value["questions"]:
                        widget.insert("end", "質問を作るための発話情報が不足しています。\n\n")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def refresh(self):
        from .ui import resize_client_for_dpi, window_dpi

        window = self.window
        if not window.winfo_exists():
            return
        if window.winfo_ismapped():
            scale = window_dpi(window) / 96
            if scale != self.scale:
                ratio = scale / self.scale
                width, height = (round(v * ratio) for v in self._client_size)
                self.scale = scale
                self._apply_scale()
                resize_client_for_dpi(window, width, height)
            self._client_size = window.winfo_width(), window.winfo_height()
        active, operation, error, summary, questions = self.notes.snapshot()
        self.regenerate_button.configure(state="disabled" if active else "normal")
        self.questions_button.configure(state="disabled" if active or not summary else "normal")
        self.add_log_button.configure(state="disabled" if active else "normal")
        self.clear_log_button.configure(
            state="disabled" if active or not self.notes.log_paths else "normal"
        )
        self.log_info.set(f"次回の要約：引継ぎ {len(self.notes.log_paths)}ログ ＋ 今回の字幕")
        self.status.set(
            ("要約を生成中…" if operation == "summary" else "質問を生成中…") if active else error
        )
        self.status_label.configure(foreground="#ffb4a9" if error and not active else "#bec6d3")
        if summary and summary["id"] != self._summary_id:
            self._summary_id = summary["id"]
            self._render(self.texts[0], summary)
            stamp = datetime.fromisoformat(summary["snapshot"]["captured_at"]).astimezone()
            omitted = summary["snapshot"]["omitted_count"]
            self.scope.set(
                f"{stamp:%m/%d %H:%M:%S} 時点の確定字幕 {summary['snapshot']['source_count']}区間"
                + f" · 引継ぎ{len(summary['snapshot']['input_logs'])}ログを含む"
                + (f"（上限60,000文字：先頭の{omitted}区間は対象外）" if omitted else "")
            )
            self.notebook.select(0)
        question_id = questions["id"] if questions else None
        if question_id != self._questions_id:
            self._questions_id = question_id
            self._render(self.texts[1], questions)
            if questions:
                self.notebook.select(1)
