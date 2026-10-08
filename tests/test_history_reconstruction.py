import asyncio
import json
from dataclasses import asdict

import pytest
from conftest import make_unit

from realtime_subtitles.history_reconstruction import (
    ParagraphDecision,
    ReconstructedParagraph,
    ReconstructionDecision,
    ReconstructionTranslator,
    align_paragraphs,
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
    add(h, "Earlier context.")
    add(h, "The landscape is")
    h.update_translation(1, "completed", text="状況は……")
    second = add(h, "changing rapidly.")
    h.update_translation(2, "completed", text="急速に変化しています。")
    original = [asdict(u) for u in h.segments()]
    target = h.reconstructions.plan(second)
    assert target.en_text == "The landscape is changing rapidly."
    assert target.source_segment_ids == (1, 2)
    assert h.reconstructions.context_for(0) == [{"speaker": "S1", "text": "Earlier context."}]
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


@pytest.mark.parametrize("change", [{"speaker": "S2"}, {"speaker": "UU"}, {"session": "new"}])
def test_boundaries_prevent_reconstruction(change):
    h = TranslationHistory()
    add(h, "The landscape is")
    assert h.reconstructions.plan(add(h, "changing.", **change)) is None


def test_clear_and_length_limit():
    h = TranslationHistory()
    add(h, "The landscape is")
    h.clear_display()
    assert h.reconstructions.plan(add(h, "changing.")) is None
    h = TranslationHistory()
    add(h, "Very long " * 300)
    assert h.reconstructions.plan(add(h, "continuation.")) is None


def test_extend_at_most_three_units_and_out_of_order_results():
    h = TranslationHistory()
    add(h, "The landscape is")
    a = h.reconstructions.plan(add(h, "changing and"))
    b = h.reconstructions.plan(add(h, "the reason is"))
    assert a.unit_id == 0 and b.unit_id == 1
    assert h.reconstructions.plan(add(h, "a new capability.")) is None
    h.reconstructions.update_translation(1, "completed", text="新しいまとまり。")
    h.reconstructions.update_translation(0, "completed", text="古い短いまとまり。")
    effective = h.reconstructions.effective_units()
    assert len(effective) == 2 and effective[0][1] == (0, 1, 2)
    assert effective[0][0].ja_text == "新しいまとまり。"


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
    assert [u.en_text for u, _ in h.reconstructions.effective_units()] == [
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


@pytest.mark.parametrize("invalid", [False, True])
def test_model_translates_full_passage_then_partitions_inside_original_unit(invalid):
    from types import SimpleNamespace

    h = TranslationHistory()
    add(h, "The landscape is")
    h.update_translation(0, "completed", text="状況は……")
    target = h.reconstructions.plan(add(h, "changing. Reliability matters."))
    requests = []

    async def run():
        translator = ReconstructionTranslator("offline", h.reconstructions)
        await translator.client.close()

        async def parse(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                status="completed",
                usage=None,
                output_parsed=ReconstructionDecision(
                    japanese_translation="状況は変化しています。信頼性が重要です。",
                    paragraphs=[
                        ParagraphDecision(
                            english_end_token=3, japanese_text="状況は変化しています。"
                        ),
                        ParagraphDecision(
                            english_end_token=4 if invalid else 5,
                            japanese_text="信頼性が重要です。",
                        ),
                    ],
                ),
            )

        translator.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        await TranslationWorker(h.reconstructions, "offline")._one(translator, target, [])

    asyncio.run(run())
    request = requests[0]
    assert request["text_format"] is ReconstructionDecision
    assert request["reasoning"] == {"effort": "none"} and request["model"] == "gpt-6-luna"
    assert "First" in request["instructions"] and "ENTIRE passage" in request["instructions"]
    data = json.loads(request["input"])
    assert data["FULL_ENGLISH"] == target.en_text
    assert "REVIEW_UNITS" not in data  # Arbitrary streaming cuts do not bias the model.
    assert data["ENGLISH_TOKENS"][3] == {"index": 3, "text": "changing."}
    revision = h.reconstructions.entries()[0]
    assert revision.translation.translation_status == ("failed" if invalid else "completed")
    assert revision.decisions[0]["decision"]["japanese_translation"].startswith("状況")
    assert h.segments()[0].ja_text == "状況は……"
    assert len(requests) == 1
    if not invalid:
        assert [target.en_text[p.en_start : p.en_end] for p in revision.paragraphs] == [
            "The landscape is changing.",
            "Reliability matters.",
        ]
        saved = h.saved_text()
        assert "EN: The landscape is changing.\nJA: 状況は変化しています。" in saved
        assert "EN: Reliability matters.\nJA: 信頼性が重要です。" in saved


@pytest.mark.parametrize(
    "ends,parts,full",
    [
        ([0], ["一。"], "一。"),  # Missing English tokens.
        ([0, 0, 2], ["一。", "二。", "三。"], "一。二。三。"),  # Duplicate English.
        ([3], ["一。"], "一。"),  # Out-of-range.
        ([2], [""], "一。"),
        ([2], ["一。"], "一。二。"),  # Missing Japanese.
        ([0, 2], ["一。", "一。"], "一。"),  # Duplicate Japanese.
        ([], [], ""),
    ],
)
def test_reject_invalid_partition_without_semantic_rules(ends, parts, full):
    result = ReconstructionDecision(
        japanese_translation=full,
        paragraphs=[
            ParagraphDecision(english_end_token=i, japanese_text=part)
            for i, part in zip(ends, parts, strict=True)
        ],
    )
    with pytest.raises(ValueError):
        align_paragraphs("Three original tokens.", result)


def test_unicode_whitespace_and_punctuation_preserved():
    en = "  OpenAI’s 🚀  reliability in production.\nReally?  "
    decision = ReconstructionDecision(
        japanese_translation="本番での信頼性。\n本当？",
        paragraphs=[
            ParagraphDecision(english_end_token=4, japanese_text="本番での信頼性。"),
            ParagraphDecision(english_end_token=5, japanese_text="本当？"),
        ],
    )
    parts = align_paragraphs(en, decision)
    assert en[parts[0].en_start : parts[0].en_end] == "OpenAI’s 🚀  reliability in production."
    assert en[parts[1].en_start : parts[1].en_end] == "Really?"


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
    decision = ReconstructionDecision(
        japanese_translation="状況は変わっています。信頼性が重要です。",
        paragraphs=[
            ParagraphDecision(english_end_token=3, japanese_text="状況は変わっています。"),
            ParagraphDecision(english_end_token=5, japanese_text="信頼性が重要です。"),
        ],
    )
    h.reconstructions.record_decision(0, decision.model_dump(), None)
    h.reconstructions.set_paragraphs(0, align_paragraphs(target.en_text, decision))
    h.reconstructions.update_translation(0, "completed", text=decision.japanese_translation)
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
    assert revisions[-1]["paragraphs"][1]["ja_text"] == "信頼性が重要です。"
    saved = (saver.directory / "subtitles.txt").read_text(encoding="utf-8")
    assert "EN: The landscape is changing.\nJA: 状況は変わっています。" in saved
