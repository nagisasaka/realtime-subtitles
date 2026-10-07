"""Synthetic fixtures only: no corpus, mic, network, credentials or cloud calls."""

import asyncio
import io
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

# Benchmarks are repo scripts, deliberately excluded from the distributable GUI wheel.
# Support both `pytest` console entrypoint and `python -m pytest`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("meeteval")
from benchmarks.notsofar1.common import normalizer
from benchmarks.notsofar1.download import choose_device
from benchmarks.notsofar1.evaluate import parse_gt, parse_hyp, score_meeting
from benchmarks.notsofar1.prepare import convert, wav_info
from benchmarks.notsofar1.run import Recorder, pace, stream


def wav(path, rate=16000, seconds=0.4, channels=1):
    t = np.arange(round(rate * seconds)) / rate
    signal = (10000 * np.sin(2 * np.pi * 440 * t)).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        f.writeframes(np.repeat(signal[:, None], channels, axis=1).tobytes())


def test_gt_preserves_overlaps_and_original_tags():
    rows = [
        {
            "speaker_id": "Alice",
            "start_time": 1,
            "end_time": 4,
            "text": "Um, <FILL/> twenty-five cats.",
            "word_timing": [["cats", 3, 4]],
        },
        {"speaker_id": "Bob", "start_time": 2, "end_time": 3, "text": "Okay!"},
    ]
    parsed = parse_gt(rows, "test", normalizer())
    assert parsed[0]["start_time"] < parsed[1]["start_time"] < parsed[0]["end_time"]
    assert parsed[0]["raw_text"] == rows[0]["text"]
    assert parsed[0]["word_timing"] == rows[0]["word_timing"]
    assert parsed[0]["words"] == "twenty five cats"
    assert parsed[1]["words"] == "ok"


@pytest.mark.parametrize("tag", ["ST", "FILL", "UNKNOWN", "PName", "BA", "PAUSE", "ISSUE"])
def test_official_annotation_rule_removes_tag_not_utterance(tag):
    n = normalizer()
    assert n(f"Hello <{tag}/> world.") == "hello world"
    assert n(f"<{tag}/>") == ""


def test_identical_normalization_numbers_abbreviations():
    n = normalizer()
    assert n("Dr. Smith has $25.") == n("Doctor Smith has twenty five dollars")
    assert n("25 percent") == n("25%")
    assert n(n("Okay! I'm gonna go.")) == n("Okay! I'm gonna go.")


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_wav_conversion_preserves_duration_and_original(tmp_path, rate):
    source, target = tmp_path / "original.wav", tmp_path / "prepared.wav"
    wav(source, rate, 1.234)
    before = source.read_bytes()
    a, b = convert(source, target)
    assert source.read_bytes() == before
    assert b["sample_rate"] == 16000 and b["channels"] == 1 and b["sample_width"] == 2
    assert abs(a["duration"] - b["duration"]) <= 1 / 16000
    if rate == 16000:
        assert target.read_bytes() == before


def test_no_downmix_or_close_talk(tmp_path):
    source = tmp_path / "stereo.wav"
    wav(source, channels=2)
    with pytest.raises(ValueError):
        convert(source, tmp_path / "target.wav")
    with pytest.raises(ValueError):
        choose_device([{"is_close_talk": True, "is_mc": False, "channels_num": 1}])


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, duration):
        self.now += duration
        await asyncio.sleep(0)


def test_absolute_pacing_no_early_send_no_cumulative_drift():
    async def scenario():
        clock = Clock()
        epoch = clock()
        for i in range(1, 101):
            clock.now += 0.031  # variable work never added to next deadline
            await pace(epoch, i * 0.2, clock, clock.sleep)
            assert clock() >= epoch + i * 0.2
        assert clock() == pytest.approx(epoch + 20)
        clock.now += 1
        assert await pace(epoch, 20.2, clock, clock.sleep) == pytest.approx(0.8)

    asyncio.run(scenario())


