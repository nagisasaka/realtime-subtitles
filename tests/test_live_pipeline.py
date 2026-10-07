import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from test_realtime_api import SyntheticMicrophone, until
from websockets.asyncio.server import serve

from realtime_subtitles.autosave import TranscriptAutosave
from realtime_subtitles.final_history import FinalHistory
from realtime_subtitles.live_client import LiveClient
from realtime_subtitles.realtime_api import State
from realtime_subtitles.speechmatics_api import SegmentHistory, SpeechmaticsClient
from realtime_subtitles.text_translation import OpenAITranslator, TranslationWorker


def event(text, index=0, speaker="S1", final=True):
    return {
        "message": "AddTranscript" if final else "AddPartialTranscript",
        "metadata": {"transcript": text, "start_time": index, "end_time": index + 1},
        "results": [
            {
                "type": "word",
                "start_time": index,
                "end_time": index + 1,
                "alternatives": [{"content": text.strip(), "speaker": speaker}],
            }
        ],
    }


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_final_unit_context_speakers_and_authoritative_text():
    h = FinalHistory()
    for i in range(7):
        segment = h.add_final(event(f"Fragment {i} without punctuation ", i), "a")
    target = h.add_final(event("First sentence. Second sentence without punctuation", 7, "S2"), "a")
    assert target.sequence_id == 7 and target.break_before
    assert target.en_text == "First sentence. Second sentence without punctuation"
    assert target.start_ms == 7000 and target.end_ms == 8000
    assert [x["text"] for x in h.context_for(7)] == [
        f"Fragment {i} without punctuation " for i in range(2, 7)
    ]
    assert len(h.segments()) == 8 and not segment.break_before
    assert h.add_final(event(target.en_text, 7, "S2"), "a") is None
    new = h.add_final(event(target.en_text, 7, "S2"), "b")
    assert new.sequence_id == 8 and h.context_for(8) == []
    assert new.break_before
    h.update_translation(7, "completed", text="一文目。二文目の断片")
    h.update_translation(7, "completed", text="must not overwrite")
    assert h.segments()[7].en_text == target.en_text
    assert h.segments()[7].ja_text == "一文目。二文目の断片"


def test_only_final_triggers_translation_partial_replaces():
    client = LiveClient()
    client.speechmatics = SimpleNamespace(history=SegmentHistory(), session_id="s")
    jobs = []
    client.translation = SimpleNamespace(submit=jobs.append)
    for text in ("The biggest", "The biggest challenge", "The biggest challenge is reliability"):
        e = event(text, final=False)
        words = client.speechmatics.history.accept(e, "s")
        client._receive(e, words)
    assert not jobs
    assert client.history.display_snapshot()[3] == "The biggest challenge is reliability"
    final = event("The biggest challenge is reliability")
    for _ in range(2):
        client._receive(final, client.speechmatics.history.accept(final, "s"))
    assert len(jobs) == 1 and jobs[0].en_text == final["metadata"]["transcript"]
    assert client.history.display_snapshot()[3] == ""
    client._session_changed(None)
    assert len(client.history.segments()) == 1


def test_unknown_and_mixed_speakers_do_not_split_final():
    h = FinalHistory()
    h.add_final(event("First.", 0, "S1"), "s")
    unknown = h.add_final(event("Unknown.", 1, "UU"), "s")
    assert unknown.speaker is None and not unknown.break_before
    mixed = event("Hello there.", 2)
    mixed["results"] = [
        {
            "type": "word",
            "start_time": 2,
            "end_time": 2.5,
            "alternatives": [{"content": "Hello", "speaker": "S1"}],
        },
        {
            "type": "word",
            "start_time": 2.5,
            "end_time": 3,
            "alternatives": [{"content": "there.", "speaker": "S2"}],
        },
    ]
    result = h.add_final(mixed, "s")
    assert len(h.segments()) == 3 and result.en_text == "Hello there."
    assert [w.speaker for w in result.words] == ["S1", "S2"]
    assert not h.add_final(event("Continue.", 3, "S2"), "s").break_before
    assert h.add_final(event("Changed.", 4, "S1"), "s").break_before


