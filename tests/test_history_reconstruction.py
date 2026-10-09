import asyncio
import json
from dataclasses import asdict

import pytest
from conftest import make_unit

from realtime_subtitles.history_reconstruction import (
    BatchTranslation,
    EnglishSplit,
    ReconstructedParagraph,
    ReconstructionTranslator,
    ReconstructionWorker,
    pair_translations,
    split_english,
)
from realtime_subtitles.text_translation import TranslationWorker
from realtime_subtitles.translation_history import TranslationHistory


def add(h, text, speaker="S1", session="s", start=None):
    start = len(h.segments()) if start is None else start
    return make_unit(
        h,
        {
            "message": "AddSegment",
            "segment": {"transcript": text, "speaker": speaker},
            "metadata": {"start_time": start, "end_time": start + 1},
        },
        session,
    )


@pytest.mark.parametrize("text", ["The landscape is.", "Independent sentence.", "Yes."])
def test_no_punctuation_or_fragment_heuristic_before_llm(text):
    h = TranslationHistory()
    add(h, text)
    assert h.reconstructions.plan(add(h, "Next unit.", start=100)) is not None


def test_revision_preserves_original_en_ja_context_and_save(tmp_path):
    h = TranslationHistory()
    add(h, "Earlier context.", speaker="S0")
    add(h, "The landscape is")
    h.update_translation(1, "completed", text="状況は……")
    second = add(h, "changing rapidly.")
    h.update_translation(2, "completed", text="急速に変化しています。")
    original = [asdict(u) for u in h.segments()]
    target = h.reconstructions.plan(second)
    assert target.en_text == "The landscape is changing rapidly."
    assert target.source_segment_ids == (1, 2)
    assert h.reconstructions.context_for(0) == [{"speaker": "S0", "text": "Earlier context."}]
    assert h.reconstructions.previous_translations(0) == []
    assert h.reconstructions.plan(second) is None
    h.reconstructions.set_paragraphs(
        0, (ReconstructedParagraph(0, len(target.en_text), "状況は急速に変化しています。"),)
    )
    h.reconstructions.update_translation(0, "completed", text="状況は急速に変化しています。")
    assert [asdict(u) for u in h.segments()] == original
    assert h.saved_text().count("状況は急速に変化しています。") == 1
    assert "reconstructed units 1,2" in h.saved_text()
    h.save(tmp_path / "history.jsonl")
    rows = [
        json.loads(row)
        for row in (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([r for r in rows if r["kind"] == "translation_unit"]) == 3
    revisions = [r for r in rows if r["kind"] == "history_revision"]
    assert revisions[0]["unit_ids"] == [1, 2]
    assert revisions[0]["translation"]["ja_text"] == "状況は急速に変化しています。"
    journal, _ = h.autosave_updates(0)
    assert len([r for r in journal if r["kind"] == "history_revision"]) == 2


@pytest.mark.parametrize("change", [{"speaker": "S2"}, {"session": "new"}])
def test_boundaries_prevent_reconstruction(change):
    h = TranslationHistory()
    add(h, "The landscape is")
    assert h.reconstructions.plan(add(h, "changing.", **change)) is None


@pytest.mark.parametrize("first", ["UU", "SU", None, ""])
@pytest.mark.parametrize("second", ["UU", "SU", None, ""])
def test_unknown_speaker_reconstruction_keeps_sources_and_applies(first, second):
    h = TranslationHistory()
    add(h, "The landscape is", speaker=first)
    unit = add(h, "changing.", speaker=second)
    originals = [asdict(s) for s in h.sources()]
    target = h.reconstructions.plan(unit)
    assert target.en_text == "The landscape is changing."
    assert target.speaker == second
    assert h.reconstructions.prepare(target.unit_id) == target
    complete(h, target, [target.en_text], ["状況は変化しています。"])
    assert h.reconstructions.entries()[0].applied
    assert h.reconstructions.effective_blocks()[0].ja_text == "状況は変化しています。"
    assert [asdict(s) for s in h.sources()] == originals
    assert [u.speaker for u in h.segments()] == [first, second]


def test_carried_unknown_tail_can_continue_without_crossing_new_speaker_or_session():
    h = TranslationHistory()
    add(h, "Known speaker.")
    first = add(h, "The landscape is", speaker="UU")
    assert h.reconstructions.plan(first) is not None
    target = h.reconstructions.plan(add(h, "changing.", speaker=None))
    complete(h, target, [target.en_text])
    next_target = h.reconstructions.plan(add(h, "Rapidly.", speaker="SU"))
    assert next_target.en_text == "Known speaker. The landscape is changing. Rapidly."
    complete(h, next_target, [next_target.en_text])
    assert h.reconstructions.plan(add(h, "Another session.", speaker="UU", session="new")) is None
    assert h.reconstructions.plan(add(h, "Known again.", speaker="S2", session="new")) is None
    assert h.reconstructions.effective_blocks()[0].en_text == next_target.en_text


def test_clear_and_length_limit():
    h = TranslationHistory()
    add(h, "The landscape is")
    h.clear_display()
    assert h.reconstructions.plan(add(h, "changing.")) is None
    h = TranslationHistory()
    add(h, "Very long " * 300)
    assert h.reconstructions.plan(add(h, "continuation.")) is None


def complete(h, target, texts, japanese=None):
    paragraphs, offset = [], 0
    for i, text in enumerate(texts):
        start = target.en_text.index(text, offset)
        end = start + len(text)
        paragraphs.append(ReconstructedParagraph(start, end, japanese[i] if japanese else f"訳{i}"))
        offset = end
    h.reconstructions.set_paragraphs(target.unit_id, tuple(paragraphs))
    h.reconstructions.update_translation(
        target.unit_id, "completed", text="\n\n".join(p.ja_text for p in paragraphs)
    )


def test_out_of_order_overlapping_response_cannot_erase_newer_paragraph():
    h = TranslationHistory()
    add(h, "First sentence.")
    a = h.reconstructions.plan(add(h, "Second sentence."))
    b = h.reconstructions.plan(add(h, "Third sentence."))
    complete(h, b, [b.en_text], ["新しいまとまり。"])
    complete(h, a, [a.en_text], ["遅い古い訳。"])
    blocks = h.reconstructions.effective_blocks()
    assert [b.en_text for b in blocks] == ["First sentence. Second sentence. Third sentence."]
    assert blocks[0].ja_text == "新しいまとまり。"
    assert not h.reconstructions.entries()[0].applied


def test_revision_validator_failure_retains_originals_and_both_candidates():
    h = TranslationHistory()
    add(h, "The price is")
    target = h.reconstructions.plan(add(h, "$2 million."))
    calls = []

    class BadTranslator:
        async def translate(self, target, context, **kwargs):
            calls.append((target.en_text, kwargs))
            return "200万トークンです。", None

    worker = TranslationWorker(h.reconstructions, "test")
    asyncio.run(worker._one(BadTranslator(), target, []))
    revision = h.reconstructions.entries()[0]
    assert len(calls) == 2
    assert revision.translation.translation_status == "validation_failed"
    assert len(revision.translation.candidates) == 2
    assert [u.en_text for u in h.reconstructions.effective_blocks()] == [
        "The price is",
        "$2 million.",
    ]
    assert h.statistics() == {"pending": 2}


def test_reconstruction_queue_bounded_and_independent():
    h = TranslationHistory()
    worker = TranslationWorker(h.reconstructions, "test", concurrency=1, queue_size=1)
    add(h, "The landscape is")
    a = h.reconstructions.plan(add(h, "changing and"))
    b = h.reconstructions.plan(add(h, "growing rapidly."))
    assert worker.submit(a) and not worker.submit(b)
    assert worker.jobs.qsize() == 1
    assert h.reconstructions.entries()[1].translation.translation_status == "skipped"
    assert h.statistics() == {"pending": 3}
    h.set_partial("Live English keeps going")
    assert h.subtitle_view().en_text == "Live English keeps going"


async def reconstruct(h, target, outputs, context=None, observer=None):
    from types import SimpleNamespace

    translator = ReconstructionTranslator("offline", h.reconstructions)
    await translator.client.close()
    calls = []

    async def parse(**kwargs):
        calls.append(kwargs)
        if observer:
            observer(len(calls))
        value = outputs[len(calls) - 1]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, SimpleNamespace):
            return value
        return SimpleNamespace(
            status="completed",
            usage=None,
            output_parsed=kwargs["text_format"].model_validate(value),
        )

    translator.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    await ReconstructionWorker(h.reconstructions, "offline")._one(translator, target, context or [])
    return calls


def batch(*texts):
    return {"translations": [{"id": i, "ja": text} for i, text in enumerate(texts)]}


def test_split_before_translation_and_ids_restore_order_without_rewriting_source():
    h = TranslationHistory()
    add(h, "The landscape is")
    h.update_translation(0, "completed", text="状況は……")
    target = h.reconstructions.plan(add(h, "changing. Reliability matters."))
    original = [asdict(u) for u in h.segments()]
    context = h.reconstructions.context_for(target.unit_id)
    translation = batch("状況は変化しています。", "信頼性が重要です。")
    translation["translations"].reverse()

    def observer(call):
        revision = h.reconstructions.entries()[0]
        assert revision.translation.translation_status != "completed"
        assert len(h.reconstructions.effective_blocks()) == 2
        if call == 2:
            assert len(revision.chunks) == 2  # Fixed before any Japanese is requested.
            assert not revision.paragraphs

    calls = asyncio.run(
        reconstruct(
            h,
            target,
            [
                {"end_tokens": [3, 5]},
                translation,
            ],
            context,
            observer,
        )
    )
    assert len(calls) == 2
    assert calls[0]["text_format"] is EnglishSplit
    assert calls[1]["text_format"] is BatchTranslation
    for call in calls:
        assert call["reasoning"] == {"effort": "none"} and call["model"] == "gpt-6-luna"
        assert call["store"] is False
        assert json.loads(call["input"])["CONTEXT"] == context
    data = json.loads(calls[0]["input"])
    assert data["FULL_ENGLISH"] == target.en_text
    assert "REVIEW_UNITS" not in data
    assert data["ENGLISH_TOKENS"][3] == {"index": 3, "text": "changing."}
    assert json.loads(calls[1]["input"])["TARGETS"] == [
        {"id": 0, "text": "The landscape is changing."},
        {"id": 1, "text": "Reliability matters."},
    ]
    revision = h.reconstructions.entries()[0]
    assert revision.translation.translation_status == "completed"
    assert [d["stage"] for d in revision.decisions] == ["split", "translate"]
    assert [asdict(u) for u in h.segments()] == original
    saved = h.saved_text()
    assert "EN: The landscape is changing.\nJA: 状況は変化しています。" in saved
    assert "EN: Reliability matters.\nJA: 信頼性が重要です。" in saved


@pytest.mark.parametrize("ends", [[0], [0, 0, 2], [3], [-1, 2], [], [2, 1]])
def test_reject_invalid_english_coverage(ends):
    with pytest.raises(ValueError):
        split_english("Three original tokens.", EnglishSplit(end_tokens=ends))


@pytest.mark.parametrize("ids", [[0], [0, 0], [0, 2], [0, 1, 2], []])
def test_reject_invalid_translation_ids(ids):
    chunks = split_english("First. Second.", EnglishSplit(end_tokens=[0, 1]))
    with pytest.raises(ValueError):
        pair_translations(
            chunks, BatchTranslation(translations=[{"id": i, "ja": "訳"} for i in ids])
        )


@pytest.mark.parametrize("field", ["index", "id"])
def test_indexes_cannot_be_coerced_from_bool_or_string(field):
    for value in [True, "0", 0.5]:
        with pytest.raises(ValueError):
            if field == "index":
                EnglishSplit(end_tokens=[value])
            else:
                BatchTranslation(translations=[{"id": value, "ja": "訳"}])


def test_empty_japanese_rejected():
    chunks = split_english("Yes.", EnglishSplit(end_tokens=[0]))
    with pytest.raises(ValueError):
        pair_translations(chunks, BatchTranslation.model_validate(batch(" \n ")))


def test_unicode_whitespace_and_punctuation_preserved():
    en = "  OpenAI’s 🚀  reliability in production.\nReally?  "
    chunks = split_english(en, EnglishSplit(end_tokens=[4, 5]))
    parts = pair_translations(
        chunks, BatchTranslation.model_validate(batch("本番での信頼性。", "本当？"))
    )
    assert en[parts[0].en_start : parts[0].en_end] == "OpenAI’s 🚀  reliability in production."
    assert en[parts[1].en_start : parts[1].en_end] == "Really?"


@pytest.mark.parametrize("valid_retry", [True, False])
def test_retry_translates_only_frozen_chunks_and_preserves_candidates(valid_retry):
    h = TranslationHistory()
    add(h, "The price is")
    target = h.reconstructions.plan(add(h, "$2 million."))
    outputs = [
        {"end_tokens": [4]},
        batch("200万トークンです。"),
        batch("価格は200万ドルです。" if valid_retry else "200万トークンです。"),
    ]
    calls = asyncio.run(reconstruct(h, target, outputs))
    assert len(calls) == 3
    assert calls[1]["input"] == calls[2]["input"]
    assert calls[1]["instructions"] != calls[2]["instructions"]
    revision = h.reconstructions.entries()[0]
    assert [d["stage"] for d in revision.decisions] == ["split", "translate", "translate"]
    assert revision.translation.retry_count == 1
    assert len(revision.translation.candidates) == 2
    assert revision.translation.translation_status == (
        "completed" if valid_retry else "validation_failed"
    )
    if not valid_retry:
        assert len(h.reconstructions.effective_blocks()) == 2
    assert h.segments()[1].en_text == "$2 million."
    with pytest.raises(ValueError, match="fixed"):
        h.reconstructions.set_chunks(
            0, split_english(target.en_text, EnglishSplit(end_tokens=[0, 4]))
        )


def test_chunk_validation_detects_currency_swapped_between_chunks():
    h = TranslationHistory()
    add(h, "$2 million.")
    target = h.reconstructions.plan(add(h, "2 million tokens."))
    bad = batch("200万トークン。", "200万ドル。")
    calls = asyncio.run(reconstruct(h, target, [{"end_tokens": [1, 4]}, bad, bad]))
    assert len(calls) == 3  # Whole-passage quantities would incorrectly balance out.
    revision = h.reconstructions.entries()[0]
    assert revision.translation.translation_status == "validation_failed"
    assert any(i["message"].startswith("Chunk 0:") for i in revision.translation.validation_issues)


@pytest.mark.parametrize(
    "outputs,expected_calls",
    [
        ([{"end_tokens": [0]}], 1),  # Invalid split must never be translated.
        ([{"end_tokens": [1]}, batch()], 2),  # Missing id: preserve old display.
        ([{"end_tokens": [1]}, ValueError("sensitive")], 2),
    ],
)
def test_structural_failure_retains_originals(outputs, expected_calls):
    h = TranslationHistory()
    add(h, "Still")
    target = h.reconstructions.plan(add(h, "live."))
    calls = asyncio.run(reconstruct(h, target, outputs))
    assert len(calls) == expected_calls
    revision = h.reconstructions.entries()[0]
    assert revision.translation.translation_status == "failed"
    assert "sensitive" not in revision.translation.translation_error
    assert len(h.reconstructions.effective_blocks()) == 2
    h.set_partial("Latest partial")
    assert h.subtitle_view().en_text == "Latest partial"


def test_transport_retry_after_split_never_resplits():
    h = TranslationHistory()
    add(h, "Still")
    target = h.reconstructions.plan(add(h, "live."))
    calls = asyncio.run(
        reconstruct(
            h, target, [{"end_tokens": [1]}, ConnectionError("secret"), batch("まだ続いています。")]
        )
    )
    assert len(calls) == 3 and calls[1]["input"] == calls[2]["input"]
    assert h.reconstructions.entries()[0].translation.translation_status == "completed"


def test_source_in_production_not_changed_to_and_production():
    en = "What we really need to think about is reliability in production."
    chunks = split_english(en, EnglishSplit(end_tokens=[len(en.split()) - 1]))
    parts = pair_translations(chunks, BatchTranslation.model_validate(batch("本番環境の信頼性。")))
    assert en[parts[0].en_start : parts[0].en_end] == en


def test_reconstruction_failure_does_not_block_original_translation_or_partial():
    from types import SimpleNamespace

    from realtime_subtitles.live_client import LiveClient
    from realtime_subtitles.speechmatics_api import SegmentHistory

    client = LiveClient()
    client.speechmatics = SimpleNamespace(history=SegmentHistory(), session_id="s")
    jobs = []
    client.translation = SimpleNamespace(submit=jobs.append)

    def broken_submit(target):
        raise RuntimeError("sidecar unavailable")

    client.reconstruction = SimpleNamespace(submit=broken_submit)
    for i in range(4):
        client._receive(
            {
                "message": "AddSegment",
                "segment": {"transcript": f"English {i}.", "speaker": "S1"},
                "metadata": {"start_time": i, "end_time": i + 1},
            },
            (),
        )
    client._receive(
        {"message": "AddPartialSegment", "segment": {"transcript": "Still live", "speaker": "S1"}},
        (),
    )
    assert len(jobs) == 2 and len(client.history.sources()) == 4
    assert client.history.subtitle_view().en_text == "Still live"
    assert client.reconstruction_error == "RuntimeError"


def test_autosave_preserves_originals_and_partitioned_revision(tmp_path):
    from realtime_subtitles.autosave import TranscriptAutosave

    h = TranslationHistory()
    add(h, "The landscape is")
    target = h.reconstructions.plan(add(h, "changing. Reliability matters."))
    asyncio.run(
        reconstruct(
            h,
            target,
            [{"end_tokens": [3, 5]}, batch("状況は変わっています。", "信頼性が重要です。")],
        )
    )
    saver = TranscriptAutosave({"subtitles": h}, directory=tmp_path)
    assert saver.close(5)
    rows = [
        json.loads(line)
        for line in (saver.directory / "subtitles.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    records = [row["record"] for row in rows if row["kind"] == "transcript"]
    assert len([r for r in records if r["kind"] == "raw_source_segment"]) == 2
    assert len([r for r in records if r["kind"] == "translation_unit"]) == 2
    revisions = [r for r in records if r["kind"] == "history_revision"]
    assert len(revisions[-1]["chunks"]) == 2
    assert [d["stage"] for d in revisions[-1]["decisions"]] == ["split", "translate"]
    assert revisions[-1]["paragraphs"][1]["ja_text"] == "信頼性が重要です。"
    saved = (saver.directory / "subtitles.txt").read_text(encoding="utf-8")
    assert "EN: The landscape is changing.\nJA: 状況は変わっています。" in saved


def test_cancel_during_batch_retains_split_and_original_display():
    h = TranslationHistory()
    add(h, "Still")
    target = h.reconstructions.plan(add(h, "live."))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reconstruct(h, target, [{"end_tokens": [1]}, asyncio.CancelledError()]))
    revision = h.reconstructions.entries()[0]
    assert revision.chunks and not revision.paragraphs
    assert revision.translation.translation_status == "cancelled"
    assert [d["status"] for d in revision.decisions] == ["completed", "cancelled"]
    assert len(h.reconstructions.effective_blocks()) == 2


@pytest.mark.parametrize("status", ["incomplete", "completed"])
def test_refusal_or_missing_parsed_response_is_not_applied(status):
    from types import SimpleNamespace

    h = TranslationHistory()
    add(h, "Still")
    target = h.reconstructions.plan(add(h, "live."))
    calls = asyncio.run(
        reconstruct(h, target, [SimpleNamespace(status=status, usage=None, output_parsed=None)])
    )
    assert len(calls) == 1
    revision = h.reconstructions.entries()[0]
    assert revision.translation.translation_status == "failed"
    assert revision.decisions[0]["decision"] is None
    assert not revision.chunks


def test_failed_extension_keeps_previously_valid_revision():
    h = TranslationHistory()
    add(h, "The landscape is")
    target = h.reconstructions.plan(add(h, "changing."))
    asyncio.run(reconstruct(h, target, [{"end_tokens": [3]}, batch("状況は変化しています。")]))
    extension = h.reconstructions.plan(add(h, "Reliability matters."))
    asyncio.run(reconstruct(h, extension, [{"end_tokens": [3, 5]}, batch()]))
    effective = h.reconstructions.effective_blocks()
    assert len(effective) == 2 and effective[0].start == (0, 0)
    assert effective[0].ja_text == "状況は変化しています。"
    assert effective[1].en_text == "Reliability matters."


def test_usage_sums_split_and_translation_attempts():
    from types import SimpleNamespace

    h = TranslationHistory()
    add(h, "Still")
    target = h.reconstructions.plan(add(h, "live."))

    def response(schema, data, count):
        return SimpleNamespace(
            status="completed",
            output_parsed=schema.model_validate(data),
            usage=SimpleNamespace(
                model_dump=lambda: {
                    "input_tokens": count,
                    "output_tokens": 2,
                    "total_tokens": count + 2,
                }
            ),
        )

    asyncio.run(
        reconstruct(
            h,
            target,
            [
                response(EnglishSplit, {"end_tokens": [1]}, 10),
                response(BatchTranslation, batch("まだ続いています。"), 20),
            ],
        )
    )
    usage = h.reconstructions.entries()[0].translation.usage
    assert usage == {"input_tokens": 30, "output_tokens": 4, "total_tokens": 34}


def test_recent_two_paragraphs_can_change_preserving_older_prefix_and_context(tmp_path):
    h = TranslationHistory()
    add(h, "Earlier context.", speaker="S0")
    add(h, "First complete thought.")
    old = h.reconstructions.plan(add(h, "Another thought. With our."))
    complete(
        h,
        old,
        ["First complete thought.", "Another thought.", "With our."],
        ["最初の訳。", "前半の訳。", "当社の"],
    )
    originals = [asdict(u) for u in h.segments()]
    target = h.reconstructions.plan(add(h, "new platform, we can scale."))
    assert target.en_text == "Another thought. With our. new platform, we can scale."
    assert h.reconstructions.entries()[-1].first_unit_offset == 0
    assert h.reconstructions.context_for(target.unit_id)[-1]["text"] == "First complete thought."
    complete(
        h,
        target,
        ["Another thought.", "With our. new platform, we can scale."],
        ["前半の訳。", "当社の新しい基盤で拡張できます。"],
    )
    blocks = h.reconstructions.effective_blocks()
    assert [b.en_text for b in blocks] == [
        "Earlier context.",
        "First complete thought.",
        "Another thought.",
        "With our. new platform, we can scale.",
    ]
    assert [b.ja_text for b in blocks[1:3]] == ["最初の訳。", "前半の訳。"]
    assert [asdict(u) for u in h.segments()[:3]] == originals
    saved = h.saved_text()
    for text in ["Another thought.", "With our.", "new platform"]:
        assert saved.count(text) == 1
    h.save(tmp_path / "tail.jsonl")
    rows = [
        json.loads(line)
        for line in (tmp_path / "tail.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    r = [r for r in rows if r["kind"] == "history_revision"][-1]
    assert r["parent_revision_id"] == old.unit_id and r["applied"]
    assert r["first_unit_offset"] == 0


def test_queued_job_resolves_tail_after_preceding_job_completes():
    h = TranslationHistory()
    add(h, "Opening.")
    a = h.reconstructions.plan(add(h, "A complete thought. We can"))
    b = h.reconstructions.plan(add(h, "continue."))  # Parent still queued.
    complete(h, a, ["Opening.", "A complete thought.", "We can"])
    # Run the actual worker: it must replace the stale queued target/context.
    calls = asyncio.run(
        reconstruct(h, b, [{"end_tokens": [2, 5]}, batch("一つの考え。", "続けられます。")])
    )
    assert json.loads(calls[0]["input"])["FULL_ENGLISH"] == "A complete thought. We can continue."
    assert json.loads(calls[0]["input"])["CONTEXT"][-1]["text"] == "Opening."
    assert h.reconstructions.entries()[b.unit_id].translation.en_text == (
        "A complete thought. We can continue."
    )
    assert [b.en_text for b in h.reconstructions.effective_blocks()] == [
        "Opening.",
        "A complete thought.",
        "We can continue.",
    ]


@pytest.mark.parametrize("status", ["failed", "skipped", "cancelled", "validation_failed"])
def test_failed_tail_job_keeps_both_prefix_and_old_tail(status):
    h = TranslationHistory()
    add(h, "Prefix.")
    a = h.reconstructions.plan(add(h, "With our"))
    complete(h, a, ["Prefix.", "With our"])
    old = h.reconstructions.effective_blocks()
    b = h.reconstructions.plan(add(h, "new tool."))
    h.reconstructions.update_translation(b.unit_id, status)
    assert h.reconstructions.effective_blocks()[:2] == old
    assert h.reconstructions.effective_blocks()[2].en_text == "new tool."
    c = h.reconstructions.plan(add(h, "Next sentence."))
    assert c.en_text == "With our new tool. Next sentence."
    h.set_partial("Latest speech")
    assert h.subtitle_view().en_text == "Latest speech"


def test_fragment_count_does_not_cut_off_continuation():
    h = TranslationHistory()
    add(h, "Word0")
    for i in range(1, 12):
        t = h.reconstructions.plan(add(h, f"Word{i}"))
        complete(h, t, [t.en_text])
    assert len(h.reconstructions.entries()[-1].unit_ids) == 12
    assert " ".join(b.en_text for b in h.reconstructions.effective_blocks()) == (
        " ".join(f"Word{i}" for i in range(12))
    )


def test_queued_tail_can_hit_character_limit_after_parent_completion():
    h = TranslationHistory()
    add(h, "A" * 700)
    a = h.reconstructions.plan(add(h, "B" * 700))
    b = h.reconstructions.plan(add(h, "C" * 700))
    assert b is not None  # Reservation was 1401 chars before A+B completed.
    complete(h, a, [a.en_text])
    assert h.reconstructions.prepare(b.unit_id) is None
    assert h.reconstructions.entries()[b.unit_id].translation.translation_status == "skipped"
    assert len(h.reconstructions.effective_blocks()) == 2


@pytest.mark.parametrize("boundary", ["clear", "session", "speaker"])
def test_tail_never_crosses_display_or_speaker_session_boundaries(boundary):
    h = TranslationHistory()
    add(h, "First.")
    a = h.reconstructions.plan(add(h, "With our"))
    complete(h, a, ["First.", "With our"])
    kwargs = {}
    if boundary == "clear":
        h.clear_display()
    elif boundary == "session":
        kwargs["session"] = "next"
    else:
        kwargs["speaker"] = "S2"
    assert h.reconstructions.plan(add(h, "Next speech.", **kwargs)) is None


def test_clear_invalidates_queued_tail_without_erasing_saved_history():
    h = TranslationHistory()
    add(h, "First.")
    t = h.reconstructions.plan(add(h, "Second."))
    h.clear_display()
    assert h.reconstructions.prepare(t.unit_id) is None
    assert len(h.reconstructions.effective_blocks()) == 2


def test_new_continuation_can_correct_boundary_inside_previously_applied_paragraph():
    h = TranslationHistory()
    prefix = "I hope. You will become part of Japan's next phase of growth."
    add(h, prefix + " If you are")
    a = h.reconstructions.plan(add(h, "looking at entering the Japanese market. Jetro. Can"))
    complete(h, a, [a.en_text])
    b = h.reconstructions.plan(add(h, "provide a wide range of support."))
    ends = [len(prefix.split()) - 1, len(b.en_text.split()) - 1]
    calls = asyncio.run(
        reconstruct(
            h, b, [{"end_tokens": ends}, batch("成長の一員となることを願います。", "支援します。")]
        )
    )
    data = json.loads(calls[1]["input"])
    assert data["TARGETS"] == [
        {"id": 0, "text": prefix},
        {
            "id": 1,
            "text": (
                "If you are looking at entering the Japanese market. Jetro. Can "
                "provide a wide range of support."
            ),
        },
    ]
    assert h.reconstructions.entries()[-1].applied
    assert len(h.reconstructions.effective_blocks()) == 2


def test_revisable_tail_never_repairs_invalid_raw_coverage():
    with pytest.raises(ValueError, match="coverage"):
        split_english(
            "Please. Dial in. Now.",
            EnglishSplit(end_tokens=[0, 2]),
        )


def test_or_branch_can_rejoin_previous_list_in_second_recent_paragraph():
    h = TranslationHistory()
    add(h, "Older unchanged thought.", speaker="S0")
    add(h, "We help establish a Japanese subsidiary.")
    a = h.reconstructions.plan(add(h, "Or branch."))
    complete(h, a, ["We help establish a Japanese subsidiary.", "Or branch."])
    b = h.reconstructions.plan(add(h, "And support. For collaboration with Japanese companies."))
    assert b.en_text.startswith("We help establish a Japanese subsidiary. Or branch.")
    complete(h, b, [b.en_text])
    assert len(h.reconstructions.effective_blocks()) == 2
    assert h.reconstructions.effective_blocks()[0].en_text == "Older unchanged thought."


def test_character_budget_uses_one_paragraph_when_two_would_overflow():
    h = TranslationHistory()
    prefix = "A" * 1000
    tail = "B" * 500
    add(h, prefix)
    a = h.reconstructions.plan(add(h, tail))
    complete(h, a, [prefix, tail])
    b = h.reconstructions.plan(add(h, "C" * 500))
    assert b.en_text == tail + " " + "C" * 500
    complete(h, b, [b.en_text])
    assert h.reconstructions.effective_blocks()[0].en_text == prefix


def test_review_second_paragraph_cannot_cross_prior_speaker_boundary():
    h = TranslationHistory()
    add(h, "Other speaker.", speaker="S0")
    add(h, "We need", speaker="S1")
    target = h.reconstructions.plan(add(h, "more capacity.", speaker="S1"))
    assert target.en_text == "We need more capacity."
