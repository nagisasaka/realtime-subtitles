"""Native text selection and clipboard actions for readonly subtitle surfaces."""

import tkinter as tk
from tkinter import ttk


def install_copy(widget):
    """Add consistent copy/select-all/menu bindings without changing editability."""
    if hasattr(widget, "copy_menu"):
        return
    is_text = isinstance(widget, tk.Text)
    is_entry = isinstance(widget, (tk.Entry, ttk.Entry))

    def full_text():
        if is_text:
            return widget.get("1.0", "end-1c")
        if is_entry:
            return widget.get()
        variable = widget.cget("textvariable") if "textvariable" in widget.keys() else ""
        return str(widget.getvar(str(variable)) if variable else widget.cget("text"))

    def selected_text():
        if is_text and widget.tag_ranges("sel"):
            return widget.get("sel.first", "sel.last")
        if is_entry and widget.selection_present():
            return widget.get()[widget.index("sel.first") : widget.index("sel.last")]
        return ""

    def copy(event=None, *, entire=False):
        value = full_text() if entire or not (is_text or is_entry) else selected_text()
        if value:
            widget.clipboard_clear()
            widget.clipboard_append(value)
        return "break"

    def select_all(event=None):
        widget.focus_set()
        if is_text:
            widget.tag_add("sel", "1.0", "end-1c")
            widget.tag_raise("sel")
        elif is_entry:
            widget.selection_range(0, "end")
        return "break"

    menu = widget.copy_menu = tk.Menu(widget, tearoff=False)
    menu.add_command(label="コピー", command=copy, accelerator="Ctrl+C")
    if is_text or is_entry:
        menu.add_command(label="すべて選択", command=select_all, accelerator="Ctrl+A")
        menu.add_command(label="全文をコピー", command=lambda: copy(entire=True))

        def focus(event=None):
            widget.focus_set()
            if is_text:
                widget.tag_raise("sel")

        widget.bind("<Button-1>", focus, add="+")
        for event in ("<Control-a>", "<Control-A>"):
            widget.bind(event, select_all)
        if is_text:
            widget.configure(exportselection=False, takefocus=True)
    for event in ("<<Copy>>", "<Control-c>", "<Control-C>"):
        widget.bind(event, copy)

    def popup(event):
        widget.focus_set()
        menu.entryconfigure(
            0, state="normal" if not (is_text or is_entry) or selected_text() else "disabled"
        )
        x, y = event.x_root, event.y_root
        if event.type != tk.EventType.ButtonPress:
            x, y = widget.winfo_rootx() + 12, widget.winfo_rooty() + 12
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        return "break"  # Do not open the subtitle settings on the same right click.

    for event in ("<Button-3>", "<Shift-F10>", "<Menu>"):
        widget.bind(event, popup)


def install_copy_tree(root):
    """Readonly messages use Text; ordinary form labels offer whole-label copy."""
    for widget in root.winfo_children():
        if isinstance(widget, (tk.Text, tk.Entry, ttk.Entry, tk.Label, ttk.Label)):
            install_copy(widget)
        if not isinstance(widget, tk.Menu):
            install_copy_tree(widget)


class ReadOnlyText(tk.Text):
    def __init__(self, master, *, text="", textvariable=None, auto_height=False, **kwargs):
        options = dict(
            state="disabled",
            width=1,
            height=1,
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            cursor="xterm",
            selectbackground="#394556",
            selectforeground="#ffffff",
            inactiveselectbackground="#394556",
        )
        options.update(kwargs)
        super().__init__(master, **options)
        self._auto_height = auto_height
        self._fit_job = None
        self._variable = textvariable
        self._trace = None
        install_copy(self)
        self.bind("<Configure>", lambda _: self._schedule_fit(), add="+")
        self.bind("<Destroy>", self._cleanup, add="+")
        if textvariable is not None:
            self._trace = textvariable.trace_add(
                "write", lambda *_: self.set_text(textvariable.get())
            )
            text = textvariable.get()
        self.set_text(text)

    def set_text(self, value):
        old = self.get("1.0", "end-1c")
        if value == old:
            return
        # Update only the changed range so a selected stable prefix remains selected
        # while a live partial grows. Tk marks/tags follow edits in the rest of the text.
        prefix = 0
        for left, right in zip(old, value, strict=False):
            if left != right:
                break
            prefix += 1
        suffix = 0
        for left, right in zip(reversed(old[prefix:]), reversed(value[prefix:]), strict=False):
            if left != right:
                break
            suffix += 1
        start = f"1.0+{self.tk.call('string', 'length', old[:prefix])}c"
        end = f"1.0+{self.tk.call('string', 'length', old[: len(old) - suffix])}c"
        replacement = value[prefix : len(value) - suffix]
        self.configure(state="normal")
        self.delete(start, end)
        self.insert(start, replacement, ())
        self.configure(state="disabled")
        self._schedule_fit()

    def _schedule_fit(self):
        if self._auto_height and self._fit_job is None:
            self._fit_job = self.after_idle(self._fit_height)

    def _fit_height(self):
        self._fit_job = None
        if not self.winfo_ismapped() or self.winfo_width() < 20:
            return
        lines = self.count("1.0", "end-1c", "displaylines")
        height = (lines[0] if lines else 0) + 1
        if self.cget("height") != height:
            self.configure(height=height)

    def _cleanup(self, event):
        if event.widget is not self:
            return
        if self._trace:
            self._variable.trace_remove("write", self._trace)
            self._trace = None
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
