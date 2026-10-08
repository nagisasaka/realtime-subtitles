"""Frozen Earnings22 boundary continuations; no ASR, microphone, or live timing claims."""

import argparse
import asyncio
import json
import time
from dataclasses import asdict, replace
from pathlib import Path

from realtime_subtitles.history_reconstruction import (
    BATCH_TRANSLATION_INSTRUCTIONS,
    SPLIT_INSTRUCTIONS,
    EnglishChunk,
    HistoryRevision,
    ReconstructedParagraph,
)
from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory

from .prepare import digest, file_hash, read_json, write_json
from .runner import Runner, client

CASE_IDS = ("4474955-r5", "4474955-r13", "4474955-r17", "4474955-r21", "4475604-r1", "4475604-r5")


def source_histories(rows):
    histories, assemblers, ended, now = {}, {}, set(), [0.0]
    for row in rows:
        kind, identity = row["event_type"], row["session_id"]
        if kind not in {"AddSegment", "AddPartialSegment", "EndOfTranscript"}:
            continue
        now[0] = row["received_monotonic_ms"] / 1000
        if identity not in histories:
            h = histories[identity] = TranslationHistory(clock=lambda: now[0])
            assemblers[identity] = TranslationUnitAssembler(h.emit_unit, clock=lambda: now[0])
        h, assembler = histories[identity], assemblers[identity]
        event = row.get("raw_event") or {}
        if kind == "AddSegment":
            source = h.record_segment(event, identity)
            if source:
                assembler.accept(source)
        elif kind == "AddPartialSegment":
            assembler.note_speaker((event.get("segment") or {}).get("speaker"), identity)
        else:
            assembler.flush("eos")
            ended.add(identity)
    if set(histories) != ended:
        raise ValueError("Missing EOS")
    return histories


def add_saved_unit(history, unit):
    sources = []
    for s in unit.raw_source_segments:
        source = history.record_segment(json.loads(s.raw_event_json), s.session_id)
        if source is None:
            raise ValueError("Duplicate source")
        sources.append(source)
    result = history.emit_unit(sources, unit.assembly_reason, unit.assembled_monotonic_ms)
    if result.en_text != unit.en_text or result.unit_id != unit.unit_id:
        raise ValueError("Frozen source replay mismatch")
    return result


def seed(history, window, result):
    """Install the same validated pre-change display in both arms, not new model prose."""
    units = history.segments()
    selected = [units[i] for i in window["unit_ids"]]
    english = " ".join(u.en_text.strip() for u in selected)
    if english != window["english"] or result["status"] != "valid":
        raise ValueError("Invalid frozen parent")
    target = replace(
        selected[0],
        sequence_id=0,
        en_text=english,
        end_ms=selected[-1].end_ms,
        ja_text=None,
        translation_status="pending",
        source_segment_ids=tuple(k for u in selected for k in u.source_segment_ids),
        raw_source_segments=tuple(s for u in selected for s in u.raw_source_segments),
    )
    h = history.reconstructions
    # The seed's three-unit window is the old planner's frozen state. Only
    # subsequent jobs use the new production planner and its character ranges.
    h._entries.append(HistoryRevision(0, tuple(window["unit_ids"]), "frozen_baseline", target))
    h._counts["pending"] = 1
    h._record(h._entries[0])
    apply_result(history, target, result)


def apply_result(history, target, result):
    h = history.reconstructions
    for call in result.get("attempts", []):
        h.record_decision(
            target.unit_id,
            call.get("parsed"),
            call.get("usage"),
            stage=call["stage"],
            status=call["status"],
            latency_ms=call.get("latency_ms", 0),
        )
    if result["status"] != "valid":
        h.update_translation(target.unit_id, "failed", error=result["status"])
        return
    h.set_chunks(target.unit_id, tuple(EnglishChunk(**c) for c in result["chunks"]))
    paragraphs = tuple(
        ReconstructedParagraph(p["en_start"], p["en_end"], p["ja"]) for p in result["paragraphs"]
    )
    h.set_paragraphs(target.unit_id, paragraphs)
    h.update_translation(
        target.unit_id, "completed", text="\n\n".join(p.ja_text for p in paragraphs)
    )


def prepare(directory, baseline):
    if (directory / "manifest.json").exists():
        return read_json(directory / "manifest.json")
    old = read_json(baseline / "manifest.json")
    for name, sha in old["source_hashes"].items():
        if file_hash(Path(old["source"]) / name) != sha:
            raise ValueError("Changed ASR input")
    histories = source_histories(
        json.loads(line) for line in (Path(old["source"]) / "events.jsonl").open(encoding="utf-8")
    )
    frozen = read_json(baseline / "candidate3/dev.json")["results"]
    cases = []
    for w in old["windows"]:
        if w["id"] not in CASE_IDS:
            continue
        units = histories[w["session_id"]].segments()
        end = w["unit_ids"][-1]
        following = units[end + 1 : end + 4]
        if len(following) != 3 or any(u.speaker != w["speaker"] for u in following):
            raise ValueError("Unexpected speaker boundary")
        cases.append(
            {"window": w, "initial": frozen[w["id"]], "following": [asdict(u) for u in following]}
        )
    manifest = {
        "source": old["source"],
        "source_hashes": old["source_hashes"],
        "baseline_hash": digest(old),
        "split_prompt": SPLIT_INSTRUCTIONS,
        "translation_prompt": BATCH_TRANSLATION_INSTRUCTIONS,
        "model": "gpt-6-luna",
        "reasoning": "none",
        "cases": cases,
        "selection": "4 fragmented seams, 2 complete-thought controls; chosen before responses",
        "mode": "Sequential jobs from identical prior display; no streaming latency claim",
        "input_unit_clock": "Saved source timestamps, not API send time",
    }
    write_json(directory / "manifest.json", manifest)
    return manifest


