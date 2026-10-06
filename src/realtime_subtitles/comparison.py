"""Opt-in console experiment: one native microphone, independent backend queues."""

import argparse
import contextlib
import json
import os
import queue
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .audio import LatestQueue, Microphone, list_microphones
from .autosave import TranscriptAutosave
from .realtime_api import RealtimeClient
from .speechmatics_api import ENDPOINT, EVENTS, SpeechmaticsClient, SubtitleSegment


class BorrowedMicrophone:
    """Existing OpenAI client interface; device lifetime belongs to Experiment only."""

    def __init__(self, owner):
        self.owner = owner
        self.frames = LatestQueue(3)
        self.enabled = False
        self.error = None

    def prepare(self):
        pass

    def start(self):
        self.enabled = True

    def stop(self):
        self.enabled = False

    def check_health(self):
        self.owner.microphone.check_health()

    def diagnostics(self):
        return {
            **self.owner.microphone.diagnostics(),
            "audio_queue": self.frames.qsize(),
            "dropped_frames": self.frames.dropped,
        }


class ExperimentLog:
    """Optional JSONL event audit, bounded and off the audio/network threads."""

    def __init__(self, path):
        self.queue = LatestQueue(512)
        self.error = ""
        self.stop_requested = threading.Event()
        self.file = Path(path).open("x", encoding="utf-8")
        self.thread = threading.Thread(target=self._write, daemon=False)
        self.thread.start()

    def put(self, record):
        self.queue.put_latest(record)

    def _write(self):
        try:
            while not self.stop_requested.is_set() or not self.queue.empty():
                try:
                    row = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.file.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.file.flush()
        except OSError as exc:
            self.error = type(exc).__name__
        finally:
            self.file.close()

    def close(self):
        self.stop_requested.set()
        self.thread.join(timeout=3)


class Experiment:
    def __init__(
        self,
        *,
        compare=False,
        endpoint=ENDPOINT,
        max_delay=4,
        log=None,
        microphone_factory=Microphone,
    ):
        self.compare, self.log = compare, log
        self.notifications = LatestQueue(256)
        self.speechmatics = SpeechmaticsClient(
            endpoint=endpoint, max_delay=max_delay, on_event=self._speechmatics_event
        )
        self.microphone_factory = microphone_factory
        self.microphone = None
        self.borrowed = None
        # Only dependency injection. No alteration to the baseline OpenAI implementation.
        self.openai = RealtimeClient(microphone_factory=self._borrow) if compare else None
        self.stop_requested = threading.Event()
        self.thread = None
        self.error = ""
        self.state = "STOPPED"
        self.openai_cursor = 0
        self.frames_dispatched = 0
        self.autosave = None

    def ensure_autosave(self):
        if self.autosave is None:
            histories = {"speechmatics": self.speechmatics.history}
            if self.openai:
                histories["openai"] = self.openai.history
            self.autosave = TranscriptAutosave(histories)
        return self.autosave

    def _borrow(self, device=None):
        self.borrowed = BorrowedMicrophone(self)
        return self.borrowed

    def _publish(self, record):
        record = {
            "received_at": datetime.now(UTC).isoformat(),
            "received_monotonic_ms": time.monotonic_ns() // 1_000_000,
            **record,
        }
        self.notifications.put_latest(record)
        if self.log:
            self.log.put(record)

    def _speechmatics_event(self, event, segments):
        if event.get("message") in EVENTS:
            self._publish(
                {
                    "backend": "speechmatics",
                    "kind": "subtitle_event",
                    "raw_server_event": event,
                    "segments": [asdict(s) for s in segments],
                }
            )

    def _openai_events(self):
        if not self.openai:
            return
        records = self.openai.history.records(self.openai_cursor)
        self.openai_cursor += len(records)
        for row in records:
            segment = SubtitleSegment(
                "openai",
                row["language"],
                row["delta"],
                True,
                session_id=row.get("session_id"),
                received_monotonic_ms=row.get("received_monotonic_ms"),
            )
            # OpenAI deltas have no segment start/end or direct speaker identity.
            self._publish(
                {
                    "backend": "openai",
                    "kind": "append_only_delta",
                    "segments": [asdict(segment)],
                    "original_record": row,
                }
            )

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, device=None, noise_reduction="far_field"):
        if self.active:
            return False
        if not os.environ.get("SPEECHMATICS_API_KEY", "").strip():
            self.state, self.error = "ERROR", "SPEECHMATICS_API_KEY が未設定です。"
            return False
        if self.compare and not os.environ.get("OPENAI_API_KEY", "").strip():
            self.state, self.error = "ERROR", "OPENAI_API_KEY が未設定です。"
            return False
        self.state, self.error = "CONNECTING", ""
        self.stop_requested.clear()
        self.thread = threading.Thread(
            target=self._run, args=(device, noise_reduction), daemon=False
        )
        self.thread.start()
        return True

    def dispatch(self, item):
        # Both destinations receive exactly the same immutable bytes object.
        if self.speechmatics.active:
            self.speechmatics.frames.put_latest(item)
        borrowed = self.borrowed
        if borrowed and borrowed.enabled:
            borrowed.frames.put_latest(item)
        self.frames_dispatched += 1

    def _run(self, device, noise):
        try:
            self.microphone = self.microphone_factory(device)
            self.microphone.prepare()
            if self.stop_requested.is_set():
                return
            if not self.speechmatics.start():
                raise RuntimeError(self.speechmatics.error)
            if self.stop_requested.is_set():
                return
            if self.openai:
                self.openai.start(device, noise)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self.stop_requested.is_set():
                ready = self.speechmatics.session_ready
                if self.openai:
                    ready = ready and self.openai.state == "RUNNING"
                if ready or self.speechmatics.state == "ERROR":
                    break
                time.sleep(0.01)
            if self.stop_requested.is_set():
                return
            if not self.compare and self.speechmatics.state == "ERROR":
                self.state, self.error = "ERROR", self.speechmatics.error
                return
            self.microphone.start()
            self.state = "RUNNING"
            self._publish(
                {
                    "kind": "run_start",
                    "compare": self.compare,
                    "speechmatics": {
                        "endpoint": self.speechmatics.endpoint,
                        "model": "enhanced",
                        "max_delay": self.speechmatics.max_delay,
                    },
                    "audio": self.microphone.diagnostics(),
                }
            )
            while not self.stop_requested.is_set():
                self.microphone.check_health()
                try:
                    self.dispatch(self.microphone.frames.get(timeout=0.05))
                except queue.Empty:
                    pass
                self._openai_events()
                if not self.compare and self.speechmatics.state == "ERROR":
                    self.error = self.speechmatics.error
                    self.state = "ERROR"
                    break
        except Exception as exc:
            self.error = f"Microphone/experiment: {type(exc).__name__}"
            self.state = "ERROR"
        finally:
            if self.microphone:
                with contextlib.suppress(Exception):
                    self.microphone.stop()
                # Deliver the converter's final padded chunk before requesting EOS.
                while not self.microphone.frames.empty():
                    self.dispatch(self.microphone.frames.get_nowait())
                deadline = time.monotonic() + 1
                while self.speechmatics.active and not self.speechmatics.frames.empty():
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            self.speechmatics.stop()
            if self.openai:
                self.openai.stop()
                self.openai.join(15)
                self._openai_events()
            self.speechmatics.join(12)
            if self.state != "ERROR":
                self.state = "STOPPED"
            self._publish({"kind": "run_end", "diagnostics": self.snapshot()})

    def stop(self):
        self.stop_requested.set()
        connecting = self.state == "CONNECTING"
        if self.active:
            self.state = "STOPPING"
        # Interrupt startup/backoff; all calls only signal workers.
        if connecting:
            self.speechmatics.stop()
            if self.openai:
                self.openai.stop()

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return not self.active

    def close(self):
        self.stop()
        self.join(20)
        if self.openai:
            self.openai._diarization.close()
        if self.autosave:
            self.autosave.close()

    def snapshot(self):
        return {
            "autosave": self.autosave.snapshot() if self.autosave else None,
            "state": self.state,
            "error": self.error,
            "speechmatics": self.speechmatics.snapshot(),
            "openai": self.openai.snapshot() if self.openai else None,
            "audio": self.microphone.diagnostics() if self.microphone else {},
            "frames_dispatched": self.frames_dispatched,
            "log_dropped": self.log.queue.dropped if self.log else 0,
            "log_error": self.log.error if self.log else "",
        }


