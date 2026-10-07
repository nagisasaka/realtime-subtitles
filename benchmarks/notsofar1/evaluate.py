"""Official time-constrained scores, with explicitly auxiliary non-overlap WER."""

import argparse
import csv
import importlib.metadata
import json
import logging
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import meeteval
import numpy as np
from meeteval.io import SegLST
from meeteval.wer.wer.time_constrained import align

from .common import ROOT, load, normalizer, save


def parse_gt(rows, meeting, normalize):
    result = []
    for index, row in enumerate(rows):
        start, end = float(row["start_time"]), float(row["end_time"])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
            raise ValueError("Invalid GT timing")
        if not isinstance(row["speaker_id"], str) or not isinstance(row["text"], str):
            raise ValueError("Invalid GT text/speaker")
        result.append(
            {
                "session_id": meeting,
                "speaker": row["speaker_id"],
                "start_time": start,
                "end_time": end,
                "words": normalize(row["text"]),
                "raw_text": row["text"],
                "utterance_id": index,
                "word_timing": row.get("word_timing"),
            }
        )
    return result  # Never concatenate, reorder or eliminate overlaps.


def parse_hyp(rows, meeting, normalize):
    result = []
    for index, row in enumerate(rows):
        if row["meeting_id"] != meeting or row["event_type"] != "AddSegment":
            continue
        if row["audio_start_ms"] is None or row["audio_end_ms"] is None:
            raise ValueError("Final missing audio timestamp; cannot time-score")
        if not isinstance(row["transcript"], str):
            raise ValueError("Final missing transcript")
        if not (0 <= row["audio_start_ms"] <= row["audio_end_ms"]):
            raise ValueError("Invalid final timing")
        result.append(
            {
                "session_id": meeting,
                "speaker": row["speaker"] or "__unknown__",
                "start_time": row["audio_start_ms"] / 1000,
                "end_time": row["audio_end_ms"] / 1000,
                "words": normalize(row["transcript"]),
                "raw_text": row["transcript"],
                "utterance_id": index,
            }
        )
    return result


def percentiles(values):
    return {
        "n": len(values),
        "p50": float(np.percentile(values, 50)) if values else None,
        "p95": float(np.percentile(values, 95)) if values else None,
        "max": max(values) if values else None,
    }


def overlap_intervals(reference):
    intervals = []
    for i, a in enumerate(reference):
        for b in reference[i + 1 :]:
            if a["speaker"] != b["speaker"]:
                lo, hi = max(a["start_time"], b["start_time"]), min(a["end_time"], b["end_time"])
                if hi > lo:
                    intervals.append((lo, hi))
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def word_points(rows):
    """Only auxiliary window WER: uniform word-center times, never claim forced alignment."""
    words = []
    for row in rows:
        tokens = row["words"].split()
        for i, token in enumerate(tokens):
            point = row["start_time"] + (row["end_time"] - row["start_time"]) * (i + 0.5) / len(
                tokens
            )
            words.append((point, token))
    return sorted(words, key=lambda x: x[0])


def auxiliary_wer(reference, hypothesis, duration):
    overlaps = overlap_intervals(reference)
    ref_words, hyp_words = word_points(reference), word_points(hypothesis)
    counts = Counter()
    windows = []
    for start in range(0, math.ceil(duration), 10):
        end = min(start + 10, duration)
        if any(lo < end and hi > start for lo, hi in overlaps):
            continue
        ref = " ".join(w for t, w in ref_words if start <= t < end)
        hyp = " ".join(w for t, w in hyp_words if start <= t < end)
        # Empty reference windows remain included: hallucinated words count as insertions.
        score = asdict(meeteval.wer.siso_word_error_rate(ref, hyp))
        for key in ("length", "errors", "substitutions", "deletions", "insertions"):
            counts[key] += score[key]
        windows.append({"start": start, "end": end, **score})
    return {
        **counts,
        "error_rate": counts["errors"] / counts["length"] if counts["length"] else None,
        "windows": windows,
        "seconds": sum(w["end"] - w["start"] for w in windows),
        "method": (
            "10-second bins with zero cross-speaker GT utterance overlap; "
            "uniform word-center allocation; boundary-sensitive auxiliary only"
        ),
    }


