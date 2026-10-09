"""Replay cached Agent events through the production assembler/window planner."""

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from realtime_subtitles.history_reconstruction import (
    BATCH_TRANSLATION_INSTRUCTIONS,
    SPLIT_INSTRUCTIONS,
)
from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replay(rows):
    """Include partial speaker boundaries and EOS, preserve receive order and clock."""
    histories, assemblers, now, ended = {}, {}, [0.0], set()
    for row in rows:
        if row["event_type"] not in {"AddPartialSegment", "AddSegment", "EndOfTranscript"}:
            continue
        identity = (row["meeting_id"], row["session_id"])
        if identity[0] == "smoke":
            continue
        now[0] = row["received_monotonic_ms"] / 1000
        if identity not in histories:
            history = TranslationHistory(clock=lambda: now[0])
            histories[identity] = history

            def emit(sources, reason, ms, h=history):
                unit = h.emit_unit(sources, reason, ms)
                h.reconstructions.plan(unit)

            assemblers[identity] = TranslationUnitAssembler(emit, clock=lambda: now[0])
        history, assembler = histories[identity], assemblers[identity]
        event = row.get("raw_event") or {}
        kind = row["event_type"]
        if kind == "AddPartialSegment":
            assembler.note_speaker((event.get("segment") or {}).get("speaker"), identity[1])
        elif kind == "AddSegment":
            source = history.record_segment(event, identity[1])
            if source:
                assembler.accept(source)
        elif kind == "EndOfTranscript":
            assembler.flush("eos")
            ended.add(identity)
    for identity in histories:
        if identity not in ended:
            raise ValueError(f"Missing EOS: {identity[0]}")
    return windows_for_histories(histories)


def windows_for_histories(histories):
    """Project replayed production histories; no timestamps inferred here."""
    windows = []
    for identity, history in histories.items():
        entries = history.reconstructions.entries()
        # Final coverage groups only; intermediate 2-unit versions stay diagnostic.
        for revision in entries:
            if any(set(revision.unit_ids) < set(r.unit_ids) for r in entries):
                continue
            target = revision.translation
            first = revision.unit_ids[0]
            context_units = history.segments()[max(0, first - 5) : first]
            windows.append(
                {
                    "id": f"{identity[0]}-r{revision.revision_id}",
                    "meeting_id": identity[0],
                    "session_id": identity[1],
                    "unit_ids": revision.unit_ids,
                    "source_ids": target.source_segment_ids,
                    "context_source_ids": [s for u in context_units for s in u.source_segment_ids],
                    "start_ms": target.start_ms,
                    "end_ms": target.end_ms,
                    "speaker": target.speaker,
                    "english": target.en_text,
                    "context": history.reconstructions.context_for(target.unit_id),
                    "raw_sources": [asdict(s) for s in target.raw_source_segments],
                }
            )
    return windows


def select(windows, anchors):
    chosen = []
    for meeting, seconds, split, category in anchors:
        pool = [
            w
            for w in windows
            if w["meeting_id"] == meeting and w["start_ms"] >= seconds * 1000 and w not in chosen
        ]
        if not pool:
            raise ValueError("No window for anchor")
        chosen.append(dict(min(pool, key=lambda w: w["start_ms"]), split=split, category=category))
    for a in chosen:
        for b in chosen:
            if a["split"] == b["split"] or a["session_id"] != b["session_id"]:
                continue
            a_ids = set(a["source_ids"]) | set(a["context_source_ids"])
            b_ids = set(b["source_ids"]) | set(b["context_source_ids"])
            if a_ids & b_ids:
                raise ValueError("Development/holdout source or context leakage")
    if len({w["id"] for w in chosen}) != len(chosen):
        raise ValueError("Repeated selection")
    return chosen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--anchors", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in (args.source / "events.jsonl").open(encoding="utf-8")]
    windows = replay(rows)
    if not args.anchors:
        for w in windows:
            print(
                w["id"],
                w["start_ms"] // 1000,
                (w["end_ms"] - w["start_ms"]) / 1000,
                len(w["english"].split()),
                w["english"][:145],
            )
        return
    if (args.output / "manifest.json").exists():
        raise FileExistsError("Frozen manifest already exists")
    selected = select(windows, read_json(args.anchors))
    manifest = {
        "start_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dataset": "Earnings22",
        "license": "CC-BY-SA-4.0",
        "source": str(args.source),
        "source_hashes": {
            name: file_hash(args.source / name)
            for name in ("manifest.json", "events.jsonl", "transcript.jsonl")
        },
        "source_manifest": read_json(args.source / "manifest.json"),
        "pipeline": "split_then_batch_translate_v1",
        "baseline_prompt": SPLIT_INSTRUCTIONS,
        "translation_prompt": BATCH_TRANSLATION_INSTRUCTIONS,
        "model": "gpt-6-luna",
        "reasoning": {"effort": "none"},
        "windows": selected,
        "selection": read_json(args.anchors),
        "planner": "Production final pairs and maximal revision groups; no queue failures",
        "previous_ja": "ASR-only cache: empty previous_translations for every variant",
        "coverage_ms": sum(w["end_ms"] - w["start_ms"] for w in selected),
    }
    write_json(args.output / "manifest.json", manifest)
    print(f"Frozen {len(selected)} windows, {manifest['coverage_ms'] / 1000:.2f} seconds")


if __name__ == "__main__":
    main()
