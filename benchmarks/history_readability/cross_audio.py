"""Freeze a held-out, cross-domain check of an already chosen prompt.

This reads existing Linden audio/log pairs. It makes no API calls, opens no audio
device, and never re-runs the Enhanced comparison that produced the legacy log.
"""

import argparse
import hashlib
import json
import subprocess
import wave
from pathlib import Path

from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory

from .naturalness import variant_config
from .prepare import (
    digest,
    file_hash,
    read_json,
    replay,
    select,
    windows_for_histories,
    write_json,
)

ANCHORS = [
    ["technical-talk", 30, "holdout", "技術講演・セキュリティと製品名"],
    ["technical-talk", 72, "holdout", "技術講演・構成要素と指示語"],
    ["technical-talk", 224, "holdout", "技術講演・細切れの原因説明"],
    ["MTG_30884", 61, "holdout", "会議・TransientNoise=high/TalkNearWhiteboard"],
    ["MTG_30861", 49, "holdout", "会議・TalkNearWhiteboard"],
    ["MTG_30862", 84, "holdout", "会議・DebateOverlaps"],
]


def audio_info(path, expected_sha):
    path = Path(path)
    with path.open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    if sha != expected_sha:
        raise ValueError("Audio does not match the saved ASR run")
    with wave.open(str(path)) as source:
        return {
            "path": str(path.resolve()),
            "sha256": sha,
            "sample_rate": source.getframerate(),
            "channels": source.getnchannels(),
            "sample_width": source.getsampwidth(),
            "frames": source.getnframes(),
            "duration_seconds": source.getnframes() / source.getframerate(),
        }


def legacy_windows(events, report):
    """Legacy log lacks receive clocks/EOS: use order, disclose local flush.

    The production assembler pairs finals without a timer, so a constant local
    test clock suffices. Never expose that clock as an observed network timestamp.
    Audio start/end remain the original server metadata.
    """
    backend = report["backends"]["agent"]
    status = backend["status"]
    if (
        status["model"] != "linden-1"
        or status["state"] != "STOPPED"
        or status["error"]
        or status["dropped_frames"]
        or not status["session_id"]
    ):
        raise ValueError("Legacy recording must have a successful completed Agent run")
    finals = sum(e.get("message") == "AddSegment" for e in events)
    if finals != backend["finals"]:
        raise ValueError("Final segment count differs from completed-run report")
    history = TranslationHistory(clock=lambda: 0.0)

    def emit(sources, reason, ms):
        unit = history.emit_unit(sources, reason, ms)
        history.reconstructions.plan(unit)

    assembler = TranslationUnitAssembler(emit, clock=lambda: 0.0)
    session = status["session_id"]
    for event in events:
        if event.get("message") == "AddPartialSegment":
            assembler.note_speaker((event.get("segment") or {}).get("speaker"), session)
        elif event.get("message") == "AddSegment":
            source = history.record_segment(event, session)
            if source:
                assembler.accept(source)
    assembler.flush("local_completed_log_end")
    windows = windows_for_histories({("technical-talk", session): history})
    for window in windows:
        window["receive_timing"] = "unavailable; order-only local replay, no latency estimate"
        window["eos"] = "not logged; local flush after completed-run report verification"
        for source in window["raw_sources"]:
            source["received_monotonic_ms"] = None
            source["received_at"] = None
    return windows


def freeze(args):
    if (args.output / "manifest.json").exists():
        raise FileExistsError("Frozen cross-audio manifest already exists")
    parent = read_json(args.parent / "manifest.json")
    candidate = variant_config(parent, "reverse_cohesion", args.parent / "reverse_cohesion.json")
    used = len(read_json(args.parent / "ledger.json")["attempts"])
    limit = min(48, parent["request_limit"] - used)
    if limit < 30:
        raise ValueError("Insufficient remaining explicit API budget")
    tech_report = read_json(args.technical_logs / "report.json")
    tech_rows = [
        json.loads(line)
        for line in (args.technical_logs / "agent-raw.jsonl").open(encoding="utf-8")
    ]
    windows = legacy_windows(tech_rows, tech_report)
    meeting_manifest = read_json(args.meeting_logs / "manifest.json")
    windows += replay(
        json.loads(line) for line in (args.meeting_logs / "events.jsonl").open(encoding="utf-8")
    )
    selected = select(windows, ANCHORS)
    recordings = [
        dict(
            audio_info(args.technical_audio, tech_report["audio_sha256"]),
            meeting_id="technical-talk",
            provenance="Local 6-minute recording; agent report binds WAV SHA to ASR log",
        )
    ]
    meeting_ids = {w["meeting_id"] for w in selected}
    for meeting in meeting_manifest["meetings"]:
        if meeting["meeting_id"] in meeting_ids:
            recordings.append(
                dict(
                    audio_info(
                        args.meeting_audio / meeting["original_audio"],
                        meeting["original_audio_sha256"],
                    ),
                    meeting_id=meeting["meeting_id"],
                    metadata=meeting["metadata"],
                    device=meeting["device"],
                )
            )
    sources = [
        args.technical_logs / "report.json",
        args.technical_logs / "agent-raw.jsonl",
        args.meeting_logs / "manifest.json",
        args.meeting_logs / "events.jsonl",
        args.meeting_logs / "transcript.jsonl",
    ]
    manifest = {
        "start_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dataset": "Local technical talk + NOTSOFAR-1 distant meetings",
        "dataset_revision": meeting_manifest["dataset_revision"],
        "dataset_version": meeting_manifest["dataset_version"],
        "source": "/",
        "source_hashes": {str(p.resolve()): file_hash(p) for p in sources},
        "recordings": recordings,
        "parent_manifest_hash": digest(parent),
        "parent_attempts_at_freeze": used,
        "request_limit": limit,
        "aggregate_request_ceiling": used + limit,
        "windows": selected,
        "selection": ANCHORS,
        "coverage_ms": sum(w["end_ms"] - w["start_ms"] for w in selected),
        "variants": {"baseline": parent["variants"]["baseline"], "reverse_cohesion": candidate},
        "judge_prompt": parent["judge_prompt"],
        "generator": parent["generator"],
        "judge": parent["judge"],
        "max_iteration_sec": 480,
        "selection_policy": "Fixed inputs before generation; no prompt retuning on these outputs",
        "mode": "Saved original Linden ASR -> current paired-unit/planner -> split/translate",
        "limitations": [
            "No new ASR/audio replay: assesses reconstruction conditional on saved recognition",
            "Meeting hashtags describe whole recordings, not verified noise in each excerpt",
            "Technical log lacks network receive timing and raw EOS; no receive latency claim",
            "ASR speaker/word errors remain authoritative; no correction from GT or recordings",
        ],
    }
    write_json(args.output / "manifest.json", manifest)
    print(f"Frozen {len(selected)} windows / {manifest['coverage_ms'] / 1000:.2f}s / limit {limit}")
    for window in selected:
        print(window["id"], window["start_ms"], window["end_ms"], window["english"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "parent",
        "output",
        "technical-audio",
        "technical-logs",
        "meeting-audio",
        "meeting-logs",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    freeze(parser.parse_args())


if __name__ == "__main__":
    main()