def score_meeting(reference, hypothesis, duration):
    ref, hyp = SegLST(reference), SegLST(hypothesis)
    mid = reference[0]["session_id"]
    tcp = meeteval.wer.tcpwer(ref, hyp, collar=5)[mid]
    tcorc = meeteval.wer.tcorcwer(ref, hyp, collar=5)[mid]
    errors = []
    # Retain both types: tcORC differences isolate text better than speaker-attributed TCP.
    pairs = [("tcpWER", tcp, reference, hypothesis, tcp.assignment)]
    orc_ref, orc_hyp = tcorc.apply_assignment(ref, hyp)
    streams = sorted(set(orc_ref.T["speaker"]) | set(orc_hyp.T["speaker"]))
    pairs.append(
        (
            "tcORC-WER",
            tcorc,
            list(orc_ref),
            list(orc_hyp),
            [(speaker, speaker) for speaker in streams],
        )
    )
    for metric, score, ref_rows, hyp_rows, assignment in pairs:
        begin = len(errors)
        for ref_speaker, hyp_speaker in assignment:
            rs = [r for r in ref_rows if r["speaker"] == ref_speaker]
            hs = [h for h in hyp_rows if h["speaker"] == hyp_speaker]
            for r, h in align(rs, hs, collar=5, style="seglst"):
                if r and h and r["words"] == h["words"]:
                    continue
                # tcORC assigned stream is NOT the person's GT identity.
                original_ref = next(
                    (v for v in reference if r and v["utterance_id"] == r["utterance_id"]), None
                )
                original_hyp = next(
                    (v for v in hypothesis if h and v["utterance_id"] == h["utterance_id"]), None
                )
                errors.append(
                    {
                        "metric": metric,
                        "meeting_id": mid,
                        "type": "S" if r and h else "D" if r else "I",
                        "gt_word": r["words"] if r else "",
                        "asr_word": h["words"] if h else "",
                        "gt_time": r["start_time"] if r else None,
                        "asr_time": h["start_time"] if h else None,
                        "gt_speaker": original_ref["speaker"] if original_ref else None,
                        "asr_speaker": hyp_speaker,
                        "gt_utterance": r["raw_text"] if r else "",
                        "asr_utterance": h["raw_text"] if h else "",
                        "gt_utterance_start": original_ref["start_time"] if original_ref else None,
                        "asr_utterance_start": original_hyp["start_time"] if original_hyp else None,
                    }
                )
        assert len(errors) - begin == score.errors, (metric, len(errors) - begin, score.errors)
    overlap = overlap_intervals(reference)
    result = {
        "reference_words": tcp.length,
        "hypothesis_words": sum(len(h["words"].split()) for h in hypothesis),
        "tcpWER": asdict(tcp),
        "tcORC_WER": asdict(tcorc),
        "ordinary_WER_nonoverlap": auxiliary_wer(reference, hypothesis, duration),
        "gt_overlap_seconds": sum(hi - lo for lo, hi in overlap),
        "gt_speakers": len({r["speaker"] for r in reference}),
        "asr_speakers": len({r["speaker"] for r in hypothesis}),
    }
    return result, errors


