"""Native Windows frozen-output preview; no microphone, network, or app settings."""

import argparse
import json
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import font

from realtime_subtitles.ui import enable_dpi_awareness, window_dpi


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=8)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("Native Windows preview required")
    manifest, a, b = load(args.manifest), load(args.a), load(args.b)
    windows = sorted(
        [w for w in manifest["windows"] if w["id"] in b["results"]],
        key=lambda w: (w["meeting_id"], w["start_ms"]),
    )
    enable_dpi_awareness()
    root = tk.Tk()
    root.title("History readability evaluation — frozen A/B (no audio)")
    root.geometry("2600x1200+60+150")
    root.configure(bg="#101014")
    root.update_idletasks()
    scale = window_dpi(root) / 96
    en = font.Font(family="Segoe UI", size=-round(20 * 0.9 * scale))
    ja = font.Font(family="Yu Gothic UI", size=-round(12 * scale))
    label = tk.Label(root, bg="#101014", fg="white", font=("Segoe UI", 12))
    label.pack(pady=6)
    columns = tk.Frame(root, bg="#101014")
    columns.pack(fill="both", expand=True)
    widgets = []
    for col, title in enumerate(("A", "B")):
        frame = tk.Frame(columns, bg="#101014")
        frame.grid(row=0, column=col, sticky="nsew", padx=16, pady=8)
        columns.columnconfigure(col, weight=1, uniform="pair")
        tk.Label(frame, text=title, bg="#101014", fg="white").pack()
        widget = tk.Text(
            frame,
            bg="#101014",
            fg="#dbe2ec",
            wrap="word",
            bd=0,
            highlightthickness=0,
            font=en,
            padx=0,
            pady=0,
        )
        widget.pack(fill="both", expand=True)
        widget.tag_configure("en", font=en, spacing3=round(12 * scale))
        widget.tag_configure("ja", font=ja, foreground="#aeb9cc")
        widgets.append(widget)
    columns.rowconfigure(0, weight=1)
    recorded = {
        "platform": sys.platform,
        "dpi": window_dpi(root),
        "en_font": en.actual(),
        "ja_font": ja.actual(),
        "mode": "8 seconds per case, ordered by recorded audio time",
        "original_receive_timing_reproduced": False,
        "frames": [],
        "errors": [],
    }
    root.report_callback_exception = lambda kind, value, tb: recorded["errors"].append(
        kind.__name__
    )
    started = time.monotonic()

    def save():
        recorded["elapsed_sec"] = time.monotonic() - started
        Path(args.output).write_text(
            json.dumps(recorded, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def frame(index):
        if index == len(windows):
            save()
            root.destroy()
            return
        w = windows[index]
        label.configure(text=f"{w['id']} | {w['start_ms'] / 1000:.2f}s | saved responses only")
        item = {"id": w["id"], "elapsed_sec": time.monotonic() - started, "columns": []}
        for widget, run in zip(widgets, (a, b), strict=True):
            result = run["results"][w["id"]]
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            spans = []
            for p in result.get("paragraphs", []):
                start = widget.index("end-1c")
                widget.insert("end", p["ja"] + "\n", "ja")
                middle = widget.index("end-1c")
                widget.insert("end", p["en"] + "\n", "en")
                spans.append((start, middle, widget.index("end-1c")))
            widget.configure(state="disabled")
            root.update_idletasks()
            item["columns"].append(
                {
                    "width": widget.winfo_width(),
                    "height": widget.winfo_height(),
                    "status": result["status"],
                    "lines": [
                        {
                            "ja": widget.count(s, m, "displaylines")[0],
                            "en": widget.count(m, e, "displaylines")[0],
                        }
                        for s, m, e in spans
                    ],
                }
            )
        recorded["frames"].append(item)
        save()
        root.after(round(args.interval * 1000), lambda: frame(index + 1))

    root.after(300, lambda: frame(0))
    root.mainloop()


if __name__ == "__main__":
    main()
