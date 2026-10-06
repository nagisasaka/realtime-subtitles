import asyncio
import json
import time

import pytest
from websockets.asyncio.server import serve

from realtime_subtitles.audio import FRAME_BYTES, LatestQueue
from realtime_subtitles.comparison import BorrowedMicrophone, Experiment
from realtime_subtitles.speechmatics_api import SegmentHistory, SpeechmaticsClient, parse_event


def en(text, final=False, start=0, end=1, speaker="S1"):
    return {
        "message": "AddTranscript" if final else "AddPartialTranscript",
        "metadata": {"transcript": text, "start_time": start, "end_time": end},
        "results": [
            {
                "type": "word",
                "start_time": start,
                "end_time": end,
                "alternatives": [{"content": text.strip(), "speaker": speaker}],
            }
        ],
    }


def ja(text, final=False, start=0, end=1, speaker="S2"):
    return {
        "message": "AddTranslation" if final else "AddPartialTranslation",
        "language": "ja",
        "results": [{"content": text, "start_time": start, "end_time": end, "speaker": speaker}],
    }


def test_partial_replacement_and_independent_languages():
    h = SegmentHistory()
    h.begin_session("one")
    h.accept(en("We need to think about ", True), "one")
    h.accept(en("deployment in", start=1, end=2), "one")
    h.accept(ja("導入", start=1, end=2), "one")
    h.accept(en("deployment in production", start=1, end=3), "one")
    _, finals, partials = h.snapshot()
    assert (
        "".join(s.text for s in finals + partials["en"])
        == "We need to think about deployment in production"
    )
    assert partials["ja"][0].text == "導入"
    h.accept(en("deployment in production.", True, 1, 3), "one")
    assert not h.snapshot()[2]["en"]
    assert h.snapshot()[2]["ja"]
    h.accept(ja("本番環境への導入です。", True, 1, 3), "one")
    assert not h.snapshot()[2]["ja"]
    assert h.snapshot()[1][-1].speaker == "S2"
    assert h.snapshot()[1][-1].start_ms == 1000


def test_multiple_translation_segments_overlap_partial_and_duplicate_final():
    h = SegmentHistory()
    h.begin_session("one")
    event = ja("前半", False, 0, 1)
    event["results"] += ja("後半", False, 1, 2)["results"]
    h.accept(event, "one")
    final = ja("前半確定", True, 0, 1)
    h.accept(final, "one")
    h.accept(final, "one")
    assert len(h.snapshot()[1]) == 1
    assert [s.text for s in h.snapshot()[2]["ja"]] == ["後半"]
    h.accept(ja("古いpartial", False, 0, 1), "one")
    assert not h.snapshot()[2]["ja"]
    h.begin_session("two")
    h.accept(final, "two")
    assert len(h.snapshot()[1]) == 2  # session-relative timestamps must not collide


def test_word_speakers_punctuation_exact_text_and_missing_metadata():
    event = en(" Hello, world! ", True)
    event["results"] = [
        {
            "type": kind,
            "start_time": start,
            "end_time": end,
            "alternatives": [{"content": content, "speaker": speaker}],
        }
        for kind, content, start, end, speaker in [
            ("word", "Hello", 0.1, 0.5, "S1"),
            ("punctuation", ",", 0.5, 0.5, "S1"),
            ("word", "world", 0.7, 1, "S2"),
            ("punctuation", "!", 1, 1, "S2"),
        ]
    ]
    segments = parse_event(event, "s")
    assert "".join(s.text for s in segments) == event["metadata"]["transcript"]
    assert [s.speaker for s in segments] == ["S1", "S1", "S2", "S2"]
    assert segments[2].start_ms == 700
    event = ja("日本語")
    del event["results"][0]["speaker"]
    del event["results"][0]["start_time"]
    assert parse_event(event)[0].speaker is None
    assert parse_event(event)[0].start_ms is None


