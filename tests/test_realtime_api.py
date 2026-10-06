import asyncio
import base64
import json
import threading
import time

import numpy as np
import pytest
from websockets.asyncio.server import serve

from realtime_subtitles.audio import AudioConverter, LatestQueue
from realtime_subtitles.realtime_api import RealtimeClient, State, safe_error, session_update


class SyntheticMicrophone:
    """Exercises the real converter with stereo 48 kHz, including silent blocks."""

    instances = []

    def __init__(self, device):
        self.frames = LatestQueue(3)
        self.stopped = threading.Event()
        self.worker = None
        self.instances.append(self)

    def prepare(self):
        pass

    def start(self):
        self.worker = threading.Thread(target=self._capture, name="synthetic-microphone")
        self.worker.start()

    def _capture(self):
        converter = AudioConverter(48000)
        index = 0
        while not self.stopped.wait(0.05):
            signal = np.zeros((2400, 2), dtype=np.float32)
            if index % 24 < 4:
                signal[:, :] = 0.1
            for frame in converter.feed(signal):
                self.frames.put_latest((time.monotonic(), frame))
            index += 1
        for frame in converter.finish():
            self.frames.put_latest((time.monotonic(), frame))

    def check_health(self):
        pass

    def stop(self):
        self.stopped.set()
        if self.worker:
            self.worker.join(1)

    def diagnostics(self):
        return {
            "device": "Synthetic 48k stereo",
            "input_rate": 48000,
            "input_channels": 2,
            "input_format": "float32",
            "output_rate": 24000,
            "output_channels": 1,
            "output_format": "PCM16 little endian",
            "dbfs": -20,
            "rms": 0.1,
            "audio_queue": self.frames.qsize(),
        }


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_protocol_vertical_slice_restart_reconnect_and_graceful_flush(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        frames, updates, closes, states = [], [], [], []
        connection_count = 0

        async def server(ws):
            nonlocal connection_count
            connection_count += 1
            number = connection_count
            await ws.send(json.dumps({"type": "session.created", "session": {"id": str(number)}}))
            async for message in ws:
                event = json.loads(message)
                if event["type"] == "session.update":
                    updates.append(event)
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif event["type"] == "session.input_audio_buffer.append":
                    frame = base64.b64decode(event["audio"], validate=True)
                    assert len(frame) == 9600
                    frames.append(frame)
                    for language, text in [
                        ("input", "Hello"),
                        ("input", " world."),
                        ("output", "こんにちは。"),
                    ]:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": f"session.{language}_transcript.delta",
                                    "delta": text,
                                    "elapsed_ms": 200,
                                }
                            )
                        )
                    await ws.send(
                        json.dumps({"type": "session.output_audio.delta", "delta": "AA=="})
                    )
                    if number == 1:
                        await ws.close(code=1011, reason="test disconnect")
                        return
                elif event["type"] == "session.close":
                    closes.append(number)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.output_transcript.delta",
                                "delta": "終了。",
                                "elapsed_ms": 400,
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "session.closed"}))

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
                retry_base=0.1,
                event_log=tmp_path / "raw-events.jsonl",
            )
            original = client._transition

            def transition(state):
                states.append(state)
                original(state)

            client._transition = transition
            try:
                for _ in range(2):
                    assert client.start()
                    await until(lambda: connection_count >= 2 and client.state == State.RUNNING)
                    await until(lambda: len(frames) >= 4)
                    await asyncio.sleep(0.3)
                    client.stop()
                    assert client.state == State.STOPPING
                    await until(lambda: not client.active)
                    assert client.state == State.STOPPED
                assert State.RECONNECTING in states
                raw_rows = [
                    json.loads(line)
                    for line in (tmp_path / "raw-events.jsonl").read_text().splitlines()
                ]
                raw_en = [r for r in raw_rows if r["type"] == "session.input_transcript.delta"]
                assert raw_en and all(r["elapsed_ms"] == 200 for r in raw_en)
                assert all(r["received_at"] and r["raw_server_event"] for r in raw_en)
                assert len(closes) == 2
                assert all(u == session_update() for u in updates)
                assert "Hello world." in client.history.full_text("en")
                assert client.history.full_text("ja").endswith("終了。")
                assert len({r["session_id"] for r in client.history.records()}) == 3
                assert any(frame == b"\0" * 9600 for frame in frames)
                assert all(
                    not m.worker or not m.worker.is_alive() for m in SyntheticMicrophone.instances
                )
            finally:
                client.stop()
                await until(lambda: not client.active)

    asyncio.run(scenario())