def test_parallel_translation_keeps_sequence_context_and_bounds():
    history = FinalHistory()
    completion = []
    active = 0
    maximum = 0
    contexts = {}

    class Translator:
        def __init__(self, key):
            pass

        async def translate(self, target, context):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            contexts[target.sequence_id] = context
            await asyncio.sleep(0.15 if target.sequence_id == 0 else 0.01)
            active -= 1
            completion.append(target.sequence_id)
            return f"訳{target.sequence_id}", {"output_tokens": 3}

        async def close(self):
            pass

    worker = TranslationWorker(history, "test", translator_factory=Translator, concurrency=3)
    worker.start()
    try:
        for i in range(8):
            worker.submit(history.add_final(event(f"English {i} ", i), "s"))
        wait_for(lambda: history.statistics().get("completed") == 8)
    finally:
        worker.finish()
        assert worker.join(3)
    assert completion[0] != 0 and maximum == 3
    assert [s.ja_text for s in history.segments()] == [f"訳{i}" for i in range(8)]
    assert contexts[7] == [{"speaker": "S1", "text": f"English {i} "} for i in range(2, 7)]


def test_queue_limit_preserves_english_and_stop_cancels_inflight():
    h = FinalHistory()
    entered = threading.Event()

    class Slow:
        def __init__(self, key):
            pass

        async def translate(self, target, context):
            entered.set()
            await asyncio.sleep(100)

        async def close(self):
            pass

    worker = TranslationWorker(
        h, "test", translator_factory=Slow, concurrency=1, queue_size=1, stop_drain=0.05
    )
    worker.submit(h.add_final(event("first", 0), "s"))
    assert not worker.submit(h.add_final(event("overflow", 1), "s"))
    assert h.segments()[1].translation_status == "skipped"
    worker.start()
    assert entered.wait(2)
    worker.submit(h.add_final(event("queued", 2), "s"))
    worker.finish()
    assert worker.join(2)
    assert h.statistics() == {"cancelled": 2, "skipped": 1}
    assert [s.en_text for s in h.segments()] == ["first", "overflow", "queued"]


