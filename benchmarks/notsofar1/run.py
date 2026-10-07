"""Real-time file replay into the app's Agent STT SDK factory. No mic or translation."""

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import subprocess
import time
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from realtime_subtitles.agent_stt import ENDPOINT, AgentSttClient
from realtime_subtitles.speechmatics_api import exception_code

from .common import DATASET, MAX_SECONDS, NORMALIZATION, ROOT, SCORING, SPLIT, load, save, sha256
from .prepare import wav_info

EVENTS = (
    "AddPartialSegment",
    "AddSegment",
    "StartOfTurn",
    "EndOfTurn",
    "SpeechStarted",
    "SpeechEnded",
    "EndOfTranscript",
    "AudioAdded",
    "RecognitionStarted",
)


def ms(value):
    return (
        round(value * 1000)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


class Recorder:
    def __init__(self, output, meeting, clock=time.monotonic):
        self.meeting, self.clock = meeting, clock
        self.session_id = None
        self.epoch = None
        self.ended = asyncio.Event()
        self.error = None
        self.counts = Counter()
        self.final_count = 0
        self.events = (output / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        self.transcript = (output / "transcript.jsonl").open("a", encoding="utf-8", buffering=1)

    def receive(self, event):
        kind = event.get("message")
        if kind == "Error":
            self.error = "server_error"  # Never serialize arbitrary error/header echoes.
            return
        if kind not in EVENTS:
            return
        self.counts[kind] += 1
        if kind == "EndOfTranscript":
            self.ended.set()
        segment = event.get("segment") or {}
        metadata = event.get("metadata") or {}
        # Segment metadata is authoritative; only fall back to actual supplied metadata.
        start = segment.get("start_time", metadata.get("start_time"))
        end = segment.get("end_time", metadata.get("end_time"))
        now = self.clock()
        row = {
            "meeting_id": self.meeting,
            "session_id": self.session_id,
            "event_type": kind,
            "audio_start_ms": ms(start),
            "audio_end_ms": ms(end),
            "received_monotonic_ms": now * 1000,
            "playback_elapsed_ms": (now - self.epoch) * 1000 if self.epoch is not None else None,
            "speaker": segment.get("speaker_id", segment.get("speaker")),
            "transcript": segment.get("transcript"),
            "raw_event": event,
        }
        self.events.write(json.dumps(row, ensure_ascii=False) + "\n")
        if kind == "AddSegment":
            self.final_count += 1
            self.transcript.write(json.dumps(row, ensure_ascii=False) + "\n")

    def close(self):
        self.events.close()
        self.transcript.close()


async def pace(epoch, audio_end, clock=time.monotonic, sleep=asyncio.sleep):
    # Chunk becomes available at its END, like a microphone. Absolute deadlines avoid drift.
    # Late chunks catch up, but no future audio may be sent before its audio-end deadline.
    deadline = epoch + audio_end
    while clock() < deadline:
        await sleep(deadline - clock())
    return clock() - deadline


async def stream(
    client,
    audio_path,
    recorder,
    send_log,
    *,
    clock=time.monotonic,
    sleep=asyncio.sleep,
    eos_timeout=20,
):
    recorder.epoch = clock()
    samples = sent = 0
    with wave.open(str(audio_path), "rb") as audio:
        while chunk := audio.readframes(3200):
            start = samples / 16000
            samples += len(chunk) // 2
            end = samples / 16000
            lateness = await pace(recorder.epoch, end, clock, sleep)
            if recorder.error or recorder.ended.is_set() or not client.is_ready_for_audio:
                raise ConnectionError("ASR session interrupted")
            before = clock()
            async with asyncio.timeout(5):
                await client.send_audio(chunk)
            if not client.is_ready_for_audio:
                raise ConnectionError("Audio gate closed")
            sent += 1
            send_log.write(
                json.dumps(
                    {
                        "meeting_id": recorder.meeting,
                        "session_id": recorder.session_id,
                        "seq_no": sent,
                        "audio_start_ms": start * 1000,
                        "audio_end_ms": end * 1000,
                        "send_monotonic_ms": before * 1000,
                        "send_complete_monotonic_ms": clock() * 1000,
                        "playback_start_monotonic_ms": recorder.epoch * 1000,
                        "late_ms": lateness * 1000,
                    }
                )
                + "\n"
            )
    # Actual binary frame count, not the SDK counter overwritten by acknowledgements.
    async with asyncio.timeout(eos_timeout):
        await client.send_message({"message": "EndOfStream", "last_seq_no": sent})
        while not recorder.ended.is_set():
            if recorder.error or not client.is_ready_for_audio:
                raise ConnectionError("Session failed during EOS")
            await sleep(0.01)
    if recorder.error:
        raise ConnectionError("Server error during EOS")
    return {
        "sent_frames": sent,
        "sent_audio_seconds": samples / 16000,
        "playback_start_monotonic_ms": recorder.epoch * 1000,
        "eos_received": True,
    }


def check_sdk_config(client):
    actual = {
        "transcription": client._transcription_config.to_dict(),
        "audio": client._audio_format.to_dict(),
        "turn": client._turn_config.to_dict(),
    }
    expected = {
        "language": "en",
        "model": "linden-1",
        "enable_partials": True,
        "diarization": "speaker",
        "emit_sentences": True,
    }
    if any(actual["transcription"].get(k) != v for k, v in expected.items()):
        raise ValueError("Production Agent STT configuration changed; review benchmark first")
    if actual["audio"] != {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}:
        raise ValueError("Production SDK audio format changed")
    return actual


async def run(root, output, meeting_ids):
    key = os.environ.get("SPEECHMATICS_API_KEY", "").strip()
    if not key:
        raise ValueError("SPEECHMATICS_API_KEY is not set")
    prepared = load(root / "prepared.json")
    meetings = [m for m in prepared["meetings"] if m["meeting_id"] in meeting_ids]
    if len(meetings) != len(set(meeting_ids)) or len(meeting_ids) != len(set(meeting_ids)):
        raise ValueError("Unknown or duplicate meeting ID")
    for m in meetings:
        path = root / m["prepared_audio"]
        if sha256(path) != m["prepared_audio_sha256"]:
            raise ValueError("Prepared WAV checksum mismatch")
        info = wav_info(path)
        if (info["sample_rate"], info["channels"], info["sample_width"]) != (16000, 1, 2):
            raise ValueError("WAV must be 16kHz mono PCM16")
        if info != m["prepared_format"]:
            raise ValueError("WAV manifest mismatch")
    duration = sum(m["prepared_format"]["duration"] for m in meetings)
    output.mkdir(parents=True, exist_ok=True)
    manifest_file = output / "manifest.json"
    if manifest_file.exists():
        manifest = load(manifest_file)
        if manifest["dataset_revision"] != prepared["revision"]:
            raise ValueError("Dataset revision mismatch")
    else:
        manifest = {
            "dataset_name": DATASET,
            "dataset_version": SPLIT,
            "dataset_revision": prepared["revision"],
            "meetings": prepared["meetings"],
            "configuration": {
                "endpoint": ENDPOINT,
                "model": "linden-1",
                "language": "en",
                "enable_partials": True,
                "diarization": "speaker",
                "emit_sentences": True,
                "sample_rate": 16000,
                "encoding": "pcm_s16le",
                "chunk_ms": 200,
            },
            "sdk_version": importlib.metadata.version("speechmatics-agent-stt"),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": {
                    name: importlib.metadata.version(name)
                    for name in ("speechmatics-rt", "numpy", "soxr")
                },
            },
            "app_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip(),
            "normalization": NORMALIZATION,
            "scoring": SCORING,
            "execution_date": datetime.now(UTC).isoformat(),
            "attempts": [],
            "reserved_audio_seconds": 0,
        }
    used = {r["meeting_id"] for r in manifest["attempts"]}
    if used.intersection(meeting_ids):
        raise ValueError("Meeting already attempted; no automatic replay/retry")
    if (
        duration + manifest["reserved_audio_seconds"] > MAX_SECONDS
        or len(used | set(meeting_ids)) > 5
    ):
        raise ValueError("Run budget exceeded (30 minutes / 5 meetings)")
    # Reserve whole selected audio before connecting: failed attempts still consume the cap.
    manifest["reserved_audio_seconds"] += duration
    for m in meetings:
        manifest["attempts"].append({"meeting_id": m["meeting_id"], "status": "reserved"})
    save(manifest_file, manifest)
    logging.getLogger("speechmatics").setLevel(logging.CRITICAL)
    for m in meetings:
        mid = m["meeting_id"]
        attempt = next(r for r in manifest["attempts"] if r["meeting_id"] == mid)
        attempt["status"] = "running"
        save(manifest_file, manifest)
        recorder = Recorder(output, mid)
        # Reuse exact production SDK factory/config. Send already-16k audio directly:
        # no live app queue, reconnect/drop policy, capture, translation or resampling.
        client = None
        try:
            client = AgentSttClient()._new_sdk(key)
            attempt["actual_sdk_config"] = check_sdk_config(client)
            for kind in (*EVENTS, "Error"):
                client.on(kind, recorder.receive)
            async with asyncio.timeout(20):
                await client.connect()
            recorder.session_id = client.session_id
            print("Streaming", mid, m["prepared_format"]["duration"], "seconds", flush=True)
            with (output / "chunks.jsonl").open("a", encoding="utf-8", buffering=1) as sends:
                stats = await stream(client, root / m["prepared_audio"], recorder, sends)
            if not recorder.final_count:
                raise RuntimeError("No final AddSegment received")
            attempt.update(status="completed", **stats)
        except asyncio.CancelledError:
            attempt.update(status="interrupted", error="CancelledError")
            raise
        except Exception as exc:
            attempt.update(status="failed", error=exception_code(exc))
            raise
        finally:
            attempt.update(session_id=recorder.session_id, event_counts=dict(recorder.counts))
            save(manifest_file, manifest)
            if client is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(3):
                        await client.close()
            recorder.close()
        print("Completed", mid, recorder.final_count, "final segments", flush=True)


@contextlib.contextmanager
def exclusive_run(output):
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".run.lock"
    # Prevent concurrent writers and duplicate reservations in the same run.
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(
            "Run locked; inspect existing process before removing stale lock"
        ) from None
    try:
        with os.fdopen(fd, "w") as file:
            file.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--meetings", nargs="+", required=True)
    args = parser.parse_args()
    try:
        with exclusive_run(args.output):
            asyncio.run(run(args.root, args.output, args.meetings))
    except Exception as exc:
        print("Benchmark stopped:", exception_code(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
