"""Summarize raw EN/JA receive gaps without printing transcript content."""

import argparse
import json
from datetime import datetime
from pathlib import Path


def analyze(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    groups = {}
    for row in rows:
        key = (row.get("session_id"), row.get("source_model", row.get("phase")))
        groups.setdefault(key, []).append(row)
    output = []
    for (session, model), events in groups.items():
        events = [r for r in events if r.get("type") and r.get("received_at")]
        if not events:
            continue
        en = [
            r
            for r in events
            if r["type"]
            in {
                "session.input_transcript.delta",
                "conversation.item.input_audio_transcription.delta",
            }
        ]
        ja = [r for r in events if r["type"] == "session.output_transcript.delta"]
        spans = []
        # Include start-to-first and last-to-end windows, including a wholly absent EN stream.
        boundaries = [events[0], *en, events[-1]]
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            begin, end = left["received_at"], right["received_at"]
            seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(begin)).total_seconds()
            translated = [r for r in ja if begin <= r["received_at"] <= end]
            if seconds >= 5 and translated:
                spans.append(
                    {
                        "from_utc": begin,
                        "to_utc": end,
                        "en_gap_seconds": round(seconds, 3),
                        "ja_events_during_gap": len(translated),
                        "en_elapsed_ms_before": left.get("elapsed_ms") if left in en else None,
                        "en_elapsed_ms_after": right.get("elapsed_ms") if right in en else None,
                        "ja_elapsed_ms_first": translated[0].get("elapsed_ms"),
                        "ja_elapsed_ms_last": translated[-1].get("elapsed_ms"),
                    }
                )
        output.append(
            {
                "session_id": session,
                "source_model": model,
                "from_utc": events[0]["received_at"],
                "to_utc": events[-1]["received_at"],
                "raw_en_events": len(en),
                "raw_ja_events": len(ja),
                "source_gaps_with_translation": spans,
            }
        )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args()
    print(json.dumps(analyze(args.path), ensure_ascii=False, indent=2))