async def execute(directory, manifest, api, *, name="comparison", ids=None, nonce=""):
    from realtime_subtitles.translation_history import SourceSegment, TranslationUnit

    if ids and not set(ids) <= {c["window"]["id"] for c in manifest["cases"]}:
        raise ValueError("Unknown case IDs")
    for filename, sha in manifest["source_hashes"].items():
        if file_hash(Path(manifest["source"]) / filename) != sha:
            raise ValueError("Changed frozen ASR source")
    histories = source_histories(
        json.loads(line)
        for line in (Path(manifest["source"]) / "events.jsonl").open(encoding="utf-8")
    )
    runner = Runner(directory, api, seconds=240)
    report = {
        "manifest_hash": digest(manifest),
        "cases": {},
        "nonce": nonce,
        "policy": "tail_review_preserve_previous_paragraph_v1",
    }
    path = directory / f"{name}.json"
    started = time.monotonic()
    for case in manifest["cases"]:
        w = case["window"]
        if ids and w["id"] not in ids:
            continue
        units = histories[w["session_id"]].segments()
        history = TranslationHistory()
        for unit in units[: w["unit_ids"][-1] + 1]:
            add_saved_unit(history, unit)
        seed(history, w, case["initial"])
        prefix = history.reconstructions.effective_blocks()
        prefix = tuple(b for b in prefix if b.start[0] >= w["unit_ids"][0])[:-1]
        steps, following = [], []
        for data in case["following"]:
            data = dict(data)
            data["raw_source_segments"] = tuple(
                SourceSegment(**s) for s in data["raw_source_segments"]
            )
            following.append(TranslationUnit(**data))
        baseline_window = {
            "id": w["id"] + "-next-old",
            "english": " ".join(u.en_text.strip() for u in following),
            "context": histories[w["session_id"]].context_for(following[0].unit_id),
        }
        baseline = await runner.translate(
            baseline_window,
            manifest["split_prompt"],
            translation_prompt=manifest["translation_prompt"],
            nonce=nonce,
        )
        for unit in following:
            current = add_saved_unit(history, unit)
            target = history.reconstructions.plan(current)
            if target is None:
                steps.append({"unit_id": current.unit_id, "status": "not_planned"})
                continue
            target = history.reconstructions.prepare(target.unit_id)
            if target is None:
                steps.append({"unit_id": current.unit_id, "status": "skipped"})
                continue
            window = {
                "id": w["id"] + f"-new-{current.unit_id}",
                "english": target.en_text,
                "context": history.reconstructions.context_for(target.unit_id),
            }
            result = await runner.translate(
                window,
                manifest["split_prompt"],
                translation_prompt=manifest["translation_prompt"],
                nonce=nonce,
                protected_prefix_chars=history.reconstructions.protected_prefix_for(target.unit_id),
            )
            apply_result(history, target, result)
            steps.append(
                {
                    "unit_id": current.unit_id,
                    "input": window,
                    "result": result,
                    "revision": asdict(history.reconstructions.entries()[-1]),
                }
            )
            print(w["id"], current.unit_id, result["status"], flush=True)
        blocks = [
            b for b in history.reconstructions.effective_blocks() if b.start[0] >= w["unit_ids"][0]
        ]
        expected = w["english"] + " " + baseline_window["english"]
        preserved = " ".join(b.en_text for b in blocks).split() == expected.split()
        if not preserved or tuple(blocks[: len(prefix)]) != prefix:
            raise AssertionError("Source coverage or retained prefix changed")
        report["cases"][w["id"]] = {
            "baseline_window": baseline_window,
            "baseline": baseline,
            "before": case["initial"]["paragraphs"] + baseline.get("paragraphs", []),
            "after": [asdict(b) for b in blocks],
            "steps": steps,
            "source_preserved": preserved,
            "prefix_preserved": True,
        }
        history.save(directory / f"{name}-{w['id']}.jsonl")
        history.save(directory / f"{name}-{w['id']}.txt")
        report["elapsed_sec"] = time.monotonic() - started
        write_json(path, report)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--name", default="comparison")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--nonce", default="")
    args = parser.parse_args()
    manifest = prepare(args.output, args.baseline)
    lock = args.output / "runner.lock"
    with lock.open("x") as f:
        import os

        f.write(str(os.getpid()))
    try:

        async def run():
            async with client() as api:
                print(
                    await execute(
                        args.output, manifest, api, name=args.name, ids=args.ids, nonce=args.nonce
                    )
                )

        asyncio.run(run())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