def test_stop_interrupts_handshake_and_backoff(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        async def silent_server(ws):
            await ws.wait_closed()

        async with serve(silent_server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
                close_timeout=0.1,
            )
            client.start()
            await until(lambda: client.snapshot()["websocket"] == "open / configuring")
            start = time.monotonic()
            client.stop()
            await until(lambda: not client.active, timeout=2)
            assert time.monotonic() - start < 1.5
            assert client.state == State.STOPPED
        # The port is now closed: force a long retry delay, then interrupt it.
        client = RealtimeClient(
            source_mode="sidecar",
            endpoint=f"ws://127.0.0.1:{port}",
            microphone_factory=SyntheticMicrophone,
            retry_base=8,
        )
        client.start()
        await until(lambda: client.state == State.RECONNECTING)
        client.stop()
        await until(lambda: not client.active, timeout=1)
        assert client.state == State.STOPPED

    asyncio.run(scenario())


def test_missing_key_and_state_validation(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = RealtimeClient(
        source_mode="sidecar",
    )
    assert not client.start()
    assert client.state == State.ERROR
    client.stop()
    assert client.state == State.STOPPED
    with pytest.raises(ValueError):
        client._transition(State.RUNNING)


def test_key_redaction():
    assert "secret" not in safe_error("secret exposed", "secret")
    assert "sk-test123" not in safe_error("sk-test123")
    assert "Bearer" not in safe_error("Authorization: Bearer private")


def test_stop_drains_delayed_final_captions(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        close_received = asyncio.Event()

        async def server(ws):
            await ws.send(json.dumps({"type": "session.created"}))
            async for message in ws:
                kind = json.loads(message)["type"]
                if kind == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif kind == "session.close":
                    close_received.set()
                    await asyncio.sleep(0.2)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.input_transcript.delta",
                                "delta": "Last sentence.",
                                "elapsed_ms": 200,
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.output_transcript.delta",
                                "delta": "最後の文。",
                                "elapsed_ms": 400,
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "session.closed"}))

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
            )
            try:
                client.start()
                await until(lambda: client.state == State.RUNNING)
                client.stop()
                await asyncio.wait_for(close_received.wait(), timeout=2)
                assert client.state == State.STOPPING
                assert SyntheticMicrophone.instances[-1].stopped.is_set()
                await until(lambda: not client.active)
                assert client.state == State.STOPPED and not client.snapshot()["error"]
                assert client.history.full_text("en") == "Last sentence."
                assert client.history.full_text("ja") == "最後の文。"
                assert client.snapshot()["audio_queue"] == 0
            finally:
                client.stop()
                await until(lambda: not client.active)

    asyncio.run(scenario())


def test_api_configuration_error_is_visible_and_not_retried(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        connections = []

        async def server(ws):
            connections.append(ws)
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "bad"}}))
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "code": "invalid_request_error",
                            "message": "unsupported field test-placeholder",
                        },
                    }
                )
            )
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
            )
            client.start()
            await until(lambda: not client.active)
            assert client.state == State.ERROR
            assert len(connections) == 1
            assert "invalid_request_error" in client.snapshot()["error"]
            assert "test-placeholder" not in client.snapshot()["error"]
            assert SyntheticMicrophone.instances[-1].worker is None

    asyncio.run(scenario())


def test_stop_during_device_start_cleans_up(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    opening = threading.Event()

    class SlowMicrophone(SyntheticMicrophone):
        def start(self):
            opening.set()
            time.sleep(0.1)
            super().start()

    async def scenario():
        async def server(ws):
            await ws.send(json.dumps({"type": "session.created"}))
            async for message in ws:
                kind = json.loads(message)["type"]
                if kind == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif kind == "session.close":
                    await ws.send(json.dumps({"type": "session.closed"}))

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SlowMicrophone,
            )
            client.start()
            await until(opening.is_set)
            client.stop()
            await until(lambda: not client.active)
            assert client.state == State.STOPPED
            mic = SlowMicrophone.instances[-1]
            assert mic.stopped.is_set() and not mic.worker.is_alive()

    asyncio.run(scenario())


def test_retry_budget_is_bounded(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        connections = []

        async def server(ws):
            connections.append(ws)
            await ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "error": {"code": "server_error", "message": "temporary failure"},
                    }
                )
            )
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
                retry_base=0.01,
                max_retries=2,
            )
            try:
                client.start()
                await until(lambda: not client.active, timeout=4)
                assert client.state == State.ERROR
                assert "上限" in client.snapshot()["error"]
                assert len(connections) == 3
            finally:
                client.stop()
                await until(lambda: not client.active)

    asyncio.run(scenario())


def test_source_delay_is_observational_only():
    from realtime_subtitles.realtime_api import TranscriptHealth

    health = TranscriptHealth()
    health.received("en", 0)
    for second in range(1, 134):
        health.received("ja", second)
    assert health.snapshot(133)["source_delayed"]
    assert health.snapshot(133)["transcript_events"] == {"en": 1, "ja": 133}
    assert not health.delayed(140)  # Both streams are now quiet.
    health.received("en", 141)
    assert not health.delayed(141)


