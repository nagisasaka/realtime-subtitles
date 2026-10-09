"""Render saved candidate paragraphs through the actual Windows app, without API/audio."""

import argparse
import ctypes
import json
import os
import struct
import sys
import tempfile
import time
import tkinter as tk
import zlib
from ctypes import wintypes
from pathlib import Path

import numpy as np

from realtime_subtitles.live_client import LiveClient
from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

from .prepare import read_json, write_json
from .tail_review import add_saved_unit, seed, source_histories


class PreviewClient(LiveClient):
    def start(self, *args, **kwargs):
        raise RuntimeError("Saved-output preview cannot start an audio or API session")


def screenshot(hwnd, path):
    """Capture only this preview's window, never the user's desktop or other apps."""
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetWindowDC.argtypes = [wintypes.HWND]
    user.GetWindowDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    rect = wintypes.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("Cannot inspect preview window")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    dc = user.GetWindowDC(hwnd)
    memory = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    previous = gdi.SelectObject(memory, bitmap)
    try:
        if not user.PrintWindow(hwnd, memory, 2):
            raise RuntimeError("Cannot capture preview window")
        header = ctypes.create_string_buffer(
            struct.pack(
                "<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0
            )
        )
        pixels = ctypes.create_string_buffer(width * height * 4)
        if not gdi.GetDIBits(memory, bitmap, 0, height, pixels, header, 0):
            raise RuntimeError("Cannot read preview pixels")
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 4)[:, :, [2, 1, 0]]

        def chunk(name, data):
            return (
                struct.pack(">I", len(data))
                + name
                + data
                + struct.pack(">I", zlib.crc32(name + data))
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + row.tobytes() for row in rgb)))
            + chunk(b"IEND", b"")
        )
        path.write_bytes(png)
    finally:
        gdi.SelectObject(memory, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, dc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("Use native Windows Python")
    manifest, result = read_json(args.manifest), read_json(args.run)["results"][args.id]
    window = next(w for w in manifest["windows"] if w["id"] == args.id)
    histories = source_histories(json.loads(row) for row in args.events.open(encoding="utf-8"))
    units = histories[window["session_id"]].segments()
    enable_dpi_awareness()
    with tempfile.TemporaryDirectory(prefix="subtitle-readability-") as directory:
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda kind, value, tb: errors.append(kind.__name__)
        app = SubtitleApp(
            root,
            client=PreviewClient(),
            settings_file=Path(directory) / "settings.json",
            device_loader=lambda: [],
        )
        root.title("字幕履歴の比較確認 · 保存済み結果のみ")
        root.attributes("-alpha", 1.0)
        root.geometry("+70+100")
        # Let the app finish its native initial-monitor DPI restoration first.
        root.after(
            300, lambda: app._resize_for_dpi(round(args.width * app.scale), round(560 * app.scale))
        )
        app.start_button.configure(state="disabled")
        h = app.client.history
        for unit in units[: window["unit_ids"][-1] + 1]:
            add_saved_unit(h, unit)
        h.set_partial("Saved-output preview — no microphone or API", window["speaker"])
        widget = app.history_text
        widget_id = str(widget)
        seed(h, window, result)
        started = time.monotonic()

        def capture():
            root.update_idletasks()
            paragraphs = result["paragraphs"]
            contents = widget.get("1.0", "end")
            # Verify rendered order from the actual source-range marks, not text search.
            blocks = [b for b in app._history_blocks if b.revision_id == 0]
            display = sorted(
                blocks,
                key=lambda b: tuple(
                    int(n) for n in widget.index(f"block_{b.key}_start").split(".")
                ),
            )
            expected = list(reversed([p["en"] for p in paragraphs]))
            displayed = [b.en_text for b in display]
            if displayed != expected:
                raise AssertionError("Native UI is not newest first")
            if abs(root.winfo_width() - round(args.width * app.scale)) > 2:
                raise AssertionError("Requested preview width was not applied")
            user = ctypes.windll.user32
            user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user.GetAncestor.restype = ctypes.c_void_p
            data = {
                "platform": sys.platform,
                "case_id": args.id,
                "source": str(args.run),
                "pid": os.getpid(),
                "hwnd": user.GetAncestor(root.winfo_id(), 2),
                "dpi": app.scale * 96,
                "width": widget.winfo_width(),
                "height": widget.winfo_height(),
                "root_height": root.winfo_height(),
                "requested_logical_width": args.width,
                "same_widget": str(app.history_text) == widget_id,
                "newest_first": displayed == expected,
                "api_or_audio_started": False,
                "live_y": app.live_text.winfo_y(),
                "errors": errors,
                "elapsed_sec": time.monotonic() - started,
                "paragraphs": [],
            }
            for b in display:
                ja, en, end = [f"block_{b.key}_{suffix}" for suffix in ("ja", "en", "end")]
                data["paragraphs"].append(
                    {
                        "en": b.en_text,
                        "ja": b.ja_text,
                        "ja_above_en": widget.compare(ja, "<", en),
                        "ja_lines": widget.count(ja, en, "displaylines")[0],
                        "en_lines": widget.count(en, end, "displaylines")[0],
                    }
                )
            if any(p["ja"] not in contents for p in paragraphs):
                raise AssertionError("Translation not rendered")
            write_json(args.output, data)
            if args.screenshot:
                screenshot(data["hwnd"], args.screenshot)

        root.after(900, capture)
        root.after(round(args.seconds * 1000), app.close)
        root.mainloop()
        if errors:
            raise RuntimeError("Native UI callback failed: " + ",".join(errors))


if __name__ == "__main__":
    main()
