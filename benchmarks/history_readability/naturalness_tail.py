"""Compare prompts through the real sequential carried-paragraph planner."""

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from realtime_subtitles.translation_history import TranslationHistory

from .naturalness import ReaderVerdict, checked_verdict, judge_data, variant_config
from .prepare import digest, file_hash, read_json, write_json
from .runner import Runner, client
from .tail_review import add_saved_unit, apply_result, seed, source_histories


async def arm(runner, source_units, window, initial, following, prompts):
    history = TranslationHistory()
    for unit in source_units[: window["unit_ids"][-1] + 1]:
        add_saved_unit(history, unit)
    seed(history, window, initial)
    prefix = tuple(
        b for b in history.reconstructions.effective_blocks() if b.start[0] >= window["unit_ids"][0]
    )[:-1]
    steps = []
    for unit in following:
        current = add_saved_unit(history, unit)
        target = history.reconstructions.plan(current)
        if target is None:
            raise ValueError("Selected continuation cannot be planned")
        target = history.reconstructions.prepare(target.unit_id)
        if target is None:
            raise ValueError("Selected continuation skipped")
        job = {
            "id": window["id"] + f"-tail-{current.unit_id}",
            "english": target.en_text,
            "context": history.reconstructions.context_for(target.unit_id),
        }
        result = await runner.translate(
            job,
            prompts["split"],
            translation_prompt=prompts["translation"],
            protected_prefix_chars=history.reconstructions.protected_prefix_for(target.unit_id),
        )
        apply_result(history, target, result)
        steps.append(
            {
                "input": job,
                "result": result,
                "revision": asdict(history.reconstructions.entries()[-1]),
            }
        )
        print(job["id"], result["status"], flush=True)
    blocks = [
        b for b in history.reconstructions.effective_blocks() if b.start[0] >= window["unit_ids"][0]
    ]
    expected = window["english"] + " " + " ".join(u.en_text for u in following)
    if " ".join(b.en_text for b in blocks).split() != expected.split():
        raise AssertionError("English coverage changed")
    if tuple(blocks[: len(prefix)]) != prefix:
        raise AssertionError("Previously stable paragraph changed")
    expected_raw = [
        s.en_text for u in source_units[: following[-1].unit_id + 1] for s in u.raw_source_segments
    ]
    if [s.en_text for s in history.sources()] != expected_raw:
        raise AssertionError("Raw ASR text changed")
    return {
        "steps": steps,
        "blocks": [asdict(b) for b in blocks],
        "status": "valid" if all(s["result"]["status"] == "valid" for s in steps) else "failed",
        "paragraphs": [{"en": b.en_text, "ja": b.ja_text or ""} for b in blocks],
        "english": expected,
        "source_preserved": True,
        "prefix_preserved": True,
    }, history


async def execute(directory, variant, ids, api, variant_file=None):
    manifest = read_json(directory / "manifest.json")
    config = variant_config(manifest, variant, variant_file)
    for name, sha in manifest["source_hashes"].items():
        if file_hash(Path(manifest["source"]) / name) != sha:
            raise ValueError("Source changed")
    windows = [w for w in manifest["windows"] if w["id"] in ids]
    if len(windows) != len(set(ids)):
        raise ValueError("Unknown IDs")
    histories = source_histories(
        json.loads(line)
        for line in (Path(manifest["source"]) / "events.jsonl").open(encoding="utf-8")
    )
    identity = {
        "manifest_hash": digest(manifest),
        "variant": variant,
        "ids": ids,
        "following_units": 3,
        "variant_config": config,
    }
    path = directory / f"{variant}.tail.json"
    if path.exists() and read_json(path)["identity"] != identity:
        raise ValueError("Prior trial identity differs")
    report = {"identity": identity, "cases": {}, "status": "running"}
    runner = Runner(directory, api, seconds=300, request_limit=manifest["request_limit"])
    started = time.monotonic()
    try:
        for w in windows:
            original = read_json(directory / f"baseline.{w['split']}.json")
            initial = original["results"][w["id"]]
            units = histories[w["session_id"]].segments()
            following = units[w["unit_ids"][-1] + 1 : w["unit_ids"][-1] + 4]
            if len(following) != 3 or any(u.speaker != w["speaker"] for u in following):
                raise ValueError("Continuation crosses speaker or recording end")
            case = report["cases"][w["id"]] = {}
            for name in ("baseline", variant):
                result, history = await arm(
                    runner,
                    units,
                    w,
                    initial,
                    following,
                    manifest["variants"]["baseline"] if name == "baseline" else config,
                )
                case[name] = result
                history.save(directory / f"{variant}.tail.{w['id']}.{name}.jsonl")
                history.save(directory / f"{variant}.tail.{w['id']}.{name}.txt")
                write_json(path, report)
            if any(case[k]["status"] != "valid" for k in ("baseline", variant)):
                case["judgment"] = {"status": "not_both_valid"}
                continue
            swap = bool(int(digest(w["id"])[0], 16) % 2)
            a, b = (case[variant], case["baseline"]) if swap else (case["baseline"], case[variant])
            data = {
                "FULL_ENGLISH": a["english"],
                "CONTEXT": w["context"],
                "A": judge_data(a),
                "B": judge_data(b),
            }
            call = await runner.call(
                w["id"] + "-tail-judge",
                manifest["judge_prompt"],
                data,
                ReaderVerdict,
                **manifest["judge"],
            )
            case["judgment"] = {
                "status": call["status"],
                "candidate_label": "A" if swap else "B",
                "call": call,
            }
            if call["status"] == "completed":
                try:
                    checked_verdict(call.get("parsed"), a["paragraphs"], b["paragraphs"])
                except ValueError:
                    case["judgment"]["status"] = "judge_invalid"
            write_json(path, report)
        report["status"] = (
            "finished"
            if all(c["judgment"]["status"] == "completed" for c in report["cases"].values())
            else "incomplete"
        )
    finally:
        report["elapsed_sec"] = time.monotonic() - started
        write_json(path, report)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--variant-file", type=Path)
    parser.add_argument("--ids", nargs="+", required=True)
    args = parser.parse_args()
    lock = args.output / "runner.lock"
    with lock.open("x") as f:
        json.dump({"pid": os.getpid()}, f)
    try:

        async def run():
            async with client() as api:
                print(await execute(args.output, args.variant, args.ids, api, args.variant_file))

        asyncio.run(run())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