def test_final_save_excludes_partial_and_preserves_metadata(tmp_path):
    h = SegmentHistory()
    h.accept(ja("確定", True, 12.34, 15.78), "session")
    h.accept(ja("未確定", False, 16, 17), "session")
    path = tmp_path / "final.jsonl"
    h.save(path)
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["text"] == "確定"
    assert rows[0]["speaker"] == "S2" and rows[0]["start_ms"] == 12340
    with pytest.raises(FileExistsError):
        h.save(path)


def test_single_microphone_fanout_and_bounded_queues(monkeypatch):
    experiment = Experiment(compare=True)
    experiment.borrowed = BorrowedMicrophone(experiment)
    experiment.borrowed.start()

    # No API or microphone: fake an active Speechmatics worker.
    class Thread:
        def is_alive(self):
            return True

    experiment.speechmatics.thread = Thread()
    en_sink = LatestQueue(2)
    experiment.borrowed.frames.sinks = (en_sink,)
    for n in range(100):
        pcm = bytes([n]) * FRAME_BYTES
        experiment.dispatch((time.monotonic(), pcm))
    left = experiment.borrowed.frames.get_nowait()[1]
    assert left[0] == 97 and experiment.speechmatics.frames.qsize() == 5
    while experiment.speechmatics.frames.qsize() > 3:
        experiment.speechmatics.frames.get_nowait()
    right = experiment.speechmatics.frames.get_nowait()[1]
    assert left is right
    assert experiment.borrowed.frames.dropped == 97
    assert en_sink.dropped == 98


