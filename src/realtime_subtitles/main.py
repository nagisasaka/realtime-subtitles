import argparse
import json
import sys
import time

from .audio import Microphone, list_microphones
from .autosave import TranscriptAutosave
from .live_client import LiveClient
from .realtime_api import State


def console(args):
    client = LiveClient()
    autosave = TranscriptAutosave({"subtitles": client.history})
    print(f"自動保存先: {autosave.directory}", flush=True)
    cursor = 0
    seen = set()
    partial = None
    next_diagnostic = 0.0
    client.start(args.device, audio_file=args.audio_file)
    started = time.monotonic()

    def print_updates():
        nonlocal cursor, partial
        updates, metadata = client.history.autosave_updates(cursor)
        cursor += len(updates)
        for record in updates:
            if record.get("kind") == "raw_source_segment":
                print(f"EN source #{record['segment_id']}: {record['en_text']}", flush=True)
                continue
            if record.get("kind") != "translation_unit":
                continue  # Raw word metadata is never subtitle text or a translation trigger.
            sequence = record["sequence_id"]
            if sequence not in seen:
                print(
                    f"Translation unit #{sequence} [{record['speaker']} "
                    f"{record['start_ms']}–{record['end_ms']} ms]: {record['en_text']}",
                    flush=True,
                )
                seen.add(sequence)
            if record["translation_status"] == "completed":
                print(f"JA #{sequence}: {record['ja_text']}", flush=True)
        if metadata["en"] and metadata["en"] != partial:
            print(f"EN partial (replacement): {metadata['en']}", flush=True)
        partial = metadata["en"]

    try:
        while client.active:
            print_updates()
            if time.monotonic() >= next_diagnostic:
                snapshot = client.snapshot()
                print(
                    json.dumps(
                        snapshot
                        if args.diagnostic
                        else {
                            "state": snapshot["state"],
                            "error": snapshot["error"],
                            "translation_error": snapshot["translation_error"],
                            "autosave_error": autosave.error,
                            "recording_error": snapshot.get("recording", {}).get("error", ""),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                next_diagnostic = time.monotonic() + 2
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        client.join()
        print_updates()
        saved = autosave.close()
        print(json.dumps(client.snapshot(), ensure_ascii=False), flush=True)
        if not saved:
            print(autosave.error or "自動保存終了待機タイムアウト", file=sys.stderr)
        if args.save:
            client.history.save(args.save, overwrite=False)
    return (
        1
        if client.state == State.ERROR or not saved or client.snapshot()["translation_error"]
        else 0
    )


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
    parser.add_argument("--audio-file", help="Replay PCM16 WAV in real time, without microphone")
    parser.add_argument("--diagnostic", action="store_true")
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

    return run_gui(client=LiveClient(), audio_file=args.audio_file)


if __name__ == "__main__":
    raise SystemExit(main())
