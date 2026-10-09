import json
from dataclasses import asdict

import pytest
from test_lecture_summary import FakeAssistant, source, wait

from realtime_subtitles.lecture_summary import LectureNotes, build_snapshot
from realtime_subtitles.settings import Settings
from realtime_subtitles.summary_archive import collect_summary_sources
from realtime_subtitles.translation_history import TranslationHistory


def archive(path, sources):
    lines = []
    for item in sources:
        record = {"kind": "raw_source_segment", **asdict(item)}
        record.pop("grouping_speaker")  # the deployed app's old log schema
        lines.append(json.dumps({"kind": "transcript", "record": record}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_saved_raw_plus_new_session_are_read_only_deduplicated_and_traceable(tmp_path):
    old = TranslationHistory()
    source(old, "Prior speaker.", "S1", "old")
    source(old, "Unknown continuation.", "UU", "old")
    log = archive(tmp_path / "old.jsonl", old.sources())
    # A duplicate copied log cannot count the same utterance twice.
    copied = tmp_path / "copy.jsonl"
    copied.write_bytes(log.read_bytes())
    original = log.read_bytes()
    new = TranslationHistory()
    source(new, "Restarted without a speaker.", "UU", "new")
    source(new, "New person using S1.", "S1", "new")
    sources, origins, logs = collect_summary_sources(new.sources(), [log, copied, log])
    assert len(sources) == 4 and len(logs) == 2
    assert [s.speaker for s in sources] == ["S1", "UU", "UU", "S1"]
    assert [s.effective_speaker for s in sources] == ["S1", "S1", "UU", "S1"]
    assert [s.segment_id for s in sources] == [0, 1, 2, 3]
    assert [o["source_segment_id"] for o in origins] == [0, 1, 0, 1]
    assert origins[0]["log_key"] and origins[2]["log_key"] is None
    snapshot = build_snapshot(sources, source_origins=origins, input_logs=logs)
    assert len(snapshot.speakers) == 3
    assert "セッション 1" in snapshot.speakers[0].label
    assert "セッション 2" in snapshot.speakers[-1].label
    assert snapshot.metadata()["source_origins"] == origins
    assert log.read_bytes() == original
    assert len(new.sources()) == 2 and not new.segments()


def test_live_log_duplicate_and_partial_trailing_write(tmp_path):
    h = TranslationHistory()
    source(h, "Exact once.")
    log = archive(tmp_path / "live.jsonl", h.sources())
    with log.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "unfinished')
    sources, _, _ = collect_summary_sources(h.sources(), [log])
    assert len(sources) == 1 and sources[0].en_text == "Exact once."


def test_invalid_complete_line_or_missing_file_is_not_silently_skipped(tmp_path):
    log = tmp_path / "bad.jsonl"
    log.write_text("broken JSON\n", encoding="utf-8")
    with pytest.raises(ValueError):
        collect_summary_sources([], [log])
    with pytest.raises(FileNotFoundError):
        collect_summary_sources([], [tmp_path / "missing.jsonl"])


def test_archive_only_notes_do_not_restore_audio_or_translation_jobs(tmp_path):
    old = TranslationHistory()
    source(old, "Retained lecture.")
    log = archive(tmp_path / "prior.jsonl", old.sources())
    current = TranslationHistory()
    fake = FakeAssistant()
    notes = LectureNotes(
        current,
        assistant_factory=fake,
        key_provider=lambda: "test-key",
        log_paths=[log],
    )
    assert not fake.calls
    assert notes.regenerate()
    wait(notes)
    assert not notes.error and notes.summary["snapshot"]["source_count"] == 1
    assert len(notes.summary["snapshot"]["input_logs"]) == 1
    assert not current.sources() and not current.segments()
    assert notes.generate_questions()
    wait(notes)
    assert notes.questions["summary_id"] == notes.summary["id"]
    assert notes.set_log_paths([])
    assert not notes.regenerate() and "確定英文" in notes.error


def test_log_failure_does_not_send_incomplete_content_to_api(tmp_path):
    h = TranslationHistory()
    source(h, "New live English.")
    fake = FakeAssistant()
    notes = LectureNotes(
        h,
        assistant_factory=fake,
        key_provider=lambda: "test-key",
        log_paths=[tmp_path / "missing.jsonl"],
    )
    assert notes.regenerate()
    wait(notes)
    assert "FileNotFoundError" in notes.error and not fake.calls
    assert h.subtitle_view().en_text == "New live English."


def test_import_cancellation_and_settings_roundtrip(tmp_path):
    h = TranslationHistory()
    source(h, "Prior speech.")
    log = archive(tmp_path / "prior.jsonl", h.sources())
    with pytest.raises(InterruptedError):
        collect_summary_sources([], [log], cancelled=lambda: True)
    path = tmp_path / "settings.json"
    Settings(summary_logs=[str(log)]).save(path)
    assert Settings.load(path).summary_logs == [str(log)]
    path.write_text(json.dumps({"summary_logs": [None, {}, str(log), str(log), ""]}))
    assert Settings.load(path).summary_logs == [str(log)]
