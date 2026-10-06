import argparse
import json
import sys
import time

from .audio import Microphone, list_microphones
from .autosave import TranscriptAutosave
from .realtime_api import RealtimeClient, State


def console(args):
    client = RealtimeClient(
        source_model=args.source_model,
        event_log=args.raw_events,
        source_mode=args.source_mode,
        diarization_enabled=not args.no_diarization,
    )
    autosave = TranscriptAutosave({"openai": client.history})
    print(f"自動保存先: {autosave.directory}", flush=True)
    client.start(args.device, args.noise_reduction)
    seen = 0
    last_state = None
    next_diagnostic = 0.0
    started = time.monotonic()
    try:
        while client.active:
            snapshot = client.snapshot()
            notice = (
                snapshot["state"],
                snapshot["error"],
                snapshot.get("english_error", ""),
                autosave.error,
            )
            if notice != last_state:
                print(
                    f"[{snapshot['state']}] {snapshot['error']} {notice[2]} {notice[3]}", flush=True
                )
                last_state = notice
            records = client.history.records(seen)
            for record in records:
                print(
                    f"{record['language'].upper()} [{record['elapsed_ms']} ms]: {record['delta']}",
                    flush=True,
                )
            seen += len(records)
            if args.diagnostic and time.monotonic() >= next_diagnostic:
                print(json.dumps(snapshot, ensure_ascii=False), flush=True)
                next_diagnostic = time.monotonic() + 1
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        failed = client.state == State.ERROR
        client.stop()
        client.join()
        final_snapshot = client.snapshot()
        failed = failed or bool(final_snapshot["error"] or final_snapshot.get("english_error"))
        print(
            f"[{final_snapshot['state']}] {final_snapshot['error']}",
            file=sys.stderr if failed else sys.stdout,
            flush=True,
        )
        if args.diagnostic:
            print(json.dumps(final_snapshot, ensure_ascii=False), flush=True)
        for record in client.history.records(seen):
            print(f"{record['language'].upper()} [{record['elapsed_ms']} ms]: {record['delta']}")
        client._diarization.close()
        if not autosave.close():
            failed = True
            print(autosave.error or "自動保存の終了待機がタイムアウトしました", file=sys.stderr)
        if args.save:
            client.history.save(args.save)
    return 1 if failed else 0


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="English microphone → EN / JA subtitles")
    parser.add_argument("--console", action="store_true", help="Run the microphone vertical slice")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument(
        "--probe-mic", action="store_true", help="Test microphone without API access"
    )
    parser.add_argument("--device", type=int, help="Input device index; default is OS input")
    parser.add_argument(
        "--noise-reduction", choices=["far_field", "near_field"], default="far_field"
    )
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument(
        "--no-diarization", action="store_true", help="Disable delayed speaker paragraphs"
    )
    parser.add_argument("--source-mode", choices=["separate", "sidecar"], default="separate")
    parser.add_argument(
        "--raw-events", help="Receive-boundary JSONL log (contains transcript text)"
    )
    parser.add_argument(
        "--source-model",
        choices=["gpt-live-transcribe", "gpt-realtime-whisper"],
        default="gpt-live-transcribe",
        help="Source transcription model inside translation session",
    )
    parser.add_argument("--seconds", type=float, help="Stop console mode after this many seconds")
    parser.add_argument("--save", help="Save console transcript history as UTF-8 JSONL")
    args = parser.parse_args()
    if sys.platform != "win32":
        print(
            "Windows側のPythonで起動してください。WSLではunit testのみ実行できます。",
            file=sys.stderr,
        )
        return 1
    if args.probe_mic:
        mic = Microphone(args.device)
        try:
            mic.prepare()
            mic.start()
            until = time.monotonic() + (args.seconds or 10)
            while time.monotonic() < until:
                mic.check_health()
                print(json.dumps(mic.diagnostics(), ensure_ascii=False), flush=True)
                # Consume frames locally; nothing is sent to the network or saved.
                while not mic.frames.empty():
                    mic.frames.get_nowait()
                time.sleep(0.2)
            return 0
        except Exception as exc:
            print(f"マイク診断エラー: {exc}", file=sys.stderr)
            return 1
        finally:
            mic.stop()
    if args.list_devices:
        try:
            devices = list_microphones()
            for device in devices:
                print(device.label)
            if not devices:
                print("入力デバイスがありません。", file=sys.stderr)
                return 1
            return 0
        except Exception as exc:
            print(f"マイク一覧を取得できません: {exc}", file=sys.stderr)
            return 1
    if args.console:
        return console(args)
    from .ui import run_gui

    return run_gui(
        client=RealtimeClient(
            source_model=args.source_model,
            event_log=args.raw_events,
            source_mode=args.source_mode,
            diarization_enabled=not args.no_diarization,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
