import asyncio
import json
import struct
import time
import wave

import numpy as np
import pytest
from websockets.asyncio.server import serve

from realtime_subtitles.agent_stt import AgentSttClient
from realtime_subtitles.audio import AudioError
from realtime_subtitles.audio_file import AudioFileSource, RealtimePacer, inspect_wav
from realtime_subtitles.live_client import LiveClient
from realtime_subtitles.text_translation import TranslationWorker


def wav_file(path, rate=48000, channels=2, seconds=0.45):
    count = round(rate * seconds)
    samples = (np.sin(np.arange(count) * 2 * np.pi * 440 / rate) * 10000).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(np.repeat(samples[:, None], channels, axis=1).tobytes())
    return samples


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds
        return False


@pytest.mark.parametrize("rate", [16000, 24000, 44100, 48000])
@pytest.mark.parametrize("channels", [1, 2])
def test_wav_stream_resampling_mono_duration_no_padding(tmp_path, rate, channels):
    path = tmp_path / "voice.wav"
    wav_file(path, rate, channels)
    clock = Clock()
    source = AudioFileSource(path, clock=clock, waiter=clock.wait)
    source.prepare()
    assert source.info["audio_duration_ms"] == 450
    source.start()
    chunks = []
    while source.worker.is_alive() or not source.frames.empty():
        try:
            chunks.append(source.frames.get(timeout=0.1)[1])
        except __import__("queue").Empty:
            pass
    source.check_health()
    source.stop()
    assert source.finished
    assert [len(c) for c in chunks] == [9600, 9600, 2400]
    client = AgentSttClient()
    client._new_sdk("test")
    result = b"".join(client._encode_audio(x) for x in chunks) + client._flush_audio()
    assert len(result) == 7200 * 2  # exactly 450 ms at 16 kHz
    converted = np.frombuffer(result, dtype="<i2")
    assert np.max(converted) == pytest.approx(10000, abs=50)
    assert np.argmax(abs(np.fft.rfft(converted))) == 198  # 440 Hz × .45 s
    assert clock.now == pytest.approx(0.45)


def test_absolute_pacing_and_no_burst_after_stall():
    clock = Clock()
    pacer = RealtimePacer(clock)
    for i in range(1000):
        clock.wait(pacer.delay(0.2))
        pacer.sent()
        assert clock.now == pytest.approx((i + 1) * 0.2)
        clock.now += 0.007  # work does not accumulate into replay speed drift
    clock.now += 2
    late = clock.now
    assert pacer.delay(0.2) == pytest.approx(0.2)
    clock.wait(0.2)
    pacer.sent()
    assert clock.now == pytest.approx(late + 0.2)
    assert pacer.delay(0.2) == pytest.approx(0.2)


def test_stop_restart_and_streaming_large_file(tmp_path, monkeypatch):
    path = tmp_path / "large.wav"
    size = 48000 * 2 * 60 * 60  # sparse one-hour WAV; never load it whole
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        48000,
        96000,
        2,
        16,
        b"data",
        size,
    )
    with path.open("wb") as f:
        f.write(header)
        f.truncate(44 + size)
    calls = []
    original = wave.Wave_read.readframes

    def guarded(self, count):
        calls.append(count)
        assert count <= 9600
        return original(self, count)

    monkeypatch.setattr(wave.Wave_read, "readframes", guarded)
    c = AudioFileSource(path)
    c.prepare()
    for _ in range(2):
        c.start()
        assert not c.start()
        _, frame = c.frames.get(timeout=2)
        assert frame == bytes(9600)
        started = time.monotonic()
        c.stop()
        assert time.monotonic() - started < 0.5
        assert not c.finished and c.sent_samples == 0
    assert len(calls) < 15


def test_bad_missing_and_truncated_wav(tmp_path):
    with pytest.raises(AudioError, match="見つかりません"):
        inspect_wav(tmp_path / "absent.wav")
    path = tmp_path / "bad.wav"
    path.write_bytes(b"not a WAV")
    with pytest.raises(AudioError):
        inspect_wav(path)
    wav_file(path)
    with path.open("r+b") as f:
        f.truncate(48)
    source = AudioFileSource(path)
    source.prepare()
    source.start()
    source.worker.join(1)
    with pytest.raises(AudioError, match="途中"):
        source.check_health()
    source.stop()


