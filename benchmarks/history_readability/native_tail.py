"""Replay recorded successful history revisions through the native UI without APIs."""

import argparse
import json
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

from realtime_subtitles.ui import SubtitleApp, enable_dpi_awareness

from .native_preview import PreviewClient
from .prepare import read_json, write_json
from .tail_review import add_saved_unit, apply_result, seed, source_histories


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--tail", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--variant", default="reverse_cohesion")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("Use native Windows Python")
    manifest = read_json(args.manifest)
    window = next(w for w in manifest["windows"] if w["id"] == args.id)
    initial = read_json(args.baseline)["results"][args.id]
    trial = read_json(args.tail)["cases"][args.id][args.variant]
    if trial["status"] != "valid":
        raise ValueError("Cannot treat an incomplete API trial as successful replay")
    histories = source_histories(json.loads(row) for row in args.events.open(encoding="utf-8"))
    units = histories[window["session_id"]].segments()
    enable_dpi_awareness()
    report = {
        "case_id": args.id,
        "variant": args.variant,
        "platform": sys.platform,
        "audio_or_api_started": False,
        "original_event_pacing": False,
        "frames": [],
    }
    with tempfile.TemporaryDirectory(prefix="subtitle-tail-") as directory:
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda kind, value, tb: errors.append(kind.__name__)
        app = SubtitleApp(
            root,
            client=PreviewClient(),
            settings_file=Path(directory) / "settings.json",
            device_loader=lambda: [],
        )
        root.title("履歴更新の検証 · 保存済み応答のみ")
        root.attributes("-alpha", 1.0)
        widget = app.history_text

        def pump(seconds=0.2):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                root.update()
                time.sleep(0.005)

        def layout():
            return (
                root.winfo_height(),
                app.live_text.winfo_y(),
                app.live_text.winfo_height(),
                app.ja_text.winfo_y(),
            )

        def line():
            return widget.get("@0,0", "@0,0 lineend")

        def capture(stage, reading_before=None):
            blocks = app._history_blocks
            visual = sorted(
                blocks,
                key=lambda b: tuple(
                    int(n) for n in widget.index(f"block_{b.key}_start").split(".")
                ),
            )
            expected = list(reversed(app.client.history.reconstructions.effective_blocks()))
            assert [b.key for b in visual] == [b.key for b in expected]
            assert all(
                widget.compare(f"block_{b.key}_ja", "<", f"block_{b.key}_en") for b in visual
            )
            assert layout() == fixed_layout
            assert app.history_text is widget
            assert not app.client.active and not errors
            current_line = line()
            if reading_before is not None:
                assert current_line == reading_before
            report["frames"].append(
                {
                    "stage": stage,
                    "newest_first": True,
                    "ja_above_en": True,
                    "same_widget": True,
                    "fixed_live_layout": True,
                    "reader_anchor_preserved": reading_before is None
                    or current_line == reading_before,
                    "block_ranges": [[b.start, b.end] for b in visual],
                    "reading_line": current_line,
                    "block_count": len(visual),
                }
            )
            write_json(args.output, report)

        try:
            h = app.client.history
            for unit in units[: window["unit_ids"][-1] + 1]:
                add_saved_unit(h, unit)
            h.set_partial("Recorded revision replay", window["speaker"])
            seed(h, window, initial)
            pump(0.6)
            fixed_layout = layout()
            report.update(dpi=app.scale * 96, layout=fixed_layout)
            capture("initial")
            # Read a paragraph well outside the tail that will be rewritten.
            anchor = app._history_blocks[2]
            widget.yview(f"block_{anchor.key}_en")
            pump()
            held_line = line()
            for index, step in enumerate(trial["steps"]):
                identity = step["revision"]["unit_ids"][-1]
                unit = add_saved_unit(h, units[identity])
                h.set_partial("Recorded revision replay", window["speaker"])
                target = h.reconstructions.plan(unit)
                target = h.reconstructions.prepare(target.unit_id)
                assert target.en_text == step["input"]["english"]
                assert h.reconstructions.context_for(target.unit_id) == step["input"]["context"]
                pump()
                capture(f"{index + 1}: raw unit", held_line)
                apply_result(h, target, step["result"])
                pump()
                capture(f"{index + 1}: translated revision", held_line)
            widget.yview_moveto(0)
            pump()
            capture("back to newest")
            assert [
                b.en_text
                for b in h.reconstructions.effective_blocks()
                if b.start[0] >= window["unit_ids"][0]
            ] == [b["en_text"] for b in trial["blocks"]]
            report.update(status="passed", callback_errors=errors)
            write_json(args.output, report)
        finally:
            app.close()
            try:
                pump()
            except tk.TclError:
                pass
    print("Native sequential replay passed:", len(report["frames"]), "frames")


if __name__ == "__main__":
    main()
