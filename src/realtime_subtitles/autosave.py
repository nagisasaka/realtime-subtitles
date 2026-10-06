"""Independent, incremental transcript persistence. Never touches audio or network workers."""

import json
import os
import threading
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

POLL_SECONDS = 0.5
TEXT_SECONDS = 5.0


def default_directory():
    return Path.home() / "RealtimeSubtitles" / "Autosave"


def render_segments(segments):
    parts = []
    speaker = session = None
    for segment in segments:
        known = segment.speaker if segment.speaker not in {None, "UU", "SU", ""} else None
        if parts and (
            (session and segment.session_id != session) or (known and speaker and known != speaker)
        ):
            parts.append("\n\n")
        parts.append(segment.text)
        if known:
            speaker = known
        session = segment.session_id
    return "".join(parts)


class TranscriptAutosave:
    """Poll lossless histories, not the diagnostic queues (which may drop events).

    Cursors advance only after flush + fsync. Failed batches are rolled back to
    their last durable byte offset on retry, so partial writes cannot duplicate
    final records. A crash can leave a truncated last JSONL line; earlier lines
    remain readable. Partial snapshots are best-effort, never promoted to finals.
    """

    def __init__(self, histories, directory=None):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
        self.directory = Path(directory or default_directory()) / stamp
        self.histories = dict(histories)
        self.cursors = dict.fromkeys(histories, 0)
        self.offsets = dict.fromkeys(histories, 0)
        self.metadata = {}
        self.errors = {}
        self.last_saved_at = None
        self._text_due = dict.fromkeys(histories, 0.0)
        self._dirty_text = set(histories)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="transcript-autosave", daemon=True)
        self._thread.start()

    @property
    def error(self):
        return " / ".join(self.errors.copy().values())

    @property
    def active(self):
        return self._thread.is_alive()

    def snapshot(self):
        return {
            "directory": str(self.directory),
            "error": self.error,
            "last_saved_at": self.last_saved_at,
            "active": self.active,
        }

    def request_close(self):
        self._stop.set()

    def close(self, timeout=5):
        self.request_close()
        self._thread.join(timeout)
        return not self.active and not self.error

    def _batch(self, provider, history):
        if provider == "openai":
            records = history.records(self.cursors[provider])
            revision, boundaries = history.speaker_boundaries.snapshot()
            metadata = {
                "kind": "speaker_boundaries_snapshot",
                "revision": revision,
                "boundaries": [asdict(b) for b in boundaries],
            }
        else:
            records, partials, session = history.autosave_records(self.cursors[provider])
            metadata = {
                "kind": "partial_snapshot",
                "session_id": session,
                "partials": {k: [asdict(s) for s in v] for k, v in partials.items()},
            }
            records = [asdict(s) for s in records]
        batch = [{"kind": "transcript", "backend": provider, "record": r} for r in records]
        if metadata != self.metadata.get(provider):
            batch.append({"backend": provider, **metadata})
        return records, metadata, batch

    def _append(self, path, offset, payload):
        # Only this worker owns this unique file. On retry discard an incomplete batch.
        with path.open("r+b" if path.exists() else "x+b") as output:
            output.seek(offset)
            output.truncate()
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
            return output.tell()

    def _export_text(self, provider, history):
        if provider == "openai":
            records = history.rendered_records()
            texts = {
                lang: "".join(r["rendered_delta"] for r in records if r["language"] == lang)
                for lang in ("en", "ja")
            }
        else:
            _, finals, _ = history.snapshot()
            texts = {
                lang: render_segments([s for s in finals if s.language == lang])
                for lang in ("en", "ja")
            }
        path = self.directory / f"{provider}.txt"
        temporary = path.with_suffix(".txt.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            for lang, text in texts.items():
                output.write(f"{lang.upper()}:\n{text}\n\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def _cycle(self):
        for provider, history in self.histories.items():
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                records, metadata, batch = self._batch(provider, history)
                if batch:
                    now = datetime.now(UTC).isoformat()
                    payload = "".join(
                        json.dumps({"saved_at": now, **r}, ensure_ascii=False) + "\n" for r in batch
                    ).encode("utf-8")
                    offset = self._append(
                        self.directory / f"{provider}.jsonl", self.offsets[provider], payload
                    )
                    self.offsets[provider] = offset
                    self.cursors[provider] += len(records)
                    self.metadata[provider] = metadata
                    self.last_saved_at = now
                    self._dirty_text.add(provider)
                if provider in self._dirty_text and (
                    self._stop.is_set() or time.monotonic() >= self._text_due[provider]
                ):
                    self._export_text(provider, history)
                    self._dirty_text.discard(provider)
                    self._text_due[provider] = time.monotonic() + TEXT_SECONDS
                self.errors.pop(provider, None)
            except Exception as exc:
                # No raw exception/response text (could contain secrets).
                self.errors[provider] = f"自動保存エラー ({provider}): {type(exc).__name__}"

    def _run(self):
        while True:
            closing = self._stop.is_set()
            self._cycle()
            if closing and not self.error:
                return
            if closing:
                time.sleep(POLL_SECONDS)
            else:
                self._stop.wait(POLL_SECONDS)