async def until(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


def test_real_sdk_mock_websocket_start_stop_start(monkeypatch):
    pytest.importorskip("speechmatics.rt")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")

    async def run():
        configs = []
        endings = []
        received = []

        async def server(ws):
            config = json.loads(await ws.recv())
            configs.append(config)
            assert config["transcription_config"]["model"] == "enhanced"
            assert config["transcription_config"]["diarization"] == "speaker"
            assert config["translation_config"] == {
                "target_languages": ["ja"],
                "enable_partials": True,
            }
            assert config["audio_format"]["sample_rate"] == 24000
            await ws.send(json.dumps({"message": "RecognitionStarted", "id": str(len(configs))}))
            count = 0
            async for data in ws:
                if isinstance(data, bytes):
                    count += 1
                    received.append(data)
                    await ws.send(json.dumps({"message": "AudioAdded", "seq_no": count}))
                    await ws.send(json.dumps(en("hello changing partial")))
                    await ws.send(json.dumps(ja("仮字幕")))
                else:
                    event = json.loads(data)
                    assert event["message"] == "EndOfStream"
                    endings.append((event["last_seq_no"], count))
                    await ws.send(json.dumps(en("Hello final.", True)))
                    await ws.send(json.dumps(ja("確定字幕。", True)))
                    await ws.send(json.dumps({"message": "EndOfTranscript"}))

        async with serve(server, "127.0.0.1", 0) as ws:
            port = ws.sockets[0].getsockname()[1]
            client = SpeechmaticsClient(endpoint=f"ws://127.0.0.1:{port}", retry_base=0.01)
            for _ in range(2):
                assert client.start()
                await until(lambda: client.state == "RUNNING")
                for _ in range(3):
                    client.frames.put_latest((time.monotonic(), b"\0" * FRAME_BYTES))
                await until(lambda: client.sent_frames == 3)
                client.stop()
                assert await asyncio.to_thread(client.join, 4)
                assert client.state == "STOPPED"
            assert endings == [(3, 3), (3, 3)]
            assert len(received) == 6
            assert len(client.history.snapshot()[1]) == 4
            assert not client.history.snapshot()[2]["en"]
            assert not client.error

    asyncio.run(run())


def test_stop_interrupts_connection_and_retry(monkeypatch):
    pytest.importorskip("speechmatics.rt")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")

    async def run():
        async def server(ws):
            await ws.recv()
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as ws:
            client = SpeechmaticsClient(endpoint=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
            client.start()
            await asyncio.sleep(0.1)
            start = time.monotonic()
            client.stop()
            assert await asyncio.to_thread(client.join, 2)
            assert time.monotonic() - start < 1 and client.state == "STOPPED"

    asyncio.run(run())


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Native Windows microphone")
def test_windows_microphone_through_sdk_to_protocol_server(monkeypatch):
    """Real microphone/PCM/SDK; server emits fixtures, NOT actual speech recognition."""
    pytest.importorskip("speechmatics.rt")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")

    async def run():
        frames = []

        async def server(ws):
            config = json.loads(await ws.recv())
            assert config["audio_format"]["encoding"] == "pcm_s16le"
            await ws.send(
                json.dumps({"message": "RecognitionStarted", "id": "native-protocol-test"})
            )
            async for message in ws:
                if isinstance(message, bytes):
                    frames.append(message)
                    assert len(message) == FRAME_BYTES
                    await ws.send(json.dumps({"message": "AudioAdded", "seq_no": len(frames)}))
                    await ws.send(json.dumps(en("Protocol fixture.", True)))
                    await ws.send(json.dumps(ja("プロトコル検証用。", True)))
                else:
                    event = json.loads(message)
                    assert event["last_seq_no"] == len(frames)
                    await ws.send(json.dumps({"message": "EndOfTranscript"}))

        async with serve(server, "127.0.0.1", 0) as ws:
            experiment = Experiment(endpoint=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
            try:
                assert experiment.start()
                await until(lambda: len(frames) >= 10, seconds=12)
                assert experiment.microphone.info["output_rate"] == 24000
                assert experiment.speechmatics.event_counts["AddTranslation"] >= 1
                experiment.stop()
                assert await asyncio.to_thread(experiment.join, 12)
                assert not experiment.microphone.stream and not experiment.microphone.worker
                assert experiment.state == "STOPPED"
            finally:
                experiment.stop()
                await asyncio.to_thread(experiment.close)

    asyncio.run(run())


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Native Windows Tk")
def test_comparison_gui_partial_replaces_and_final_style_promotes():
    import tkinter as tk

    from realtime_subtitles.comparison_ui import ComparisonApp
    from realtime_subtitles.ui import enable_dpi_awareness

    enable_dpi_awareness()
    root = tk.Tk()
    experiment = Experiment(compare=True)
    app = ComparisonApp(root, experiment)
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    try:
        key = ("speechmatics", "en")
        app.update_text(key, "We need ", "deployment in")
        app.update_text(key, "We need ", "deployment in production")
        assert app.texts[key].get("1.0", "end-1c") == "We need deployment in production"
        assert app.texts[key].tag_ranges("partial")
        app.update_text(key, "We need deployment in production")
        assert not app.texts[key].tag_ranges("partial")
        app.update_text(("speechmatics", "ja"), "確定。", "仮字幕")
        app.update_text(("speechmatics", "ja"), "確定。", "変更した字幕")
        assert app.texts[("speechmatics", "ja")].get("1.0", "end-1c") == "確定。変更した字幕"
        # Preserve a reader's position when a later partial changes.
        text = "".join(f"Line {i}\n" for i in range(100))
        app.update_text(key, text, "tail")
        root.update()
        app.scroll(key, "moveto", 0.3)
        root.update()
        visible = app.texts[key].get("@0,0", "@0,0 lineend")
        app.update_text(key, text, "changed tail")
        root.update()
        assert app.texts[key].get("@0,0", "@0,0 lineend") == visible
        assert len(app.texts) == 4 and not errors
    finally:
        experiment.close()
        root.destroy()


def test_reconnect_keeps_finals_and_drops_old_partial(monkeypatch):
    pytest.importorskip("speechmatics.rt")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")

    async def run():
        connections = []

        async def server(ws):
            await ws.recv()
            connections.append(True)
            identity = str(len(connections))
            await ws.send(json.dumps({"message": "RecognitionStarted", "id": identity}))
            async for raw in ws:
                if isinstance(raw, bytes):
                    if identity == "1":
                        await ws.send(json.dumps(en("Old final. ", True)))
                        await ws.send(json.dumps(en("Old partial", False, 1, 2)))
                        await ws.close(code=1011)
                        return
                    await ws.send(json.dumps({"message": "AudioAdded", "seq_no": 1}))
                elif json.loads(raw).get("message") == "EndOfStream":
                    await ws.send(json.dumps({"message": "EndOfTranscript"}))

        async with serve(server, "127.0.0.1", 0) as ws:
            client = SpeechmaticsClient(
                endpoint=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}", retry_base=0.01
            )
            try:
                client.start()
                await until(lambda: client.state == "RUNNING")
                client.frames.put_latest((time.monotonic(), b"\0" * FRAME_BYTES))
                await until(lambda: len(client.history.snapshot()[1]) == 1)
                # A send observes the broken transport; no watchdog of another backend.
                client.frames.put_latest((time.monotonic(), b"\0" * FRAME_BYTES))
                await until(lambda: len(connections) == 2 and client.state == "RUNNING")
                assert not client.history.snapshot()[2]["en"]
                assert client.history.snapshot()[1][0].text == "Old final. "
            finally:
                client.stop()
                await asyncio.to_thread(client.join, 10)

    asyncio.run(run())


def test_speechmatics_failure_does_not_restart_openai_or_open_second_mic(monkeypatch):
    from realtime_subtitles.subtitle_buffer import TranscriptHistory

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    opened = []

    class Mic:
        def __init__(self, device):
            opened.append(self)
            self.frames = LatestQueue(3)
            self.producer = None
            self.error = None
            self.stopped = False

        def prepare(self):
            pass

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def check_health(self):
            self.frames.put_latest((time.monotonic(), b"\0" * FRAME_BYTES))

        def diagnostics(self):
            return {}

    class OpenAI:
        def __init__(self, **kwargs):
            self.factory = kwargs["microphone_factory"]
            self.history = TranscriptHistory()
            self.starts = 0
            self.stops = 0
            self.state = "STOPPED"

        def start(self, *args):
            self.starts += 1
            self.mic = self.factory()
            self.mic.start()
            self.state = "RUNNING"

        def stop(self):
            self.stops += 1
            self.mic.stop()
            self.state = "STOPPED"

        def join(self, *args):
            return True

        def snapshot(self):
            return {"state": self.state, "error": ""}

    monkeypatch.setattr("realtime_subtitles.comparison.RealtimeClient", OpenAI)
    experiment = Experiment(compare=True, microphone_factory=Mic)

    # Simulate a Speechmatics-only authentication failure after worker startup.
    def start_failed():
        experiment.speechmatics.state = "ERROR"
        experiment.speechmatics.error = "not_authorised"
        return True

    monkeypatch.setattr(experiment.speechmatics, "start", start_failed)
    try:
        assert experiment.start()
        deadline = time.monotonic() + 3
        while experiment.frames_dispatched < 10 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert experiment.frames_dispatched >= 10
        assert len(opened) == 1 and experiment.openai.starts == 1 and experiment.openai.stops == 0
        assert experiment.openai.state == "RUNNING" and experiment.state == "RUNNING"
    finally:
        experiment.stop()
        assert experiment.join(3)
    assert opened[0].stopped


def test_auth_failure_is_explicit_and_never_echoes_key(monkeypatch):
    pytest.importorskip("speechmatics.rt")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-secret-value")

    async def run():
        async def reject(connection, request):
            return connection.respond(401, "test-secret-value")

        async with serve(lambda ws: None, "127.0.0.1", 0, process_request=reject) as ws:
            client = SpeechmaticsClient(endpoint=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}")
            assert client.start()
            await until(lambda: not client.active)
            assert client.state == "ERROR" and client.error == "HTTP 401"
            assert "test-secret-value" not in json.dumps(client.snapshot())

    asyncio.run(run())