def evaluate(root, output):
    manifest = load(output / "manifest.json")
    normalize = normalizer()
    # ORC assigns overlapping GT speakers to shared output streams by design.
    # Exact alignment error totals are checked below; segment sort remains unchanged.
    logging.getLogger("preprocess").setLevel(logging.ERROR)
    events = [
        json.loads(line)
        for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    chunks = [json.loads(line) for line in (output / "chunks.jsonl").read_text().splitlines()]
    all_metrics, all_errors = [], []
    for attempt in manifest["attempts"]:
        if attempt["status"] != "completed" or not attempt.get("eos_received"):
            print("Not scored (incomplete):", attempt["meeting_id"])
            continue
        mid = attempt["meeting_id"]
        m = next(m for m in manifest["meetings"] if m["meeting_id"] == mid)
        gt = parse_gt(load(root / "references" / mid / "gt_transcription.json"), mid, normalize)
        hyp = parse_hyp(events, mid, normalize)
        save(output / f"{mid}-reference-normalized.json", gt)
        save(output / f"{mid}-hypothesis-normalized.json", hyp)
        duration = m["prepared_format"]["duration"]
        metrics, errors = score_meeting(gt, hyp, duration)
        final_delays, partial_delays = [], []
        first_partials = {}
        for event in events:
            if event["meeting_id"] != mid or event["playback_elapsed_ms"] is None:
                continue
            if (
                event["event_type"] in ("AddSegment", "AddPartialSegment")
                and event["audio_end_ms"] is not None
            ):
                delay = event["playback_elapsed_ms"] - event["audio_end_ms"]
                (final_delays if event["event_type"] == "AddSegment" else partial_delays).append(
                    delay
                )
            if event["event_type"] == "AddPartialSegment" and event["audio_start_ms"] is not None:
                key = (event["speaker"], event["audio_start_ms"])
                first_partials.setdefault(
                    key, event["playback_elapsed_ms"] - event["audio_start_ms"]
                )
        metrics.update(
            meeting_id=mid,
            categories=m["categories"],
            duration_seconds=duration,
            hashtags=m["metadata"]["Hashtags"],
            device=m["device"]["wav_file_names"],
            final_latency_ms=percentiles(final_delays),
            partial_audio_end_latency_ms=percentiles(partial_delays),
            first_partial_from_segment_start_ms=percentiles(list(first_partials.values())),
            send_lateness_ms=percentiles([c["late_ms"] for c in chunks if c["meeting_id"] == mid]),
            observed_event_counts=attempt["event_counts"],
        )
        all_metrics.append(metrics)
        all_errors.extend(errors)
        print(
            mid,
            "tcpWER",
            metrics["tcpWER"]["error_rate"],
            "tcORC-WER",
            metrics["tcORC_WER"]["error_rate"],
            flush=True,
        )
    aggregated = {}
    for category in sorted({c for m in all_metrics for c in m["categories"]} | {"ALL"}):
        selected = [m for m in all_metrics if category == "ALL" or category in m["categories"]]
        n = sum(m["reference_words"] for m in selected)
        aggregated[category] = {"meetings": len(selected), "reference_words": n}
        for metric in ("tcpWER", "tcORC_WER"):
            err = sum(m[metric]["errors"] for m in selected)
            aggregated[category][metric] = err / n if n else None
    result = {
        "scoring": manifest["scoring"],
        "normalization": manifest["normalization"],
        "meeteval_version": importlib.metadata.version("meeteval"),
        "meetings": all_metrics,
        "categories": aggregated,
    }
    save(output / "metrics.json", result)
    fields = [
        "meeting_id",
        "categories",
        "duration_seconds",
        "reference_words",
        "hypothesis_words",
        "substitutions",
        "deletions",
        "insertions",
        "WER",
        "tcpWER",
        "tcORC-WER",
        "final_p50_ms",
        "final_p95_ms",
        "final_max_ms",
    ]
    with (output / "metrics.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for m in all_metrics:
            writer.writerow(
                {
                    **{k: m[k] for k in fields[:5]},
                    **{k: m["tcpWER"][k] for k in ("substitutions", "deletions", "insertions")},
                    "WER": m["ordinary_WER_nonoverlap"]["error_rate"],
                    "tcpWER": m["tcpWER"]["error_rate"],
                    "tcORC-WER": m["tcORC_WER"]["error_rate"],
                    **{f"final_{k}_ms": m["final_latency_ms"][k] for k in ("p50", "p95", "max")},
                }
            )
    if all_errors:
        with (output / "errors.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(all_errors[0]))
            writer.writeheader()
            writer.writerows(all_errors)
    substitutions = Counter(
        (e["gt_word"], e["asr_word"])
        for e in all_errors
        if e["type"] == "S" and e["metric"] == "tcORC-WER"
    )
    save(
        output / "substitutions.json",
        [{"gt": r, "asr": h, "count": c} for (r, h), c in substitutions.most_common()],
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.root, args.output)
