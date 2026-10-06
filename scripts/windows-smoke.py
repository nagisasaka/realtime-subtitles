"""Explicit native microphone check; no external API and no audio recording."""

import json
import queue
import sys
import time

from realtime_subtitles.audio import FRAME_BYTES, Microphone, list_microphones


def main():
    if sys.platform != "win32":
        raise SystemExit("Run this script with Windows Python.")
    sys.stdout.reconfigure(encoding="utf-8")
    devices = list_microphones()
    default = next((d for d in devices if d.is_default), None)
    if default is None:
        raise SystemExit("No default Windows input microphone.")
    print(f"Default: {default.label}", flush=True)
    for run in range(2):
        mic = Microphone()
        levels = []
        frame_count = 0
        try:
            mic.prepare()
            mic.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                mic.check_health()
                levels.append(mic.dbfs)
                try:
                    _, frame = mic.frames.get(timeout=0.1)
                    assert len(frame) == FRAME_BYTES
                    frame_count += 1
                except queue.Empty:
                    pass
        finally:
            mic.stop()
        assert frame_count >= 10, f"Only {frame_count} frames captured"
        assert max(levels) > -120, "Input remains completely silent"
        assert not mic.worker and not mic.stream
        print(
            json.dumps(
                {
                    "run": run + 1,
                    "frames": frame_count,
                    "min_dbfs": min(levels),
                    "max_dbfs": max(levels),
                    **mic.info,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
