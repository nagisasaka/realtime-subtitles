import hashlib
import json
import wave
from copy import deepcopy

import pytest

from benchmarks.history_readability.cross_audio import audio_info, legacy_windows


def report(finals=4):
    return {
        "backends": {
            "agent": {
                "finals": finals,
                "status": {
                    "model": "linden-1",
                    "state": "STOPPED",
                    "error": "",
                    "dropped_frames": 0,
                    "session_id": "session",
                },
            }
        }
    }


def events():
    return [
        {
            "message": "AddSegment",
            "segment": {"transcript": text, "speaker": "S1"},
            "metadata": {"start_time": i, "end_time": i + 0.5},
        }
        for i, text in enumerate(("The system", "is ready.", "It can", "start now."))
    ]


def test_legacy_replay_keeps_audio_times_and_source_but_does_not_invent_receive_time():
    raw = events()
    original = deepcopy(raw)
    windows = legacy_windows(raw, report())
    assert raw == original
    assert len(windows) == 1
    w = windows[0]
    assert w["english"] == "The system is ready. It can start now."
    assert w["start_ms"] == 0 and w["end_ms"] == 3500
    assert "not logged" in w["eos"]
    for i, source in enumerate(w["raw_sources"]):
        assert source["received_monotonic_ms"] is None
        assert source["received_at"] is None
        assert json.loads(source["raw_event_json"]) == raw[i]


def test_legacy_report_must_match_complete_agent_log():
    for key, value in (("model", "enhanced"), ("state", "ERROR"), ("dropped_frames", 1)):
        r = report()
        r["backends"]["agent"]["status"][key] = value
        with pytest.raises(ValueError, match="completed"):
            legacy_windows(events(), r)
    with pytest.raises(ValueError, match="count"):
        legacy_windows(events(), report(finals=5))


def test_partial_speaker_boundary_is_not_crossed_in_legacy_replay():
    raw = events()
    raw.insert(
        1,
        {
            "message": "AddPartialSegment",
            "segment": {"transcript": "interruption", "speaker": "S2"},
        },
    )
    for event in raw[2:]:
        event["segment"]["speaker"] = "S2"
    windows = legacy_windows(raw, report())
    assert len(windows) == 1
    assert windows[0]["source_ids"] == (1, 2, 3)
    assert windows[0]["speaker"] == "S2"
    assert windows[0]["context"] == [{"speaker": "S1", "text": "The system"}]


def test_audio_must_match_asr_recording_sha(tmp_path):
    path = tmp_path / "original.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    info = audio_info(path, sha)
    assert info["duration_seconds"] == 0.1
    assert info["sample_rate"] == 16000
    with pytest.raises(ValueError, match="match"):
        audio_info(path, "wrong")
