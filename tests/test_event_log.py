import json
from datetime import datetime

from realtime_subtitles.event_log import EventJournal


def test_raw_transcript_is_preserved_with_receive_time_and_alignment(tmp_path):
    path = tmp_path / "events.jsonl"
    raw = '{ "type": "session.input_transcript.delta", "delta": " hello", "elapsed_ms": 200 }'
    journal = EventJournal(path)
    journal.record(raw, session_id="test-session", model="gpt-live-transcribe")
    journal.record(raw, session_id="test-session", model="gpt-live-transcribe")
    journal.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2  # Repeated elapsed_ms is not an event identifier.
    assert rows[0]["raw_server_event"] == raw
    assert rows[0]["elapsed_ms"] == 200
    assert rows[0]["session_id"] == "test-session"
    assert rows[0]["source_model"] == "gpt-live-transcribe"
    assert datetime.fromisoformat(rows[0]["received_at"]).tzinfo is not None
    assert rows[1]["received_monotonic_ns"] >= rows[0]["received_monotonic_ns"]
    assert journal.dropped == 0 and not journal.error


def test_journal_excludes_audio_payload_and_session_credentials(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = EventJournal(path)
    for event in [
        {"type": "session.output_audio.delta", "delta": "secret-audio-bytes"},
        {"type": "session.created", "session": {"id": "test", "client_secret": "secret-key"}},
        {"type": "error", "error": {"code": "test", "message": "Authorization: secret-key"}},
    ]:
        journal.record(json.dumps(event))
    journal.close()
    text = path.read_text()
    assert "secret" not in text
    assert "Authorization" not in text
    assert len(text.splitlines()) == 3