def test_source_delay_does_not_reconnect_translation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        connections = []

        async def server(ws):
            connections.append(ws)
            await ws.send(json.dumps({"type": "session.created"}))
            async for message in ws:
                kind = json.loads(message)["type"]
                if kind == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif kind == "session.input_audio_buffer.append":
                    await ws.send(
                        json.dumps({"type": "session.output_transcript.delta", "delta": "訳"})
                    )
                elif kind == "session.close":
                    await ws.send(json.dumps({"type": "session.closed"}))

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                source_mode="sidecar",
                endpoint=f"ws://127.0.0.1:{port}",
                microphone_factory=SyntheticMicrophone,
            )
            try:
                client.start()
                await until(lambda: bool(client.history.full_text("ja")))
                with client._lock:
                    client._transcripts.first_target = time.monotonic() - 300
                await asyncio.sleep(0.7)
                assert client.snapshot()["source_delayed"]
                assert len(connections) == 1
                assert client.state == State.RUNNING
            finally:
                client.stop()
                await until(lambda: not client.active)

    asyncio.run(scenario())


def test_speech_pause_separates_both_languages_without_changing_raw_delta():
    client = RealtimeClient(
        source_mode="sidecar",
    )
    for lang in ["input", "output"]:
        client._handle_event({"type": f"session.{lang}_transcript.delta", "delta": "First."})
    client._paragraph_pending = {"en": True, "ja": True}
    for lang in ["output", "input"]:  # Independent arrival order.
        client._handle_event(
            {"type": f"session.{lang}_transcript.delta", "delta": "Next", "elapsed_ms": 400}
        )
        client._handle_event({"type": f"session.{lang}_transcript.delta", "delta": " word."})
    for language in ["en", "ja"]:
        assert client.history.full_text(language) == "First.\nNext word."
    records = client.history.records()
    assert sum(r["kind"] == "paragraph" for r in records) == 2
    assert [r["delta"] for r in records if r["kind"] == "delta"] == [
        "First.",
        "First.",
        "Next",
        " word.",
        "Next",
        " word.",
    ]


def test_separate_english_failure_never_reconnects_translation(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def scenario():
        translated, english, translation_connections, english_connections = [], [], [], []

        async def translation_server(ws):
            translation_connections.append(ws)
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "ja-session"}}))
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "session.update":
                    assert event["session"]["audio"]["input"]["transcription"] is None
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif event["type"] == "session.input_audio_buffer.append":
                    translated.append(event["audio"])
                    await ws.send(
                        json.dumps({"type": "session.output_transcript.delta", "delta": "訳"})
                    )
                elif event["type"] == "session.close":
                    await ws.send(json.dumps({"type": "session.closed"}))

        async def english_server(ws):
            english_connections.append(ws)
            number = len(english_connections)
            await ws.send(
                json.dumps({"type": "session.created", "session": {"id": f"en-{number}"}})
            )
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "session.update":
                    transcription = event["session"]["audio"]["input"]["transcription"]
                    assert transcription == {
                        "model": "gpt-live-transcribe",
                        "languages": ["en"],
                        "delay": "low",
                    }
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif event["type"] == "input_audio_buffer.append":
                    english.append(event["audio"])
                    await ws.send(
                        json.dumps(
                            {
                                "type": "conversation.item.input_audio_transcription.delta",
                                "delta": "Hello.",
                                "item_id": "item1",
                            }
                        )
                    )
                    if number == 1:
                        await ws.close(code=1011)
                        return
                elif event["type"] == "input_audio_buffer.commit":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "transcript": "Hello.",
                                "item_id": "item1",
                            }
                        )
                    )

        async with (
            serve(translation_server, "127.0.0.1", 0) as ja,
            serve(english_server, "127.0.0.1", 0) as en,
        ):
            client = RealtimeClient(
                source_mode="separate",
                endpoint=f"ws://127.0.0.1:{ja.sockets[0].getsockname()[1]}",
                transcription_endpoint=f"ws://127.0.0.1:{en.sockets[0].getsockname()[1]}",
                microphone_factory=SyntheticMicrophone,
                retry_base=0.01,
                event_log=tmp_path / "fanout.jsonl",
            )
            try:
                client.start()
                await until(lambda: len(english_connections) == 2 and len(english) >= 4)
                assert len(translation_connections) == 1
                assert client.state == State.RUNNING
                assert client.history.full_text("en") and client.history.full_text("ja")
                assert all(frame in translated for frame in english[:-1])
                assert {
                    r["session_id"] for r in client.history.records() if r["language"] == "ja"
                } == {"ja-session"}
            finally:
                client.stop()
                await until(lambda: not client.active, timeout=7)
            assert client.state == State.STOPPED
            logs = [json.loads(r) for r in (tmp_path / "fanout.jsonl").read_text().splitlines()]
            assert {r["stream"] for r in logs} == {"english", "translation"}

    asyncio.run(scenario())
