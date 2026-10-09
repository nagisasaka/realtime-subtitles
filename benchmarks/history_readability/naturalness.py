"""Five preregistered ideas, frozen inputs and anonymous cross-model judging.

Only cached ASR text is used; no microphone, Speechmatics or production settings.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

from pydantic import Field

from realtime_subtitles.history_reconstruction import (
    BATCH_TRANSLATION_INSTRUCTIONS,
    SPLIT_INSTRUCTIONS,
)
from realtime_subtitles.text_translation import safe_error

from .judge import JUDGE_PROMPT, CandidateGrade, Finding, Verdict, check_verdict
from .prepare import digest, file_hash, read_json, replay, select, write_json
from .runner import Runner, client

IDEAS = {
    "discourse": {
        "title": "意味のまとまりで分割",
        "split": """Additional reading-unit policy: when two grammatically complete clauses form
one local speech act (a claim and its immediate explanation, an instruction and its
condition, or an introduction and its list), prefer one chunk. Keep a transition
to a different subject or action separate. A topic heading must stay with its
explanation if available. Do not merge every statement merely because the speaker
or broad topic is the same. Never use a minimum word count or invent missing text.
""",
        "translation": "",
    },
    "japanese_syntax": {
        "title": "日本語の語順・述語を自然に",
        "split": "",
        "translation": """Japanese readability: interpret each complete TARGET as connected speech,
not as isolated ASR sentences. Reorder its clauses into idiomatic Japanese so the
topic, modifiers and predicate are easy to follow. Do not mirror spurious English
periods as Japanese fragments. Prefer a natural verb over a stiff noun stack or
English-shaped construction. If the supplied meaning is complete, finish the
Japanese predicate; if the TARGET itself is incomplete, preserve that uncertainty
without fabricating its completion. Do not move content between TARGET IDs.
""",
    },
    "concise": {
        "title": "冗長な日本語表現を簡潔に",
        "split": "",
        "translation": """Write efficient Japanese suitable for a quick glance. Use direct familiar
verbs and compact phrasing instead of inflated nominalizations and boilerplate
such as unnecessary 'ということ' or 'することができます'. This is not a summary:
retain every factual proposition, condition, attribution, qualification and contrast.
Do not delete repeated source claims or uncertainty just to shorten the subtitle.
Prefer the shorter equivalent expression only when it conveys the same information.
""",
    },
    "referents": {
        "title": "指示語・主語の対応を明確に",
        "split": "",
        "translation": """Make Japanese references understandable when the paired caption
is read alone.
Use CONTEXT to resolve a pronoun or omitted repeated topic only when the antecedent
is unambiguous. A brief repeated noun may replace 'それ' if it prevents confusion;
do not restate earlier claims or add explanatory facts. Preserve ambiguity when
multiple antecedents are possible. Keep the speaker's perspective and avoid a
run of unnatural explicit pronouns when ordinary Japanese omission is clear.
""",
    },
    "terminology": {
        "title": "用語・数値表現を一貫させる",
        "split": "",
        "translation": """Use consistent Japanese for recurring terms in TARGETS and their English
CONTEXT. Prefer conventional domain terminology to a literal general-language
calque. Keep technical abbreviations or uncertain proper names in English rather
than guessing corrections or inventing expansions. Keep amounts readable with
standard Japanese magnitudes (万/億) when exact and unambiguous; never alter the
value, currency, reporting period, comparison basis or direction of change.
Do not insert glossary explanations, parenthetical expansions or absent units.
""",
    },
}

HUMAN_RUBRIC = (
    JUDGE_PROMPT
    + """
