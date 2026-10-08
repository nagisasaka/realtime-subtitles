"""Compare frozen outputs across implementations without relabeling their manifests."""

import argparse
import asyncio
from copy import deepcopy
from pathlib import Path

from .judge import JUDGE_PROMPT, Verdict, check_verdict
from .prepare import digest, read_json, write_json
from .runner import Runner, client


def comparable_window(window):
    result = deepcopy(window)
    # SourceSegment.received_at is assigned during local replay, not taken from audio.
    # Keep all audio/monotonic times, source IDs, ASR text, context, speaker and raw events.
    for source in result.get("raw_sources", []):
        source.pop("received_at", None)
    return result


def checked_windows(old_manifest, new_manifest, old_run, new_run, ids=None):
    for manifest, run in [(old_manifest, old_run), (new_manifest, new_run)]:
        if run["inputs"]["manifest_hash"] != digest(manifest):
            raise ValueError("Run does not belong to its manifest")
    if old_manifest["source_hashes"] != new_manifest["source_hashes"]:
        raise ValueError("ASR source hashes differ")
    previous = {w["id"]: w for w in old_manifest["windows"]}
    selected = [w for w in new_manifest["windows"] if w["id"] in new_run["results"]]
    if ids:
        selected = [w for w in selected if w["id"] in ids]
        if len(selected) != len(ids):
            raise ValueError("Unknown comparison IDs")
    for window in selected:
        identity = window["id"]
        if identity not in old_run["results"] or identity not in previous:
            raise ValueError("Historical output missing")
        if comparable_window(previous[identity]) != comparable_window(window):
            raise ValueError("Comparison inputs differ")
    if not selected:
        raise ValueError("No comparison windows")
    return selected


def judge_data(result):
    paragraphs = [dict(p, paragraph=i) for i, p in enumerate(result["paragraphs"])]
    return {
        "paragraphs": paragraphs,
        "INTERNAL_BOUNDARIES": [
            {"after_paragraph": i, "left": paragraphs[i], "right": paragraphs[i + 1]}
            for i in range(len(paragraphs) - 1)
        ],
    }


async def compare(args, api):
    old_manifest, manifest = read_json(args.old_manifest), read_json(args.manifest)
    old, current = read_json(args.old), read_json(args.current)
    windows = checked_windows(old_manifest, manifest, old, current, args.ids)
    report = deepcopy(current)
    report["judgments"] = {}
    report["comparison_status"] = "running"
    report["comparison"] = {
        "historical_manifest_hash": digest(old_manifest),
        "historical_run_hash": digest(old),
        "current_run_hash": digest(current),
        "identical_inputs_except_replay_received_at": True,
        "judge_prompt": JUDGE_PROMPT,
        "reverse": args.reverse,
        "ids": [w["id"] for w in windows],
        "historical_invalid_drafts_are_not_displayed_results": True,
    }
    runner = Runner(args.directory, api, seconds=args.seconds)

    async def grade(window):
        identity = window["id"]
        reference, candidate = old["results"][identity], current["results"][identity]
        if not reference.get("paragraphs") or not candidate.get("paragraphs"):
            report["judgments"][identity] = {"status": "missing_valid_pair"}
        else:
            swap = bool(int(digest(identity)[0], 16) % 2) ^ args.reverse
            a, b = (candidate, reference) if swap else (reference, candidate)
            call = await runner.call(
                identity + "-judge",
                JUDGE_PROMPT,
                {
                    "FULL_ENGLISH": window["english"],
                    "CONTEXT": window["context"],
                    "A": judge_data(a),
                    "B": judge_data(b),
                },
                Verdict,
            )
            judgment = {
                "status": call["status"],
                "candidate_label": "A" if swap else "B",
                "call": call,
                "draft_only": reference["status"] != "valid" or candidate["status"] != "valid",
            }
            if call["status"] == "completed" and call.get("parsed"):
                try:
                    check_verdict(
                        Verdict.model_validate(call["parsed"]), a["paragraphs"], b["paragraphs"]
                    )
                except ValueError:
                    judgment["status"] = "judge_invalid"
            report["judgments"][identity] = judgment
        write_json(args.output, report)
        print(identity, report["judgments"][identity]["status"], flush=True)

    try:
        for window in windows:
            await grade(window)
        report["comparison_status"] = (
            "finished"
            if all(j["status"] == "completed" for j in report["judgments"].values())
            else "incomplete"
        )
    except BaseException:
        report["comparison_status"] = "incomplete"
        raise
    finally:
        write_json(args.output, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    for name in ["old-manifest", "manifest", "old", "current", "directory", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--seconds", type=float, default=240)
    args = parser.parse_args()

    async def run():
        async with client() as api:
            await compare(args, api)

    lock = args.directory / "runner.lock"
    with lock.open("x") as stream:
        import os

        stream.write(str(os.getpid()))
    try:
        asyncio.run(run())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
