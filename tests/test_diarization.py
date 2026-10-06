import asyncio
import io
import json
import logging
import threading
import time
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from realtime_subtitles.audio import FRAME_BYTES, LatestQueue
from realtime_subtitles.diarization import (
    AudioTimelineTap,
    DiarizationError,
    DiarizationSidecar,
    OpenAIDiarizer,
    WindowRing,
    wav_bytes,
)
from realtime_subtitles.realtime_api import RealtimeClient, State
from realtime_subtitles.speaker_timeline import (
    BoundaryTimeline,
    SpeakerBoundary,
    SpeakerSegment,
    align_english,
    align_japanese,
    normalize_text,
    segment_boundaries,
    similarity,
)
from realtime_subtitles.subtitle_buffer import TranscriptHistory


class Timeline:
    def timestamp(self, language, elapsed, session):
        return {
            "timeline_id": "capture",
            "audio_time_ms": elapsed if language == "ja" else None,
            "timing_source": "elapsed"
            if language == "ja" and elapsed is not None
            else "unavailable",
        }


def history():
    result = TranscriptHistory()
    result.set_audio_timeline(Timeline())
    return result


def boundary(**kwargs):
    values = dict(
        id="boundary",
        audio_time_ms=10_000,
        speaker_before="A",
        speaker_after="B",
        timeline_id="capture",
        window_start_ms=0,
        window_end_ms=30_000,
        text_before="What do you think about deployment?",
        text_hint="I think the reliability is the biggest challenge.",
    )
    values.update(kwargs)
    return SpeakerBoundary(**values)