Also assess perceived Japanese naturalness and reading effort from a fluent reader's
perspective; you are a model proxy, not an actual human subject. Judge exact wording,
not the presence of editorial guidelines. Naturalness 1=broken, 2=marked translationese,
3=understandable with awkwardness, 4=natural with minor friction, 5=effortless idiomatic
Japanese. Ease 1=repeated rereading needed, 2=hard, 3=some mental repair, 4=clear,
5=immediately graspable. Faithful incomplete edges can still be natural; do not reward
invented completions. Rate both candidates before selecting readability preference.
Record concrete wording issues in style_findings with short exact source/output quotes.
Do not award a win solely for shortening, adding nouns, fewer chunks or more formal prose.
Chronological paragraph IDs are provided; DISPLAY_TOP_TO_BOTTOM is the actual newest-first
history display. Preserve normal reading order within each paragraph. Ignore raw ASR
punctuation errors if a Japanese phrase naturally expresses the exact supplied meaning.
Do not consider alternative translations of an ambiguous ASR error automatically wrong.
Give short evidence, not hidden reasoning. Output reason and finding reasons in Japanese.
"""
)


class ReaderGrade(CandidateGrade):
    naturalness: int = Field(ge=1, le=5)
    ease: int = Field(ge=1, le=5)
    style_findings: list[Finding]


class ReaderVerdict(Verdict):
    A: ReaderGrade
    B: ReaderGrade


def freeze(directory, source, anchors):
    path = directory / "manifest.json"
    if path.exists():
        return read_json(path)
    if source is None or anchors is None:
        raise ValueError("First run requires --source and --anchors")
    windows = select(
        replay(json.loads(line) for line in (source / "events.jsonl").open(encoding="utf-8")),
        read_json(anchors),
    )
    variants = {
        "baseline": {
            "title": "現行",
            "split": SPLIT_INSTRUCTIONS,
            "translation": BATCH_TRANSLATION_INSTRUCTIONS,
        }
    }
    for key, idea in IDEAS.items():
        variants[key] = {
            "title": idea["title"],
            "split": SPLIT_INSTRUCTIONS + idea["split"],
            "translation": BATCH_TRANSLATION_INSTRUCTIONS + idea["translation"],
        }
    manifest = {
        "start_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dataset": "Earnings22",
        "license": "CC-BY-SA-4.0",
        "source": str(source),
        "source_hashes": {
            n: file_hash(source / n) for n in ("manifest.json", "events.jsonl", "transcript.jsonl")
        },
        "source_manifest": read_json(source / "manifest.json"),
        "windows": windows,
        "variants": variants,
        "selection": read_json(anchors),
        "judge_prompt": HUMAN_RUBRIC,
        "generator": {"model": "gpt-6-luna", "effort": "none"},
        "judge": {"model": "gpt-6-astra", "effort": "low"},
        "request_limit": 240,
        "max_iteration_sec": 480,
        "mode": "Frozen current initial-pair requests. Sequential carried-tail test separately.",
        "selection_gate": "No new confirmed major/pairing error, net readability wins; "
        "held-out and reverse-order/repeat checks before adoption.",
        "coverage_ms": sum(w["end_ms"] - w["start_ms"] for w in windows),
    }
    write_json(path, manifest)
    return manifest


def judge_data(result):
    paragraphs = [dict(p, paragraph=i) for i, p in enumerate(result["paragraphs"])]
    return {
        "paragraphs": paragraphs,
        "DISPLAY_TOP_TO_BOTTOM": list(reversed(range(len(paragraphs)))),
        "INTERNAL_BOUNDARIES": [
            {"after_paragraph": i, "left": paragraphs[i], "right": paragraphs[i + 1]}
            for i in range(len(paragraphs) - 1)
        ],
    }


def checked_verdict(parsed, a, b):
    verdict = ReaderVerdict.model_validate(parsed)
    check_verdict(verdict, a, b)
    for grade, paragraphs in ((verdict.A, a), (verdict.B, b)):
        for finding in grade.style_findings:
            if not 0 <= finding.paragraph < len(paragraphs):
                raise ValueError("Style finding paragraph out of range")
    return verdict


def variant_config(manifest, name, path=None):
    if path is None:
        return manifest["variants"][name]
    extension = read_json(path)
    if extension["parent_manifest_hash"] != digest(manifest) or extension["id"] != name:
        raise ValueError("Follow-up idea identity differs")
    if name in manifest["variants"]:
        raise ValueError("Cannot replace a preregistered idea")
    return extension["config"]


def metrics(report):
    results = list(report["results"].values())
    valid = [r for r in results if r["status"] == "valid"]
    latencies = [sum(a.get("latency_ms", 0) for a in r.get("attempts", ())) for r in valid]
    result = {
        "generated": len(results),
        "valid": len(valid),
        "paragraphs": sum(len(r.get("paragraphs", ())) for r in valid),
        "ja_chars": sum(len(p["ja"]) for r in valid for p in r["paragraphs"]),
        "retry_count": sum(r.get("retry_count", 0) for r in results),
        "api_wait_p50_ms": statistics.median(latencies) if latencies else None,
        "readability": {},
        "judge_complete": 0,
        "judge_missing": 0,
        # Counts alone cannot establish that the *same* error was retained.
        # Every flagged candidate needs evidence review even if baseline has errors too.
        "candidate_major_windows": [],
        "candidate_pairing_windows": [],
        "baseline_major_windows": [],
        "baseline_pairing_windows": [],
        "naturalness_delta": [],
        "ease_delta": [],
    }
    for identity, g in report["judgments"].items():
        if g["status"] != "completed":
            result["judge_missing"] += 1
            continue
        result["judge_complete"] += 1
        v = g["call"]["parsed"]
        c = g["candidate_label"]
        baseline = "B" if c == "A" else "A"
        preference = (
            "win"
            if v["readability"] == c
            else ("loss" if v["readability"] == baseline else v["readability"])
        )
        result["readability"][preference] = result["readability"].get(preference, 0) + 1
        for field, key in (
            ("major_translation_errors", "major_windows"),
            ("alignment_errors", "pairing_windows"),
        ):
            if v[c][field]:
                result["candidate_" + key].append(identity)
            if v[baseline][field]:
                result["baseline_" + key].append(identity)
        for field in ("naturalness", "ease"):
            result[field + "_delta"].append(v[c][field] - v[baseline][field])
    for field in ("naturalness", "ease"):
        values = result[field + "_delta"]
        result[field + "_delta"] = statistics.mean(values) if values else None
    return result


async def execute(args, manifest, api):
    config = variant_config(manifest, args.variant, getattr(args, "variant_file", None))
    for name, sha in manifest["source_hashes"].items():
        if file_hash(Path(manifest["source"]) / name) != sha:
            raise ValueError("Source changed")
    windows = [w for w in manifest["windows"] if w["split"] == args.split]
    if args.ids:
        if not set(args.ids) <= {w["id"] for w in windows}:
            raise ValueError("Unknown IDs")
        windows = [w for w in windows if w["id"] in args.ids]
    suffix = ("." + args.nonce if args.nonce else "") + (".reverse" if args.reverse else "")
    path = args.output / f"{args.variant}.{args.split}{suffix}.json"
    inputs = {
        "manifest_hash": digest(manifest),
        "variant": config,
        "ids": [w["id"] for w in windows],
        "nonce": args.nonce,
        "reverse": args.reverse,
    }
    baseline_path = getattr(args, "baseline_run", None)
    if baseline_path:
        inputs["comparison_hash"] = file_hash(baseline_path)
    previous = read_json(path) if path.exists() else None
    if previous and previous["inputs"] != inputs:
        raise ValueError("Existing experiment identity differs")
    baseline = None
    if args.variant != "baseline":
        baseline = read_json(baseline_path or args.output / f"baseline.{args.split}.json")
        if baseline["inputs"]["manifest_hash"] != digest(manifest):
            raise ValueError("Baseline manifest mismatch")
    runner = Runner(args.output, api, seconds=args.seconds, request_limit=manifest["request_limit"])
    report = {
        "inputs": inputs,
        "variant": args.variant,
        "split": args.split,
        "results": {},
        "judgments": {},
        "started_unix": time.time(),
        "status": "running",
    }
    if previous:
        report["previous_invocations"] = previous.get("previous_invocations", []) + [
            {k: previous.get(k) for k in ("started_unix", "ended_unix", "elapsed_sec", "status")}
        ]
    write_json(path, report)
    started = time.monotonic()

    async def case(w):
        result = await runner.translate(
            w, config["split"], translation_prompt=config["translation"], nonce=args.nonce
        )
        report["results"][w["id"]] = result
        write_json(path, report)
        print(args.variant, w["id"], result["status"], flush=True)
        if baseline is None:
            return
        original = baseline["results"].get(w["id"], {})
        if result["status"] != "valid" or original.get("status") != "valid":
            report["judgments"][w["id"]] = {"status": "not_both_valid"}
            write_json(path, report)
            return
        swap = bool(int(digest(w["id"])[0], 16) % 2) ^ args.reverse
        a, b = (result, original) if swap else (original, result)
        data = {
            "FULL_ENGLISH": w["english"],
            "CONTEXT": w["context"],
            "A": judge_data(a),
            "B": judge_data(b),
        }
        call = await runner.call(
            w["id"] + "-judge",
            manifest["judge_prompt"],
            data,
            ReaderVerdict,
            nonce=args.nonce,
            **manifest["judge"],
        )
        grade = {"status": call["status"], "candidate_label": "A" if swap else "B", "call": call}
        if call["status"] == "completed":
            try:
                checked_verdict(call.get("parsed"), a["paragraphs"], b["paragraphs"])
            except ValueError:
                grade["status"] = "judge_invalid"
        report["judgments"][w["id"]] = grade
        write_json(path, report)
        print(args.variant, w["id"], "judge", grade["status"], flush=True)

    async def guarded_case(w):
        try:
            await case(w)
        except Exception as exc:
            # A budget/schema failure in one case must not cancel other saved results.
            # Never serialize exception messages (they may contain API credentials).
            report["results"].setdefault(w["id"], {"status": "error", "error": safe_error(exc)})
            if baseline is not None:
                report["judgments"][w["id"]] = {"status": "error", "error": safe_error(exc)}
            write_json(path, report)

    try:
        await asyncio.gather(*(guarded_case(w) for w in windows))
        complete = len(report["results"]) == len(windows) and all(
            r["status"] not in {"deadline", "error", "cancelled"}
            for r in report["results"].values()
        )
        complete &= baseline is None or (
            len(report["judgments"]) == len(windows)
            and all(g["status"] == "completed" for g in report["judgments"].values())
        )
        report["status"] = "finished" if complete else "incomplete"
    finally:
        report.update(elapsed_sec=round(time.monotonic() - started, 3), ended_unix=time.time())
        report["metrics"] = metrics(report)
        write_json(path, report)
    print(json.dumps(report["metrics"], ensure_ascii=False), flush=True)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--anchors", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--variant-file", type=Path)
    parser.add_argument("--baseline-run", type=Path)
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--nonce", default="")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--seconds", type=float, default=300)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = freeze(args.output, args.source, args.anchors)
    if args.prepare:
        print(
            "Frozen", len(manifest["windows"]), "windows /", len(manifest["variants"]) - 1, "ideas"
        )
        return
    lock = args.output / "runner.lock"
    with lock.open("x") as f:
        json.dump({"pid": os.getpid(), "started_unix": time.time()}, f)
    try:

        async def run():
            async with client() as api:
                await execute(args, manifest, api)

        asyncio.run(run())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
