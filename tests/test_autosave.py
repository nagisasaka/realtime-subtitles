import json
import time
from dataclasses import replace

from realtime_subtitles.autosave import TranscriptAutosave
from realtime_subtitles.speaker_timeline import Anchor, SpeakerBoundary
from realtime_subtitles.speechmatics_api import SegmentHistory
from realtime_subtitles.subtitle_buffer import TranscriptHistory


def until(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def rows(save, provider="openai"):
    return [
        json.loads(line)
        for line in (save.directory / f"{provider}.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_automatic_live_save_backfill_clear_restart_and_close(tmp_path):
    history = TranscriptHistory()
    history.append("en", "We're here.", session_id="first")
    save = TranscriptAutosave({"openai": history}, tmp_path)
    try:
        until(lambda: save.cursors["openai"] == 1)
        assert rows(save)[0]["record"]["delta"] == "We're here."
        history.clear_display()
        history.append("ja", "ここにいます。", 200, "first")
        history.append("en", " Again.", session_id="second")
    finally:
        assert save.close()
    records = [r["record"] for r in rows(save) if r["kind"] == "transcript"]
    assert len(records) == 3
    assert records[1]["elapsed_ms"] == 200
    text = (save.directory / "openai.txt").read_text(encoding="utf-8")
    assert "We're here. Again." in text and "ここにいます。" in text
    next_save = TranscriptAutosave({"openai": TranscriptHistory()}, tmp_path)
    assert next_save.close()
    assert next_save.directory != save.directory
    assert (save.directory / "openai.txt").read_text(encoding="utf-8") == text


def test_partial_snapshot_separate_from_finals_and_metadata(tmp_path):
    history = SegmentHistory()
    history.begin_session("first")
    partial = {
        "message": "AddPartialTranslation",
        "language": "ja",
        "results": [{"content": "仮字幕", "start_time": 1, "end_time": 2, "speaker": "S2"}],
    }
    history.accept(partial, "first")
    save = TranscriptAutosave({"speechmatics": history}, tmp_path)
    try:
        until(lambda: "speechmatics" in save.metadata)
        assert rows(save, "speechmatics")[-1]["partials"]["ja"][0]["text"] == "仮字幕"
        final = {
            **partial,
            "message": "AddTranslation",
            "results": [{**partial["results"][0], "content": "確定字幕。"}],
        }
        history.accept(final, "first")
        history.accept(final, "first")
        history.begin_session(None)
    finally:
        assert save.close()
    records = [r["record"] for r in rows(save, "speechmatics") if r["kind"] == "transcript"]
    assert len(records) == 1
    assert records[0]["start_ms"] == 1000 and records[0]["speaker"] == "S2"
    assert records[0]["is_final"]
    text = (save.directory / "speechmatics.txt").read_text(encoding="utf-8")
    assert "仮字幕" not in text and "確定字幕。" in text


def test_failed_partial_write_retries_without_losing_or_duplicating_finals(tmp_path, monkeypatch):
    history = TranscriptHistory()
    history.append("en", "First.")
    original = TranscriptAutosave._append
    failures = []
    allow = False

    def fail(self, path, offset, payload):
        if not allow:
            with path.open("ab") as output:
                output.write(payload[:23])
            failures.append(True)
            raise OSError("secret must never appear in status")
        return original(self, path, offset, payload)

    monkeypatch.setattr(TranscriptAutosave, "_append", fail)
    save = TranscriptAutosave({"openai": history}, tmp_path)
    try:
        until(lambda: bool(save.error))
        assert save.cursors["openai"] == 0
        assert "secret" not in save.error
        # Capture/history continue normally while disk persistence is failing.
        for i in range(100):
            history.append("ja", f"文{i}。")
        allow = True
        until(lambda: save.cursors["openai"] == 101 and not save.error)
    finally:
        allow = True
        assert save.close()
    records = [r["record"] for r in rows(save) if r["kind"] == "transcript"]
    assert [r["sequence"] for r in records] == list(range(101))
    assert failures


def test_boundary_update_persists_without_overwriting_authoritative_text(tmp_path):
    h = TranscriptHistory()
    text = "What we really need to think about is reliability in production."
    h.append("en", text)
    save = TranscriptAutosave({"openai": h}, tmp_path)
    try:
        until(lambda: save.cursors["openai"] == 1)
        boundary = SpeakerBoundary(
            "one", 1000, "A", "B", confidence=0.9, positions={"en": Anchor(0, 5, 5, 9, 0.9)}
        )
        h.speaker_boundaries.merge(boundary)
        until(lambda: save.metadata["openai"]["revision"] == h.speaker_boundaries.revision)
        h.speaker_boundaries.merge(replace(boundary, audio_time_ms=1100, confidence=0.95))
    finally:
        assert save.close()
    records = rows(save)
    assert [r["record"]["delta"] for r in records if r["kind"] == "transcript"] == [text]
    assert records[-1]["boundaries"][0]["audio_time_ms"] == 1100
    rendered = (save.directory / "openai.txt").read_text(encoding="utf-8")
    assert text[:5] + "\n\n" + text[5:] in rendered
    assert h.full_text("en") == text


def test_disk_write_does_not_block_history_append(tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    original = TranscriptAutosave._append

    def slow(self, *args):
        entered.set()
        release.wait(4)
        return original(self, *args)

    monkeypatch.setattr(TranscriptAutosave, "_append", slow)
    h = TranscriptHistory()
    save = TranscriptAutosave({"openai": h}, tmp_path)
    try:
        assert entered.wait(2)
        completed = threading.Event()

        def append():
            h.append("en", "Audio and subtitles continue.")
            completed.set()

        threading.Thread(target=append).start()
        assert completed.wait(1)
    finally:
        release.set()
        assert save.close()
    assert save.cursors["openai"] == 1


def test_flushed_journal_survives_abrupt_process_exit(tmp_path):
    import subprocess
    import sys

    code = """
import os,sys,time
from realtime_subtitles.autosave import TranscriptAutosave
from realtime_subtitles.subtitle_buffer import TranscriptHistory
h=TranscriptHistory()
h.append('en', 'Already received English.')
h.append('ja', '受信済みの日本語。')
save=TranscriptAutosave({'openai':h}, sys.argv[1])
deadline=time.monotonic()+5
while save.cursors['openai'] != 2:
    if time.monotonic()>deadline: os._exit(2)
    time.sleep(0.02)
os._exit(0)  # No close(), no atexit, no orderly shutdown.
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=10)
    path = next(tmp_path.glob("*/openai.jsonl"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    deltas = [r["record"]["delta"] for r in records if r["kind"] == "transcript"]
    assert deltas == ["Already received English.", "受信済みの日本語。"]
