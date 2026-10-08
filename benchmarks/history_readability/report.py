"""Transparent counts and blind pairs; no subjective scalar quality score."""

import argparse
import statistics
from pathlib import Path

from .prepare import read_json, write_json


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo = int(index)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)


def metrics(run):
    results = list(run["results"].values())
    word_lengths = [len(p["en"].split()) for r in results for p in r.get("paragraphs", [])]
    latencies = [sum(c.get("latency_ms", 0) for c in r["attempts"]) for r in results]
    output = {
        "windows": len(results),
        "valid": sum(r["status"] == "valid" for r in results),
        "structural_failures": sum(r["status"] == "structural_failure" for r in results),
        "statuses": {r["id"]: r["status"] for r in results},
        "paragraphs_including_rejected_drafts": len(word_lengths),
        "short_paragraphs_le5_words": sum(n <= 5 for n in word_lengths),
        "median_paragraph_words": statistics.median(word_lengths) if word_lengths else None,
        "generation_latency_p50_ms": percentile(latencies, 0.5),
        "generation_latency_p95_ms": percentile(latencies, 0.95),
        "retries": sum(r.get("retry_count", 0) for r in results),
        "judged": 0,
        "missing_judgments": 0,
        "readability": {"win": 0, "loss": 0, "tie": 0, "uncertain": 0},
        "baseline": {},
        "candidate": {},
    }
    for grade in run.get("judgments", {}).values():
        if grade["status"] != "completed" or not grade.get("call", {}).get("parsed"):
            output["missing_judgments"] += 1
            continue
        output["judged"] += 1
        parsed, candidate = grade["call"]["parsed"], grade["candidate_label"]
        choice = parsed["readability"]
        verdict = (
            choice if choice in {"tie", "uncertain"} else "win" if choice == candidate else "loss"
        )
        output["readability"][verdict] += 1
        for key, label in [
            ("candidate", candidate),
            ("baseline", "B" if candidate == "A" else "A"),
        ]:
            scores = parsed[label]
            counts = {
                "unnecessary_boundaries": sum(
                    b["grade"] == "unnecessary" for b in scores["boundaries"]
                ),
                "uncertain_boundaries": sum(
                    b["grade"] == "uncertain" for b in scores["boundaries"]
                ),
                **{
                    name: len(scores[name])
                    for name in ("major_translation_errors", "alignment_errors", "overmerging")
                },
            }
            for name, count in counts.items():
                output[key][name] = output[key].get(name, 0) + count
    return output


def pairs(manifest, run, baseline):
    lines = [
        "# 匿名比較表",
        "",
        "内部境界のみを評価。棄却draftも診断用に掲載し、実画面表示とは区別する。",
        "",
    ]
    for w in manifest["windows"]:
        if w["id"] not in run["results"]:
            continue
        grade = run.get("judgments", {}).get(w["id"], {})
        candidate_a = grade.get("candidate_label") == "A"
        old, new = baseline["results"][w["id"]], run["results"][w["id"]]
        a, b = (new, old) if candidate_a else (old, new)
        lines.extend(
            [
                f"## {w['id']} ({w['start_ms'] / 1000:.2f}–{w['end_ms'] / 1000:.2f}s)",
                "",
                "原文: " + w["english"],
                "",
            ]
        )
        for label, result in [("A", a), ("B", b)]:
            lines.extend([f"### {label}", ""])
            for p in result.get("paragraphs", []):
                lines.extend([p["ja"], "", p["en"], "", "---", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    run = read_json(args.run)
    summary = metrics(run)
    write_json(args.run.with_suffix(".metrics.json"), summary)
    import json

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.baseline and args.manifest:
        args.run.with_suffix(".pairs.md").write_text(
            pairs(read_json(args.manifest), run, read_json(args.baseline)), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