@pytest.mark.parametrize("monitor_enabled", [False, True])
def test_file_uses_production_pipeline_eos_restart_and_metadata(
    tmp_path, monkeypatch, monitor_enabled
):
    path = tmp_path / "voice.wav"
    wav_file(path)
    for key in ("OPENAI_API_KEY", "SPEECHMATICS_API_KEY"):
        monkeypatch.setenv(key, "test")
    calls, endings, starts, audio = [], [], [], []
    monitors = []

    def monitor_factory():
        from realtime_subtitles.audio_monitor import AudioMonitor

        def unavailable():
            raise OSError("test output unavailable")

        monitor = AudioMonitor(output_factory=unavailable)
        monitors.append(monitor)
        return monitor

    class Translator:
        def __init__(self, key):
            pass

        async def translate(self, unit, context):
            calls.append((unit, context))
            return "最後の発言。", None

        async def close(self):
            pass

    def no_mic(*args):
        pytest.fail("File input must not open a microphone or a recorder")

    async def run():
        async def server(ws):
            config = json.loads(await ws.recv())
            assert config["transcription_config"]["model"] == "linden-1"
            assert config["audio_format"]["sample_rate"] == 16000
            session = str(len(starts))
            starts.append(config)
            await ws.send(json.dumps({"message": "RecognitionStarted", "id": session}))
            count, data = 0, bytearray()
            async for message in ws:
                if isinstance(message, bytes):
                    count += 1
                    data.extend(message)
                    await ws.send(json.dumps({"message": "AudioAdded", "seq_no": count}))
                    if count == 1:
                        await ws.send(
                            json.dumps(
                                {
                                    "message": "AddSegment",
                                    "metadata": {"start_time": 0, "end_time": 0.2},
                                    "segment": {"speaker": "S1", "transcript": "Beginning."},
                                }
                            )
                        )
                else:
                    eos = json.loads(message)
                    assert eos["message"] == "EndOfStream" and eos["last_seq_no"] == count
                    endings.append(eos)
                    audio.append(bytes(data))
                    await ws.send(
                        json.dumps(
                            {
                                "message": "AddSegment",
                                "metadata": {"start_time": 0.2, "end_time": 0.45},
                                "segment": {"speaker": "S1", "transcript": "Last final."},
                            }
                        )
                    )
                    await ws.send(json.dumps({"message": "EndOfTranscript"}))

        async with serve(server, "127.0.0.1", 0) as sock:
            endpoint = f"ws://127.0.0.1:{sock.sockets[0].getsockname()[1]}"
            client = LiveClient(
                microphone_factory=no_mic,
                monitor_factory=monitor_factory,
                recorder_factory=no_mic,
                speechmatics_factory=lambda **kw: AgentSttClient(endpoint=endpoint, **kw),
                translation_factory=lambda h, k: TranslationWorker(
                    h, k, translator_factory=Translator
                ),
            )
            for _ in range(2):
                started = time.monotonic()
                assert client.start(audio_file=str(path), audio_monitor=monitor_enabled)
                assert not client.start(audio_file=str(path))
                assert await asyncio.to_thread(client.join, 5)
                assert time.monotonic() - started >= 0.45
                assert client.playback_state == "Finished", client.snapshot()
                assert client.snapshot()["position_ms"] == 450
            assert len(endings) == 2 and len(calls) == 2
            assert all(unit.en_text == "Beginning. Last final." for unit, _ in calls)
            assert all(len(unit.source_segment_ids) == 2 for unit, _ in calls)
            assert len(monitors) == (2 if monitor_enabled else 0)
            assert all(m.error and not m.thread.is_alive() for m in monitors)

            assert [len(x) for x in audio] == [14400, 14400]
            assert audio[0] == audio[1]
            assert all(context == [] for _, context in calls)
            assert client.history.statistics() == {"completed": 2}
            assert client.history.subtitle_view().unit_id == 1
            rows, _ = client.history.autosave_updates(0)
            sessions = [r for r in rows if r["kind"] == "input_session"]
            assert len(sessions) == 2
            assert all(
                r["filename"] == "voice.wav" and r["audio_sample_rate"] == 48000 for r in sessions
            )
            assert str(tmp_path) not in json.dumps(sessions)

    asyncio.run(run())


def test_stop_during_file_handshake_is_idle_not_eos_error(tmp_path, monkeypatch):
    path = tmp_path / "voice.wav"
    wav_file(path)
    for key in ("OPENAI_API_KEY", "SPEECHMATICS_API_KEY"):
        monkeypatch.setenv(key, "test")

    class Translator:
        def __init__(self, key):
            pass

        async def close(self):
            pass

    async def run():
        connected = asyncio.Event()

        async def server(ws):
            await ws.recv()
            connected.set()
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as socket:
            endpoint = f"ws://127.0.0.1:{socket.sockets[0].getsockname()[1]}"
            client = LiveClient(
                recorder_factory=None,
                speechmatics_factory=lambda **kw: AgentSttClient(endpoint=endpoint, **kw),
                translation_factory=lambda h, k: TranslationWorker(
                    h, k, translator_factory=Translator
                ),
            )
            assert client.start(audio_file=str(path))
            await asyncio.wait_for(connected.wait(), 2)
            client.stop()
            assert await asyncio.to_thread(client.join, 2)
            assert client.playback_state == "Idle" and not client.error

    asyncio.run(run())