def event(text="Hello world.", kind="AddSegment", speaker="S2"):
    return {
        "message": kind,
        "segment": {"transcript": text, "speaker": speaker},
        "metadata": {"start_time": 0, "end_time": 0.4},
    }


def test_recorder_retains_raw_speaker_and_missing_metadata(tmp_path):
    async def scenario():
        recorder = Recorder(tmp_path, "meeting", clock=lambda: 100)
        recorder.session_id = "session"
        recorder.epoch = 99
        raw = event()
        recorder.receive(raw)
        recorder.receive({"message": "SpeechStarted"})
        recorder.receive({"message": "Error", "reason": "secret must not be serialized"})
        recorder.close()
        rows = [json.loads(s) for s in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert rows[0]["raw_event"] == raw and rows[0]["speaker"] == "S2"
        assert rows[0]["audio_end_ms"] == 400 and rows[0]["received_monotonic_ms"] == 100000
        assert rows[1]["audio_start_ms"] is None and rows[1]["transcript"] is None
        assert "secret" not in (tmp_path / "events.jsonl").read_text()
        assert parse_hyp(rows, "meeting", normalizer())[0]["speaker"] == "S2"

    asyncio.run(scenario())


def test_final_collection_at_eos(tmp_path):
    async def scenario():
        source = tmp_path / "source.wav"
        wav(source)
        clock = Clock()
        recorder = Recorder(tmp_path, "meeting", clock)

        class Client:
            is_ready_for_audio = True
            sent = []

            async def send_audio(self, chunk):
                self.sent.append(chunk)

            async def send_message(self, message):
                assert message == {"message": "EndOfStream", "last_seq_no": 2}
                # Final exists ONLY after EOS: must not be lost.
                recorder.receive(event())
                recorder.receive({"message": "EndOfTranscript"})

        c = Client()
        log = io.StringIO()
        stats = await stream(c, source, recorder, log, clock=clock, sleep=clock.sleep)
        recorder.close()
        assert stats["eos_received"] and recorder.final_count == 1
        assert len(c.sent) == 2 and sum(map(len, c.sent)) == wav_info(source)["frames"] * 2
        assert len(log.getvalue().splitlines()) == 2

    asyncio.run(scenario())


def test_abnormal_termination_not_retried(tmp_path):
    async def scenario():
        source = tmp_path / "source.wav"
        wav(source)
        clock = Clock()
        recorder = Recorder(tmp_path, "meeting", clock)

        class Client:
            is_ready_for_audio = True
            calls = 0

            async def send_audio(self, chunk):
                self.calls += 1
                self.is_ready_for_audio = False

        client = Client()
        with pytest.raises(ConnectionError):
            await stream(client, source, recorder, io.StringIO(), clock=clock, sleep=clock.sleep)
        recorder.close()
        assert client.calls == 1

    asyncio.run(scenario())


def row(text, speaker, start=1, end=2):
    return {
        "session_id": "test",
        "speaker": speaker,
        "start_time": start,
        "end_time": end,
        "words": text,
        "raw_text": text,
        "utterance_id": start,
    }


def test_wer_counts_and_optimal_speaker_assignment():
    ref = [row("the red cat", "Alice"), row("hello world", "Bob", 4, 5)]
    hyp = [row("the blue cat", "S9"), row("hello world", "S1", 4, 5)]
    scores, errors = score_meeting(ref, hyp, 10)
    assert scores["tcpWER"]["error_rate"] == pytest.approx(1 / 5)
    assert scores["tcORC_WER"]["error_rate"] == pytest.approx(1 / 5)
    assert scores["tcpWER"]["substitutions"] == 1
    assert scores["ordinary_WER_nonoverlap"]["error_rate"] == pytest.approx(1 / 5)
    assert errors[0]["gt_word"] == "red" and errors[0]["asr_word"] == "blue"
    assert ("Alice", "S9") in scores["tcpWER"]["assignment"]


def test_overlap_not_forced_into_chronological_wer():
    ref = [row("one two", "Alice", 1, 3), row("three four", "Bob", 1, 3)]
    hyp = [row("three four", "S1", 1, 3), row("one two", "S2", 1, 3)]
    scores, _ = score_meeting(ref, hyp, 10)
    assert scores["tcpWER"]["error_rate"] == 0
    assert scores["tcORC_WER"]["error_rate"] == 0
    assert scores["ordinary_WER_nonoverlap"]["error_rate"] is None
    assert scores["gt_overlap_seconds"] == 2


def test_eos_timeout_is_failure_and_preserves_already_received_final(tmp_path):
    async def scenario():
        source = tmp_path / "source.wav"
        wav(source, seconds=0.2)
        clock = Clock()
        recorder = Recorder(tmp_path, "meeting", clock)

        class Client:
            is_ready_for_audio = True

            async def send_audio(self, chunk):
                recorder.receive(event())

            async def send_message(self, message):
                pass  # Never sends EndOfTranscript.

        with pytest.raises(TimeoutError):
            await stream(
                Client(),
                source,
                recorder,
                io.StringIO(),
                clock=clock,
                sleep=clock.sleep,
                eos_timeout=0.005,
            )
        recorder.close()
        assert len((tmp_path / "transcript.jsonl").read_text().splitlines()) == 1
        assert not recorder.ended.is_set()

    asyncio.run(scenario())


def test_late_final_is_collected_before_end_of_transcript(tmp_path):
    async def scenario():
        source = tmp_path / "source.wav"
        wav(source, seconds=0.2)
        clock = Clock()
        recorder = Recorder(tmp_path, "meeting", clock)

        class Client:
            is_ready_for_audio = True

            async def send_audio(self, chunk):
                pass

            async def send_message(self, message):
                async def deliver():
                    await asyncio.sleep(0)
                    recorder.receive(event("Last word."))
                    recorder.receive({"message": "EndOfTranscript"})

                asyncio.create_task(deliver())

        stats = await stream(
            Client(), source, recorder, io.StringIO(), clock=clock, sleep=clock.sleep
        )
        recorder.close()
        assert stats["eos_received"] and recorder.final_count == 1

    asyncio.run(scenario())


def test_run_lock_rejects_concurrent_writers_and_cleans_up(tmp_path):
    from benchmarks.notsofar1.run import exclusive_run

    with exclusive_run(tmp_path):
        with pytest.raises(RuntimeError):
            with exclusive_run(tmp_path):
                pytest.fail("Second writer must not run")
    assert not (tmp_path / ".run.lock").exists()


def test_no_openai_dependency_in_runner():
    # Import in a fresh Python process, separate from app tests importing translation modules.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import benchmarks.notsofar1.run, sys; assert "openai" not in sys.modules',
        ],
        capture_output=True,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("already_attempted,budget", [(True, 0), (False, 1800)])
def test_budget_and_duplicate_guards_before_api(tmp_path, monkeypatch, already_attempted, budget):
    from benchmarks.notsofar1 import run as runner
    from benchmarks.notsofar1.common import REVISION, save, sha256

    root, output = tmp_path / "data", tmp_path / "results"
    root.mkdir()
    audio = root / "a.wav"
    wav(audio, seconds=0.2)
    mid = "MTG_test"
    save(
        root / "prepared.json",
        {
            "revision": REVISION,
            "meetings": [
                {
                    "meeting_id": mid,
                    "prepared_audio": "a.wav",
                    "prepared_format": wav_info(audio),
                    "prepared_audio_sha256": sha256(audio),
                }
            ],
        },
    )
    save(
        output / "manifest.json",
        {
            "dataset_revision": REVISION,
            "reserved_audio_seconds": budget,
            "attempts": [{"meeting_id": mid, "status": "failed"}] if already_attempted else [],
        },
    )
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "fixture-only")

    def forbidden():
        pytest.fail("API client must never be constructed for rejected input")

    monkeypatch.setattr(runner, "AgentSttClient", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(runner.run(root, output, [mid]))
    assert not (output / "events.jsonl").exists()
