"""Read saved finals for lecture notes only; never replay them into live ASR/translation."""

import hashlib
import json
from dataclasses import fields, replace
from pathlib import Path

from .translation_history import SourceSegment


def collect_summary_sources(live_sources, log_paths, *, cancelled=lambda: False):
    """Return private snapshot IDs plus their original log/session/source provenance.

    Leave saved labels/text and live SourceSegment objects unchanged. The summary
    snapshot applies its own continuous-conversation speaker grouping later.
    """
    entries, logs = [], []
    names = {field.name for field in fields(SourceSegment)}
    for filename in dict.fromkeys(log_paths):
        path = Path(filename)
        log_key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
        log_name = f"{path.parent.name}/{path.name}"
        count = 0
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if cancelled():
                    raise InterruptedError("Summary import cancelled")
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break  # A currently running/crashed writer may leave a torn final line.
                    raise
                row = row.get("record", row)
                if row.get("kind") != "raw_source_segment":
                    continue
                source = SourceSegment(**{k: v for k, v in row.items() if k in names})
                if (
                    not isinstance(source.en_text, str)
                    or not isinstance(source.segment_id, int)
                    or source.speaker is not None
                    and not isinstance(source.speaker, str)
                    or not isinstance(source.received_at, str)
                ):
                    raise ValueError("Invalid archived source")
                entries.append((source, {"log_key": log_key, "log_name": log_name}))
                count += 1
        if not count:
            raise ValueError("No raw confirmed sources in the selected log")
        logs.append({"log_key": log_key, "log_name": log_name, "source_count": count})
    entries.extend((s, {"log_key": None, "log_name": None}) for s in live_sources)
    # All native autosaves use UTC ISO timestamps. Arrival order within a log is stable.
    entries.sort(key=lambda item: item[0].received_at)
    sources, origins, seen = [], [], set()
    for source, origin in entries:
        if cancelled():
            raise InterruptedError("Summary import cancelled")
        identity = (
            source.session_id or origin["log_key"],
            source.segment_id,
            source.start_ms,
            source.end_ms,
            source.en_text,
            source.speaker,
        )
        if identity in seen:
            continue
        seen.add(identity)
        snapshot_id = len(sources)
        sources.append(replace(source, segment_id=snapshot_id))
        origins.append(
            {
                "snapshot_source_id": snapshot_id,
                "source_segment_id": source.segment_id,
                "session_id": source.session_id,
                **origin,
            }
        )
    return sources, tuple(origins), tuple(logs)
