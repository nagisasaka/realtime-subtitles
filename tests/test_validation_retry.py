import asyncio
from copy import deepcopy

import pytest

from realtime_subtitles.text_translation import TranslationWorker
from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory
from realtime_subtitles.translation_validation import TranslationValidator


def setup_unit(text="$2 million", *, speaker="S1"):
    h = TranslationHistory()
    source = h.record_segment(
        {
            "message": "AddSegment",
            "segment": {"transcript": text, "speaker": speaker},
            "metadata": {"start_time": 1, "end_time": 2},
        },
        "s",
    )
    unit = h.emit_unit([source], "test", round(h.clock() * 1000))
    return h, unit


@pytest.mark.parametrize(
    ("outputs", "status", "retries"),
    [
        (["200万ドル"], "completed", 0),
        (["200万トークン", "200万ドル"], "completed", 1),
        (["200万トークン", "200万円"], "validation_failed", 1),
        (["", "200万ドル"], "completed", 1),
    ],
)
def test_quality_retry_bounded_preserves_inputs_and_candidates(outputs, status, retries):
    h, unit = setup_unit()
    context = [{"speaker": "S1", "text": "Keep the stated price."}]
    original = deepcopy(context)
    calls = []

    class Translator:
        async def translate(self, target, ctx, **kwargs):
            calls.append((target, ctx, kwargs))
            return outputs[len(calls) - 1], {"output_tokens": 4}

    worker = TranslationWorker(h, "test")
    asyncio.run(worker._one(Translator(), unit, context))
    (result,) = h.segments()
    assert result.translation_status == status and result.retry_count == retries
    assert len(calls) == retries + 1 == len(result.candidates)
    assert result.candidates[0]["text"] == outputs[0]
    assert all(t is unit and c is context for t, c, _ in calls)
    assert context == original and h.sources()[0].en_text == "$2 million"
    assert result.source_segment_ids == (0,)
    assert result.translation_latency_ms >= 0 and result.validation_latency_ms >= 0
    assert result.end_to_end_ja_latency_ms >= result.assembler_hold_ms
    if retries:
        assert "currency USD" in calls[1][2]["retry_instruction"] or not outputs[0]
    if status == "validation_failed":
        assert result.ja_text is None and result.validation_status == "failed"
    else:
        assert result.ja_text == "200万ドル"


def test_warning_does_not_retry():
    h, unit = setup_unit("2 seconds")

    class Translator:
        async def translate(self, target, context):
            return "二秒", None

    asyncio.run(TranslationWorker(h, "test")._one(Translator(), unit, []))
    (result,) = h.segments()
    assert result.translation_status == "completed" and result.validation_status == "warning"
    assert result.retry_count == 0 and result.ja_text == "二秒"


def test_validator_failure_isolated_and_candidate_saved():
    h, unit = setup_unit()

    class BrokenValidator:
        def validate(self, *args, **kwargs):
            raise RuntimeError("private diagnostic details")

    class Translator:
        async def translate(self, target, context):
            return "200万ドル", None

    worker = TranslationWorker(h, "test", validator=BrokenValidator())
    asyncio.run(worker._one(Translator(), unit, []))
    (result,) = h.segments()
    assert result.translation_status == "validation_failed"
    assert result.candidates[0]["text"] == "200万ドル"
    assert "private" not in worker.error
    h.set_partial("English keeps updating.")
    assert h.display_snapshot()[3] == "English keeps updating."


def test_validation_retry_does_not_block_other_request_or_english():
    h, first = setup_unit()
    source = h.record_segment(
        {
            "message": "AddSegment",
            "segment": {"transcript": "A second sentence.", "speaker": "S1"},
            "metadata": {"start_time": 2, "end_time": 3},
        },
        "s",
    )
    second = h.emit_unit([source], "test", round(h.clock() * 1000))

    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()

        class Translator:
            async def translate(self, target, context, **kwargs):
                if target.sequence_id == 0:
                    if not kwargs:
                        return "200万トークン", None
                    waiting.set()
                    await release.wait()
                    return "200万ドル", None
                return "二つ目の文です。", None

        worker = TranslationWorker(h, "test")
        task = asyncio.create_task(worker._one(Translator(), first, []))
        await waiting.wait()
        h.set_partial("Fresh English partial")
        await worker._one(Translator(), second, [])
        assert h.segments()[1].translation_status == "completed"
        assert h.segments()[0].translation_status == "retrying"
        assert h.display_snapshot()[3] == "Fresh English partial"
        release.set()
        await task
        assert [s.ja_text for s in h.segments()] == ["200万ドル", "二つ目の文です。"]

    asyncio.run(run())


def test_audio_latency_estimate_is_distinct_from_receive_time():
    now = [10.0]
    h = TranslationHistory(clock=lambda: now[0])
    h.note_audio_frame("s", 5.2, 4800)
    source = h.record_segment(
        {
            "message": "AddSegment",
            "segment": {"transcript": "Yes.", "speaker": "S1"},
            "metadata": {"start_time": 1, "end_time": 2},
        },
        "s",
    )
    assert source.received_monotonic_ms == 10000
    assert source.estimated_audio_end_monotonic_ms == 7000
    a = TranslationUnitAssembler(h.emit_unit, clock=h.clock)
    a.accept(source)
    a.flush("eos")
    (unit,) = h.segments()

    class Translator:
        async def translate(self, target, context):
            now[0] += 0.1
            return "はい。", None

    asyncio.run(TranslationWorker(h, "test", clock=h.clock)._one(Translator(), unit, []))
    (result,) = h.segments()
    assert result.end_to_end_ja_latency_ms == 100
    assert result.audio_end_to_end_ja_latency_ms == 3100
    assert result.latency_origin == "first_source_segment_received"
    h.note_audio_frame("s", 10.2, 4800, healthy=False)
    assert h._audio_anchors["s"] is None


def test_known_corrupt_latin_is_not_a_blanket_english_ban():
    v = TranslationValidator()
    assert any(x.severity == "error" for x in v.validate("The landscape is", "状況はcuntegn"))
    assert not v.validate("Use Model Armor and mTLS.", "Model ArmorとmTLSを使います。")
