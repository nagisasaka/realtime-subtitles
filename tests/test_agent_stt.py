import json
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
from conftest import make_unit

from realtime_subtitles.agent_stt import AgentSttClient
from realtime_subtitles.audio_recording import AudioRecorder
from realtime_subtitles.final_history import FinalHistory
from realtime_subtitles.live_client import LiveClient
from realtime_subtitles.translation_history import TranslationHistory, TranslationUnit


def agent_event(text, start=0, speaker="S1", partial=False):
    return {
        "message": "AddPartialSegment" if partial else "AddSegment",
        "segment": {"transcript": text, "speaker": speaker},
        "metadata": {"start_time": start, "end_time": start + 2},
    }


def test_low_level_word_finals_cannot_create_translation_jobs():
    client = LiveClient(recorder_factory=None)
    client.speechmatics = SimpleNamespace(session_id="a")
    submitted = []
    client.translation = SimpleNamespace(submit=submitted.append)
    # Hundreds of word events remain isolated diagnostic metadata.
    for i in range(2000):
        client._receive(
            {
                "message": "AddTranscript",
                "metadata": {"transcript": "word ", "start_time": i},
                "results": [{"type": "word", "alternatives": [{"content": "word"}]}],
            },
            (),
        )
    assert not client.history.segments() and not submitted
    for text in ("The biggest", "The biggest challenge is reliability."):
        client._receive(agent_event(text, partial=True), ())
    assert client.history.display_snapshot()[3] == "The biggest challenge is reliability."
    assert not submitted
    for i in range(7):
        client._receive(agent_event(f"Entire server sentence {i}.", i * 2), ())
    assert len(submitted) == 7 and all(isinstance(x, TranslationUnit) for x in submitted)
    assert len(client.history.word_metadata) == 2000
    assert [x["text"] for x in client.history.context_for(6)] == [
        f"Entire server sentence {i}." for i in range(1, 6)
    ]
    assert not hasattr(submitted[0], "words")
    assert client.history.display_snapshot()[3] == ""


def test_legacy_audit_one_message_not_one_word():
    history = FinalHistory()
    message = {
        "message": "AddTranscript",
        "metadata": {"transcript": "Hello world."},
        "results": [
            {"type": "word", "alternatives": [{"content": "Hello", "speaker": "S1"}]},
            {"type": "word", "alternatives": [{"content": "world", "speaker": "S1"}]},
            {"type": "punctuation", "alternatives": [{"content": "."}]},
        ],
    }
    result = history.add_final(message, "audit")
    assert len(history.segments()) == 1 and result.en_text == "Hello world."
    assert len(result.words) == 3


def test_agent_resampling_stream_duration_and_frequency():
    c = AgentSttClient()
    c._new_sdk("test-key")
    source = (np.sin(2 * np.pi * 440 * np.arange(24000) / 24000) * 10000).astype("<i2")
    chunks = [c._encode_audio(source[i : i + 4800].tobytes()) for i in range(0, 24000, 4800)]
    converted = np.frombuffer(b"".join(chunks) + c._flush_audio(), dtype="<i2")
    assert len(converted) == 16000
    assert np.argmax(abs(np.fft.rfft(converted))) == 440


def test_unit_raw_word_journal_separation_and_duplicate_final(tmp_path):
    h = TranslationHistory()
    event = agent_event("The server owns this entire text.", 12.34, "S2")
    unit = make_unit(h, event, "s")
    assert unit.start_ms == 12340 and unit.end_ms == 14340
    h.record_word_metadata({"message": "AddTranscript", "results": []}, "s")
    assert make_unit(h, event, "s") is None
    assert len(h.segments()) == 1
    h.update_translation(0, "completed", text="サーバーの文章。")
    journal, _ = h.autosave_updates(0)
    assert [x["kind"] for x in journal] == [
        "raw_source_segment",
        "translation_unit",
        "raw_word_metadata",
        "translation_unit",
    ]
    h.save(tmp_path / "units.jsonl")
    rows = [
        json.loads(x) for x in (tmp_path / "units.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1 and rows[0]["en_text"] == event["segment"]["transcript"]


def test_recording_rotates_and_keeps_exact_pcm_without_second_mic(tmp_path):
    recorder = AudioRecorder(tmp_path, rotate_seconds=1)
    pcm = np.arange(48000, dtype=np.int16).astype("<i2").tobytes()
    for start in range(0, len(pcm), 9600):
        recorder.put_latest((time.monotonic(), pcm[start : start + 9600]))
    assert recorder.close()
    recovered = b""
    for name in recorder.files:
        with wave.open(str(recorder.directory / name)) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
            assert wav.getnframes() == 24000
            recovered += wav.readframes(wav.getnframes())
    assert len(recorder.files) == 2 and recovered == pcm
    assert not recorder.error


def test_recording_overflow_is_nonblocking_and_gaps_are_explicit(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = AudioRecorder._run

    def delayed(self):
        started.set()
        release.wait(2)
        original(self)

    monkeypatch.setattr(AudioRecorder, "_run", delayed)
    r = AudioRecorder(tmp_path, queue_size=1)
    assert started.wait(1)
    r.put_latest((0, b"\x01\x00" * 4800))
    r.put_latest((0, b"\x02\x00" * 4800))
    assert r.dropped_samples == 4800  # Producer returned while disk worker was paused.
    release.set()
    assert r.close()
    with wave.open(str(r.directory / r.files[0])) as w:
        assert w.readframes(9600) == b"\x01\x00" * 4800 + bytes(9600)
    gap = json.loads((r.directory / "gaps.jsonl").read_text())
    assert gap["start_sample"] == 4800 and gap["end_sample"] == 9600


def test_recording_disk_failure_stays_in_recorder(tmp_path):
    file = tmp_path / "not-directory"
    file.write_text("file")
    r = AudioRecorder(file)
    r.put_latest((0, bytes(9600)))
    assert not r.close() and r.error
    # Capture can keep feeding even after failure, without queue growth or exceptions.
    before = r.frames.qsize()
    for _ in range(200):
        r.put_latest((0, bytes(9600)))
    assert r.frames.qsize() == before
