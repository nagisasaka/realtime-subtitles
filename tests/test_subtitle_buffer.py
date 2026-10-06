import json

from realtime_subtitles.subtitle_buffer import TranscriptHistory, rolling_text


def test_delta_concatenation_duplicate_elapsed_and_history(tmp_path):
    history = TranscriptHistory()
    for text in ["We", "'re", " here", "."]:
        history.append("en", text, 200, "session1")
    history.append("ja", "ここに", 200, "session1")
    history.append("ja", "います。", 400, "session1")
    assert history.full_text("en") == "We're here."
    assert history.full_text("ja") == "ここにいます。"
    assert len(history.records()) == 6
    history.clear_display()
    assert history.display()[1] == {"en": "", "ja": ""}
    assert history.full_text("en") == "We're here."
    path = tmp_path / "history.jsonl"
    history.save(path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["elapsed_ms"] == records[1]["elapsed_ms"]
    assert records[-1]["delta"] == "います。"
    assert records[0]["time"] and records[0]["sequence"] == 0


def test_rolling_buffer_keeps_recent_sentence_and_caps_long_sentence():
    assert rolling_text("Old sentence. Current subtitle", 24) == "Current subtitle"
    assert rolling_text("あ" * 1000, 100) == "あ" * 100
    assert rolling_text("Short") == "Short"


def test_display_bounded_history_lossless():
    history = TranscriptHistory()
    for _ in range(500):
        history.append("en", "abc def ghi.")
    assert len(history.display()[1]["en"]) <= 4000
    assert history.full_text("en") == "abc def ghi." * 500