def test_overlapping_windows_and_gap_never_compresses_sample_clock():
    ring = WindowRing()
    windows = []
    for start in range(0, 80_000, 200):
        pcm = bytes([start // 200 % 256]) * FRAME_BYTES
        windows += ring.feed("capture", start, pcm)
    assert [(w.start_ms, w.end_ms) for w in windows] == [(0, 30000), (25000, 55000), (50000, 80000)]
    assert windows[0].pcm[-5 * 48000 :] == windows[1].pcm[: 5 * 48000]
    assert len(ring.frames) <= 150
    ring.feed("capture", 90000, b"\0" * FRAME_BYTES)
    assert ring.next_start == 90000 and len(ring.frames) == 1
    assert not ring.feed("next-session", 0, b"\0" * FRAME_BYTES)
    assert len(ring.frames) == 1


def test_pcm_fanout_and_translation_gap_mapping():
    source, tapped = LatestQueue(3), LatestQueue(20)
    tap = AudioTimelineTap("translation", tapped)
    source.sinks = (tap,)
    for n in range(3):
        frame = bytes([n]) * FRAME_BYTES
        source.put_latest((time.monotonic(), frame))
        timeline, chunk = tapped.get_nowait()
        assert timeline == "translation"
        assert chunk.pcm is frame
        assert (chunk.start_ms, chunk.end_ms) == (n * 200, (n + 1) * 200)
        if n != 1:
            tap.note_sent(frame)
    assert tap.total_samples == 14400
    assert tap.timestamp("ja", 400, "translation")["audio_time_ms"] == 600
    assert tap.timestamp("en", None, "english")["audio_time_ms"] is None
    assert tap.timestamp("en", 400, "translation")["audio_time_ms"] is None
    assert tap.timestamp("ja", None, "translation")["audio_time_ms"] is None


def test_global_diarization_timestamps_and_malformed_response():
    parsed = OpenAIDiarizer.parse_response(
        {"segments": [{"speaker": "B", "start": 13.72, "end": 20.1, "text": "Hello"}]}, 50000, 30
    )
    assert parsed[0].start_ms == 63720
    for payload in [
        {},
        {"segments": None},
        {"segments": [{"speaker": "A", "start": float("nan"), "end": 5, "text": "bad"}]},
    ]:
        with pytest.raises((ValueError, TypeError, KeyError)):
            OpenAIDiarizer.parse_response(payload, 0, 30)


def test_http_provider_multipart_wav_and_errors(monkeypatch):
    def request(req, timeout):
        assert timeout == 40
        assert b'name="response_format"\r\n\r\ndiarized_json' in req.data
        assert b'name="chunking_strategy"\r\n\r\nauto' in req.data
        assert b"RIFF" in req.data and b"WAVE" in req.data
        return io.BytesIO(
            json.dumps(
                {"segments": [{"speaker": "A", "start": 0, "end": 0.1, "text": "Hello"}]}
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", request)
    provider = OpenAIDiarizer("secret-not-to-be-logged")
    assert provider.diarize(wav_bytes(b"\0" * FRAME_BYTES), 1000)[0].start_ms == 1000

    def error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "secret-not-to-be-logged", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", error)
    with pytest.raises(DiarizationError, match="HTTP 429") as caught:
        provider.diarize(wav_bytes(b"\0" * FRAME_BYTES), 0)
    assert caught.value.retryable
    assert "secret" not in str(caught.value)

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(b'{"segments":null}'))
    with pytest.raises(DiarizationError, match="Malformed") as caught:
        provider.diarize(wav_bytes(b"\0" * FRAME_BYTES), 0)
    assert caught.value.retryable
    # A subsequent valid window can be processed by the same provider.
    monkeypatch.setattr("urllib.request.urlopen", request)
    assert provider.diarize(wav_bytes(b"\0" * FRAME_BYTES), 200)[0].start_ms == 200


def test_speaker_changes_ignore_local_label_identity_and_overlap_speech():
    segments = [
        SpeakerSegment("A", 0, 5000, "First turn"),
        SpeakerSegment("A", 5000, 10000, "Same person"),
        SpeakerSegment("B", 10000, 15000, "I think reliability is very important."),
        SpeakerSegment("C", 12000, 16000, "Overlapping speech"),
    ]
    changes = segment_boundaries(segments, "capture", 0, 30000)
    assert len(changes) == 1 and changes[0].audio_time_ms == 10000


def test_normalization_and_fuzzy_filler_article_difference():
    assert normalize_text("  ＷＥ’ＲＥ, um,  Here！ ") == "were here"
    assert (
        similarity(
            "I think reliability is the biggest challenge",
            "I think um the reliability is the biggest challenge",
        )
        > 0.9
    )


def test_english_alignment_ignores_elapsed_and_network_delay():
    data = history()
    data.append("en", "What do you think about deployment?\n")
    data.append("en", "I think reliability is the biggest challenge.")
    records = data.records()
    records[1]["received_monotonic_ms"] += 70_000
    records[1]["elapsed_ms"] = 999_999
    aligned = align_english(boundary(), records)
    assert aligned and aligned.char_offset == len(records[0]["delta"])
    assert aligned.time_ms is None and aligned.timing == "text"
    assert aligned.confidence >= 0.78


def test_low_similarity_and_repeated_phrase_rejected():
    data = history()
    data.append(
        "en", "What do you think about deployment? Totally unrelated words here about lunch."
    )
    assert align_english(boundary(), data.records()) is None
    data = history()
    phrase = "What do you think about deployment? I think reliability is the biggest challenge. "
    data.append("en", phrase * 2)
    assert align_english(boundary(), data.records()) is None
    match = align_english(boundary(), data.records(), lower=len(phrase))
    assert match.char_offset >= len(phrase)


def test_monotonic_boundaries_and_recent_scope():
    data = history()
    data.append(
        "en", "What do you think about deployment? I think reliability is the biggest challenge. "
    )
    data.append(
        "en", "What should we improve next? We need much better monitoring and deployment tools."
    )
    first, _ = data.speaker_boundaries.merge(boundary())
    first = data.speaker_boundaries.align(first.id, data.records())
    second, _ = data.speaker_boundaries.merge(
        boundary(
            id="next",
            audio_time_ms=20000,
            text_before="What should we improve next?",
            text_hint="We need much better monitoring and deployment tools.",
        )
    )
    second = data.speaker_boundaries.align(second.id, data.records())
    assert second.en_char_offset >= first.positions["en"].match_end
    # The ring worker supplies only recent received text from the same capture epoch.
    assert not data.timeline_records("different", 0, 10**15)
    assert not data.timeline_records("capture", 0, 1)


def test_duplicate_merge_update_preserves_id_and_local_label_swap():
    timeline = BoundaryTimeline()
    first, _ = timeline.merge(boundary(audio_time_ms=28500))
    newer = boundary(
        id="new",
        audio_time_ms=28700,
        speaker_before="B",
        speaker_after="A",
        window_start_ms=25000,
        window_end_ms=55000,
    )
    updated, old = timeline.merge(newer)
    assert old == first and updated.id == first.id
    assert updated.audio_time_ms == 28700 and updated.observations == 2
    assert len(timeline.snapshot()[1]) == 1
    separate, old = timeline.merge(replace(newer, id="another", timeline_id="new-capture"))
    assert old is None and len(timeline.snapshot()[1]) == 2


def test_authoritative_text_never_replaced_and_ja_sentence_break_and_save(tmp_path):
    data = history()
    before = "Let us discuss our biggest operational concern. "
    authoritative = "What we really need to think about is reliability in production."
    data.append("en", before)
    data.append("en", authoritative)
    data.append("ja", "最大の懸念について話しましょう。", 8000)
    data.append("ja", "本当に考えるべきなのは本番環境での信頼性です。", 10000)
    candidate = boundary(
        text_before=before, text_hint=authoritative.replace("in production", "and production")
    )
    candidate, _ = data.speaker_boundaries.merge(candidate)
    candidate = data.speaker_boundaries.align(candidate.id, data.records())
    assert set(candidate.positions) == {"en", "ja"}
    assert data.full_text("en") == before + authoritative
    assert data.rendered_text("en") == before + "\n\n" + authoritative
    assert "\n\n本当に" in data.rendered_text("ja")
    assert "and production" not in data.rendered_text("en")
    path = tmp_path / "subtitles.jsonl"
    data.save(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["kind"] == "speaker_boundary"
    assert rows[1]["delta"] == authoritative and rows[1]["rendered_delta"] == "\n\n" + authoritative
    data.clear_display()
    assert authoritative in data.rendered_text("en")
    data.save(tmp_path / "subtitles.txt")
    assert before + "\n\n" + authoritative in (tmp_path / "subtitles.txt").read_text(
        encoding="utf-8"
    )


def test_japanese_missing_timing_requires_consistent_sentence_lag():
    data = history()
    for n in range(4):
        data.append("en", f"Sentence number {n} has enough words. ")
        data.append("ja", f"文の番号は{n}です。")
    records = data.records()
    for n, record in enumerate(records):
        record["received_monotonic_ms"] = (n // 2) * 4000 + (800 if n % 2 else 0)
    candidate = boundary(
        text_before="Sentence number 1 has enough words.",
        text_hint="Sentence number 2 has enough words.",
    )
    en = align_english(
        candidate, records, lower=records[4]["char_start"], upper=records[4]["char_end"]
    )
    assert en is not None
    ja = align_japanese(candidate, records, en)
    assert ja is not None and ja.sequence == 5 and ja.timing == "receive_sentence_estimate"
    records[3]["received_monotonic_ms"] += 3000
    assert align_japanese(candidate, records, en) is None


def test_slow_or_failed_sidecar_cannot_block_pcm_consumer():
    gate = threading.Event()
    entered = threading.Event()

    class Provider:
        def diarize(self, audio, start):
            entered.set()
            gate.wait(5)
            raise DiarizationError("HTTP 429", True)

    mic = SimpleNamespace(frames=LatestQueue(3))
    sidecar = DiarizationSidecar(
        history(),
        provider_factory=lambda key: Provider(),
        window_sec=0.4,
        overlap_sec=0.2,
        logger=logging.getLogger("test-silent"),
    )
    sidecar.attach(mic, "test", "capture")
    try:
        for n in range(100):
            frame = bytes([n]) * FRAME_BYTES
            mic.frames.put_latest((time.monotonic(), frame))
            assert mic.frames.get_nowait()[1] is frame
            time.sleep(0.002)
        assert entered.wait(2)
        assert sidecar.jobs.qsize() <= 1 and sidecar.frames.qsize() <= 20
        assert sidecar.tap.next_ms == 20000
        assert sidecar.jobs.dropped > 0
    finally:
        gate.set()
        sidecar.close()
        for thread in sidecar._threads:
            thread.join(2)
            assert not thread.is_alive()


def test_api_sidecar_failure_does_not_reconnect_realtime(monkeypatch):
    from test_realtime_api import SyntheticMicrophone, until

    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    class Provider:
        def diarize(self, audio, start):
            raise DiarizationError("HTTP 401")

    async def scenario():
        connections = 0
        frames = []

        async def server(ws):
            nonlocal connections
            connections += 1
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "capture"}}))
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif event["type"] == "session.input_audio_buffer.append":
                    frames.append(event)
                    for kind, text in [("input", "English "), ("output", "日本語。")]:
                        await ws.send(
                            json.dumps({"type": f"session.{kind}_transcript.delta", "delta": text})
                        )
                elif event["type"] == "session.close":
                    await ws.send(json.dumps({"type": "session.closed"}))

        async with serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            client = RealtimeClient(
                endpoint=f"ws://127.0.0.1:{port}",
                source_mode="sidecar",
                microphone_factory=SyntheticMicrophone,
            )
            client._diarization = DiarizationSidecar(
                client.history,
                provider_factory=lambda key: Provider(),
                window_sec=0.4,
                overlap_sec=0.2,
                logger=logging.getLogger("test-silent"),
            )
            try:
                client.start()
                await until(lambda: client.snapshot()["diarization"]["error"] == "HTTP 401")
                count = len(frames)
                await until(lambda: len(frames) >= count + 4)
                assert client.state == State.RUNNING and connections == 1
                assert client.history.full_text("en") and client.history.full_text("ja")
            finally:
                client.stop()
                await until(lambda: not client.active)
                client._diarization.close()

    asyncio.run(scenario())


def test_overlap_can_improve_character_position_without_changing_text():
    from realtime_subtitles.speaker_timeline import Anchor

    data = history()
    text = "What do you think about deployment? I think reliability is the biggest challenge."
    data.append("en", text)
    old = boundary(
        audio_time_ms=28500,
        confidence=0.79,
        en_char_offset=0,
        positions={"en": Anchor(0, 0, 0, 10, 0.79)},
    )
    data.speaker_boundaries.merge(old)
    newer = boundary(
        id="new-window", audio_time_ms=28700, window_start_ms=25000, window_end_ms=55000
    )
    merged, _ = data.speaker_boundaries.merge(newer)
    aligned = data.speaker_boundaries.align(merged.id, data.records())
    assert aligned.id == old.id and aligned.en_char_offset == text.index("I think")
    assert data.full_text("en") == text
    assert len(data.speaker_boundaries.snapshot()[1]) == 1


def test_ja_rejects_word_middle_and_unknown_audio_mapping():
    data = history()
    data.append("en", "What do you think about deployment? ")
    data.append("en", "I think reliability is the biggest challenge.")
    data.append("ja", "本番環境での", 8000)
    data.append("ja", "信頼性についてです。", 10000)
    en = align_english(boundary(), data.records())
    assert en is not None
    assert align_japanese(boundary(), data.records(), en) is None


def test_broken_tap_does_not_escape_into_audio_conversion():
    class BrokenQueue:
        def put_latest(self, item):
            raise RuntimeError("synthetic queue failure")

    tap = AudioTimelineTap("capture", BrokenQueue())
    source = LatestQueue(3)
    source.sinks = (tap,)
    pcm = b"\0" * FRAME_BYTES
    source.put_latest((0, pcm))
    assert source.get_nowait()[1] is pcm
    assert not tap.active and tap.error
