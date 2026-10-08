"""One bounded iteration, resumable via content-addressed request cache."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from .judge import JUDGE_PROMPT, Verdict, check_verdict
from .prepare import digest, read_json, write_json
from .runner import Runner, client


def utc():
    return datetime.now(UTC).isoformat()


async def execute(args, api):
    manifest = read_json(args.output / "manifest.json")
    windows = [w for w in manifest["windows"] if w["split"] == args.split]
    if args.ids:
        windows = [w for w in windows if w["id"] in args.ids]
        if len(windows) != len(args.ids):
            raise ValueError("Unknown IDs or split mismatch")
    prompt = args.prompt.read_text() if args.prompt else manifest["baseline_prompt"]
    directory = args.output / args.name
    path = directory / f"{args.split}{('-' + args.nonce) if args.nonce else ''}.json"
    inputs = {
        "manifest_hash": digest(manifest),
        "prompt": prompt,
        "judge": JUDGE_PROMPT,
        "window_ids": [w["id"] for w in windows],
        "nonce": args.nonce,
        "compare": str(args.compare) if args.compare else None,
        "reverse": args.reverse,
    }
    if path.exists() and read_json(path)["inputs"] != inputs:
        raise ValueError("Iteration identity changed; use a new name")
    report = {
        "inputs": inputs,
        "started": utc(),
        "status": "running",
        "results": {},
        "judgments": {},
        "nonce": args.nonce,
    }
    started = time.monotonic()
    runner = Runner(args.output, api, seconds=args.seconds)
    write_json(path, report)

    async def translate(w):
        try:
            result = await runner.translate(w, prompt, nonce=args.nonce)
        except Exception as exc:
            result = {"id": w["id"], "status": "error", "error": type(exc).__name__, "attempts": []}
        report["results"][w["id"]] = result
        write_json(path, report)
        print(w["id"], result["status"], flush=True)

    def judge_data(result):
        paragraphs = [dict(p, paragraph=i) for i, p in enumerate(result["paragraphs"])]
        return {
            "paragraphs": paragraphs,
            "full_translation": result["japanese_translation"],
            "INTERNAL_BOUNDARIES": [
                {"after_paragraph": i, "left": paragraphs[i], "right": paragraphs[i + 1]}
                for i in range(len(paragraphs) - 1)
            ],
        }

    async def judge(w, baseline):
        original, candidate = baseline["results"].get(w["id"]), report["results"][w["id"]]
        if not original or not original.get("paragraphs") or not candidate.get("paragraphs"):
            report["judgments"][w["id"]] = {"status": "missing_valid_pair"}
            write_json(path, report)
            return
        swap = bool(int(digest(w["id"])[0], 16) % 2) ^ args.reverse
        a, b = (candidate, original) if swap else (original, candidate)
        data = {
            "FULL_ENGLISH": w["english"],
            "CONTEXT": w["context"],
            "A": judge_data(a),
            "B": judge_data(b),
        }
        call = await runner.call(w["id"] + "-judge", JUDGE_PROMPT, data, Verdict, nonce=args.nonce)
        grade = {
            "status": call["status"],
            "candidate_label": "A" if swap else "B",
            "call": call,
            "draft_only": original["status"] != "valid" or candidate["status"] != "valid",
        }
        if call["status"] == "completed" and call.get("parsed"):
            try:
                check_verdict(
                    Verdict.model_validate(call["parsed"]), a["paragraphs"], b["paragraphs"]
                )
            except ValueError:
                grade["status"] = "judge_invalid"
        report["judgments"][w["id"]] = grade
        write_json(path, report)
        print(w["id"], "judge", grade["status"], flush=True)

    try:
        await asyncio.gather(*(translate(w) for w in windows))
        if args.compare:
            baseline = read_json(args.compare)
            if baseline["inputs"]["manifest_hash"] != digest(manifest):
                raise ValueError("Baseline input mismatch")
            await asyncio.gather(*(judge(w, baseline) for w in windows))
        report["status"] = "finished"
    finally:
        report.update(ended=utc(), elapsed_sec=round(time.monotonic() - started, 3))
        write_json(path, report)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--nonce", default="")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--seconds", type=float, default=240)
    args = parser.parse_args()
    lock = args.output / "runner.lock"
    # Exclusive process lock; after abnormal exit inspect the process before removing this file.
    with lock.open("x") as f:
        import os

        f.write(json.dumps({"pid": os.getpid(), "started": utc()}))
    try:

        async def run():
            async with client() as api:
                print(await execute(args, api))

        asyncio.run(run())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