def test_sdk_responses_schema_target_only_and_no_temperature():
    async def run():
        from openai import AsyncOpenAI

        calls = []

        async def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": "gpt-6-luna",
                    "output": [
                        {
                            "type": "message",
                            "id": "m",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "日本語のみ。", "annotations": []}
                            ],
                        }
                    ],
                },
            )

        translator = OpenAITranslator("test-placeholder")
        await translator.client.close()
        translator.client = AsyncOpenAI(
            api_key="test-placeholder",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        h = FinalHistory()
        target = h.add_final(event("Translate only this final", 2), "s")
        try:
            text, _ = await translator.translate(
                target, [{"speaker": "S1", "text": "Earlier context"}]
            )
        finally:
            await translator.close()
        assert text == "日本語のみ。"
        body = calls[0]
        assert body["model"] == "gpt-6-luna" and body["reasoning"] == {"effort": "none"}
        assert body["store"] is False and "temperature" not in body
        payload = json.loads(body["input"])
        assert payload["TARGET"]["text"] == target.en_text
        assert payload["CONTEXT"] == [{"speaker": "S1", "text": "Earlier context"}]

    asyncio.run(run())


def test_translation_http_error_does_not_erase_finals_or_leak_secret():
    class AuthError(Exception):
        status_code = 401

    class Fails:
        def __init__(self, key):
            pass

        async def translate(self, target, context):
            raise AuthError("secret-value")

        async def close(self):
            pass

    h = FinalHistory()
    worker = TranslationWorker(h, "test", translator_factory=Fails)
    worker.start()
    try:
        for i in range(2):
            worker.submit(h.add_final(event(f"English {i}", i), "s"))
        wait_for(lambda: h.statistics().get("failed") == 2)
    finally:
        worker.finish()
        assert worker.join(2)
    assert worker.error == "翻訳エラー: HTTP 401"
    assert all(s.en_text and s.ja_text is None for s in h.segments())


def test_autosave_updates_out_of_order_translations_and_clear(tmp_path):
    h = FinalHistory()
    first = h.add_final(event("Question?", 0), "s")
    second = h.add_final(event("Answer.", 1, "S2"), "s")
    save = TranscriptAutosave({"subtitles": h}, tmp_path)
    try:
        h.update_translation(second.sequence_id, "completed", text="答え。")
        h.clear_display()
        h.update_translation(first.sequence_id, "completed", text="質問？")
    finally:
        assert save.close()
    latest = {}
    for line in (save.directory / "subtitles.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["kind"] == "transcript":
            record = row["record"]
            latest[record["sequence_id"]] = record
    assert latest[0]["ja_text"] == "質問？" and latest[1]["ja_text"] == "答え。"
    assert latest[1]["break_before"]
    assert h.display_snapshot()[2] == []
    text = (save.directory / "subtitles.txt").read_text(encoding="utf-8")
    assert text.index("質問？") < text.index("答え。") and "---" in text
    manual = tmp_path / "final.jsonl"
    h.save(manual, overwrite=False)
    assert len(manual.read_text(encoding="utf-8").splitlines()) == 2


def test_native_sdk_pipeline_eos_restart_reconnect_and_translation_failure(monkeypatch):
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    SyntheticMicrophone.instances = []
    configs = []
    endings = []
    calls = []

    class Translator:
        def __init__(self, key):
            pass

        async def translate(self, target, context):
            calls.append(target.en_text)
            if "FAIL" in target.en_text:
                raise ValueError("test-secret")
            await asyncio.sleep(0.01)
            return "翻訳。", None

        async def close(self):
            pass

    async def run():
        async def server(ws):
            config = json.loads(await ws.recv())
            configs.append(config)
            number = len(configs)
            assert "translation_config" not in config
            assert config["transcription_config"]["enable_partials"] is True
            assert config["transcription_config"]["diarization"] == "speaker"
            assert config["transcription_config"]["model"] == "enhanced"
            await ws.send(json.dumps({"message": "RecognitionStarted", "id": str(number)}))
            count = 0
            async for message in ws:
                if isinstance(message, bytes):
                    assert len(message) == 9600
                    count += 1
                    await ws.send(json.dumps({"message": "AudioAdded", "seq_no": count}))
                    await ws.send(json.dumps(event("unfinished", final=False)))
                    if number == 1:
                        await ws.close(code=1011)
                        return
                    if count == 1:
                        await ws.send(json.dumps(event("FAIL one final. Two sentences.", 0)))
                    if count == 2:
                        await ws.send(json.dumps(event("Next final without punctuation ", 1, "S2")))
                else:
                    msg = json.loads(message)
                    assert msg["message"] == "EndOfStream"
                    endings.append((msg["last_seq_no"], count))
                    await ws.send(json.dumps(event("Final at EOS.", 4, "S1")))
                    await ws.send(json.dumps({"message": "EndOfTranscript"}))

        async with serve(server, "127.0.0.1", 0) as server_socket:
            endpoint = f"ws://127.0.0.1:{server_socket.sockets[0].getsockname()[1]}"
            client = LiveClient(
                microphone_factory=SyntheticMicrophone,
                speechmatics_factory=lambda **kw: SpeechmaticsClient(
                    endpoint=endpoint, retry_base=0.01, **kw
                ),
                translation_factory=lambda h, k: TranslationWorker(
                    h, k, translator_factory=Translator
                ),
            )
            for _ in range(2):
                prior = len(calls)
                assert client.start()
                try:
                    await until(lambda prior=prior: len(calls) >= prior + 2)
                    assert client.state == State.RUNNING
                finally:
                    client.stop()
                    assert await asyncio.to_thread(client.join, 5)
                assert client.state == State.STOPPED
            assert len(configs) == 3  # one independent Speechmatics reconnect, then user restart
            assert len(SyntheticMicrophone.instances) == 2  # no second mic on reconnect
            assert all(a == b and b >= 2 for a, b in endings)
            assert len(calls) == 6 and all(x != "unfinished" for x in calls)
            assert client.history.statistics() == {"failed": 2, "completed": 4}
            assert [s.sequence_id for s in client.history.segments()] == list(range(6))

    asyncio.run(run())


def test_stop_interrupts_handshake(monkeypatch):
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test")

    async def run():
        async def server(ws):
            await ws.recv()
            await ws.wait_closed()

        async with serve(server, "127.0.0.1", 0) as socket:
            c = SpeechmaticsClient(endpoint=f"ws://127.0.0.1:{socket.sockets[0].getsockname()[1]}")
            c.start()
            await asyncio.sleep(0.1)
            c.stop()
            assert await asyncio.to_thread(c.join, 2)
            assert c.state == "STOPPED"

    asyncio.run(run())


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Native Windows Tk")
def test_overlay_final_translation_replacement_and_scroll(tmp_path):
    import tkinter as tk

    from realtime_subtitles.ui import SubtitleApp

    root = tk.Tk()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    client = LiveClient()
    app = SubtitleApp(
        root, client=client, settings_file=tmp_path / "settings.json", device_loader=lambda: []
    )

    def pump():
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)

    try:
        client.history.set_partial("The biggest")
        pump()
        client.history.set_partial("The biggest challenge")
        pump()
        assert app.en_text.get("1.0", "end-1c") == "The biggest challenge"
        assert app.en_text.tag_ranges("partial")
        first = client.history.add_final(event("The biggest challenge", 0), "s")
        client.history.set_partial("")
        second = client.history.add_final(event("Next speaker.", 1, "S2"), "s")
        client.history.update_translation(second.sequence_id, "completed", text="次の話者。")
        pump()
        assert app.ja_text.get("1.0", "end-1c") == "［翻訳待ち…］\n\n次の話者。"
        client.history.update_translation(first.sequence_id, "completed", text="最大の課題")
        pump()
        assert app.ja_text.get("1.0", "end-1c") == "最大の課題\n\n次の話者。"
        assert not app.en_text.tag_ranges("partial")
        for i in range(2, 70):
            s = client.history.add_final(event(f"Line {i}.\n", i), "s")
            if i > 2:
                client.history.update_translation(s.sequence_id, "completed", text=f"行{i}。\n")
        pump()
        assert app.ja_text.yview()[1] > 0.999
        app._scroll_caption("ja", "moveto", 0.5)
        pump()
        visible = app.ja_text.get("@0,0", "@0,0 lineend")
        client.history.update_translation(2, "completed", text="遅れて届いた長い翻訳です。\n" * 4)
        pump()
        assert app.ja_text.get("@0,0", "@0,0 lineend") == visible
        client.history.clear_display()
        pump()
        assert not app.en_text.get("1.0", "end-1c")
        assert len(client.history.segments()) == 70 and not errors
    finally:
        app.close()
        deadline = time.monotonic() + 5
        while app.autosave.active and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_translation_retry_is_bounded_and_stop_interrupts_retry_sleep():
    class Busy(Exception):
        status_code = 429

    calls = []

    class RateLimited:
        def __init__(self, key):
            pass

        async def translate(self, target, context):
            calls.append(target.sequence_id)
            raise Busy("secret-not-for-logs")

        async def close(self):
            pass

    h = FinalHistory()
    worker = TranslationWorker(h, "test", translator_factory=RateLimited, stop_drain=0.01)
    worker.start()
    try:
        worker.submit(h.add_final(event("one"), "s"))
        wait_for(lambda: h.statistics().get("failed") == 1)
        assert calls == [0, 0]
        worker.submit(h.add_final(event("two", 1), "s"))
        wait_for(lambda: len(calls) == 3)
    finally:
        worker.finish()
        assert worker.join(2)
    assert h.segments()[1].translation_status == "cancelled"
    assert calls == [0, 0, 1]


def test_capture_prepare_failure_and_missing_keys_are_visible(monkeypatch):
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    c = LiveClient()
    assert not c.start() and c.state == State.ERROR
    assert "SPEECHMATICS_API_KEY" in c.error
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    def broken(device):
        raise OSError("unavailable")

    c = LiveClient(microphone_factory=broken)
    assert c.start() and c.join(2)
    assert c.state == State.ERROR and c.error == "OSError"


def test_autosave_disk_failure_retries_final_updates_without_duplicates(tmp_path, monkeypatch):
    original = TranscriptAutosave._append
    allowed = threading.Event()

    def unavailable(self, path, offset, payload):
        if not allowed.is_set():
            with path.open("ab") as output:
                output.write(payload[:15])
            raise OSError("private-details")
        return original(self, path, offset, payload)

    monkeypatch.setattr(TranscriptAutosave, "_append", unavailable)
    h = FinalHistory()
    h.add_final(event("Original English"), "s")
    save = TranscriptAutosave({"subtitles": h}, tmp_path)
    try:
        wait_for(lambda: bool(save.error))
        assert "private-details" not in save.error
        h.update_translation(0, "completed", text="日本語。")
        allowed.set()
        wait_for(lambda: save.cursors["subtitles"] == 2)
    finally:
        allowed.set()
        assert save.close()
    records = [
        json.loads(line)
        for line in (save.directory / "subtitles.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["record"]["translation_status"] for r in records if r["kind"] == "transcript"] == [
        "pending",
        "completed",
    ]