def print_events(experiment):
    while True:
        try:
            event = experiment.notifications.get_nowait()
        except queue.Empty:
            return
        for segment in event.get("segments", []):
            status = "final" if segment["is_final"] else "partial/replacement"
            print(
                f"[{segment['backend']} {segment['language']} {status} "
                f"{segment['start_ms']}–{segment['end_ms']} ms "
                f"{segment['speaker'] or '?'}] {segment['text']}",
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(
        description="Speechmatics experiment (baseline remains default)"
    )
    parser.add_argument(
        "--compare", action="store_true", help="Same microphone PCM to both providers"
    )
    parser.add_argument("--gui", action="store_true", help="Experimental comparison window")
    parser.add_argument("--device", type=int)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--max-delay", type=float, default=4, choices=[0.7, 1, 2, 3, 4])
    parser.add_argument(
        "--event-log", help="Opt-in UTF-8 JSONL, contains transcript text; no audio"
    )
    parser.add_argument("--save", help="Save Speechmatics finals to a new JSONL on exit")
    args = parser.parse_args()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    if args.list_devices:
        for device in list_microphones():
            print(device.label)
        return 0
    log = ExperimentLog(args.event_log) if args.event_log else None
    experiment = Experiment(
        compare=args.compare, endpoint=args.endpoint, max_delay=args.max_delay, log=log
    )
    try:
        if args.gui:
            from .comparison_ui import run_gui

            run_gui(experiment, args.device)
        else:
            experiment.ensure_autosave()
            print(f"自動保存先: {experiment.autosave.directory}", flush=True)
            experiment.start(args.device)
            deadline = time.monotonic() + args.seconds if args.seconds else float("inf")
            last = None
            while experiment.active and time.monotonic() < deadline:
                print_events(experiment)
                snapshot = experiment.snapshot()
                states = (
                    snapshot["state"],
                    snapshot["speechmatics"]["state"],
                    snapshot["speechmatics"]["error"],
                    snapshot["speechmatics"]["warning"],
                    experiment.autosave.error,
                )
                if states != last:
                    print(json.dumps(snapshot, ensure_ascii=False), flush=True)
                    last = states
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        experiment.close()
        if not args.gui:
            print_events(experiment)
            print(json.dumps(experiment.snapshot(), ensure_ascii=False), flush=True)
        if args.save:
            experiment.speechmatics.history.save(args.save)
        if log:
            log.close()
    return (
        1
        if (
            experiment.error
            or experiment.speechmatics.error
            or (experiment.autosave and experiment.autosave.error)
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
