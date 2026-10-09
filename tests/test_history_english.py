import asyncio
import json
from dataclasses import asdict

import pytest
from test_history_reconstruction import add, batch, reconstruct

from realtime_subtitles.history_english import (
    JoinCleanup,
    apply_join_cleanup,
    candidate_payload,
    fragment_start_tokens,
    render_english,
)
from realtime_subtitles.history_reconstruction import EnglishSplit, split_english
from realtime_subtitles.translation_assembler import TranslationUnitAssembler
from realtime_subtitles.translation_history import TranslationHistory


def edit(index, punctuation=False, lowercase=False):
    return {
        "start_token": index,
        "remove_previous_punctuation": punctuation,
        "lowercase_initial": lowercase,
    }


def cleanup(english, starts, edits, ends=None):
    chunks = split_english(english, EnglishSplit(end_tokens=ends or [len(english.split()) - 1]))
    return apply_join_cleanup(english, chunks, [JoinCleanup(**e) for e in edits], starts)


def test_cleanup_only_when_requested_and_keeps_real_commas_colons():
    english = "After lunch, We offer two plans: Basic and Pro."
    chunks, rejected = cleanup(english, (2, 7), [edit(2, lowercase=True)])
    assert render_english(english, chunks[0].edits) == (
        "After lunch, we offer two plans: Basic and Pro."
    )
    assert not rejected
    unchanged, _ = cleanup(english, (2, 7), [])
    assert render_english(english, unchanged[0].edits) == english


@pytest.mark.parametrize("word", ["OpenAI", "MCP", "JWT", "mTLS", "I", "I'm", "I’m", "3.14", "$2"])
def test_acronyms_mixed_case_i_and_numbers_cannot_be_lowercased(word):
    english = f"We use {word} today."
    chunks, rejected = cleanup(english, (2,), [edit(2, lowercase=True)])
    assert render_english(english, chunks[0].edits) == english
    assert rejected


@pytest.mark.parametrize("token", ["2,", "12:", "12:30,", "3.14,", "https:", "http:"])
def test_numbers_and_urls_not_changed(token):
    english = f"See {token} Details."
    chunks, rejected = cleanup(english, (2,), [edit(2, punctuation=True)])
    assert render_english(english, chunks[0].edits) == english
    assert rejected


def test_cleanup_cannot_cross_new_paragraph_or_non_fragment_boundary():
    english = "We need, More capacity."
    for starts, ends in [((2,), [1, 3]), ((), [3]), ((3,), [3])]:
        chunks, rejected = cleanup(english, starts, [edit(2, True, True)], ends)
        assert all(not c.edits for c in chunks)
        assert rejected


@pytest.mark.parametrize("punctuation", [".", "?", "!", '."'])
def test_keep_sentence_start_case_when_terminal_punctuation_is_retained(punctuation):
    english = f"Done{punctuation} New sentence."
    chunks, rejected = cleanup(english, (1,), [edit(1, lowercase=True)])
    assert render_english(english, chunks[0].edits) == english
    assert rejected


def test_invalid_cleanup_is_ignored_without_losing_other_paragraphs():
    english = "We need, More capacity."
    chunks, rejected = cleanup(english, (2,), [edit(999, True, True), edit(2, True, True)])
    assert render_english(english, chunks[0].edits) == "We need more capacity."
    assert rejected == ({"start_token": 999, "reason": "not_an_editable_fragment_join"},)


def paired_target():
    h = TranslationHistory()
    assembler = TranslationUnitAssembler(h.emit_unit)
    for i, text in enumerate(("We need,", "More capacity", "To serve:", "Our users.")):
        source = h.record_segment(
            {
                "message": "AddSegment",
                "segment": {"transcript": text, "speaker": "UU"},
                "metadata": {"start_time": i, "end_time": i + 1},
            },
            "s",
        )
        assembler.accept(source)
    return h, h.reconstructions.plan(h.segments()[-1])


def test_production_translation_display_mapping_save_and_tail_preserve_raw(tmp_path):
    h, target = paired_target()
    raw = [asdict(s) for s in h.sources()]
    units = [asdict(s) for s in h.segments()]
    assert fragment_start_tokens(target) == (2, 4, 6)
    assert [c["start_token"] for c in candidate_payload(target.en_text, (2, 4, 6))] == [2, 4, 6]
    calls = asyncio.run(
        reconstruct(
            h,
            target,
            [
                {
                    "end_tokens": [7],
                    "join_cleanup": [
                        edit(2, True, True),
                        edit(4, lowercase=True),
                        edit(6, True, True),
                    ],
                },
                batch("利用者にサービスを提供するには、さらに容量が必要です。"),
            ],
        )
    )
    assert len(calls) == 2
    corrected = "We need more capacity to serve our users."
    assert json.loads(calls[1]["input"])["TARGETS"] == [{"id": 0, "text": corrected}]
    assert [asdict(s) for s in h.sources()] == raw
    assert [asdict(s) for s in h.segments()] == units
    (block,) = h.reconstructions.effective_blocks()
    assert block.en_text == "We need, More capacity To serve: Our users."
    assert block.display_en_text == corrected
    for identity, lo, hi, start, end in block.source_runs:
        original = h.segments()[identity].en_text.strip()[lo:hi]
        assert original.lower() == corrected[start:end].lower()
        assert hi - lo == end - start
    assert block.start == (0, 0) and block.end == (1, len(h.segments()[1].en_text))
    assert f"EN: {corrected}\n" in h.saved_text()
    h.save(tmp_path / "result.jsonl")
    rows = [
        json.loads(line)
        for line in (tmp_path / "result.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    revision = next(r for r in rows if r["kind"] == "history_revision")
    assert revision["translation"]["en_text"] == target.en_text
    assert len(revision["paragraphs"][0]["edits"]) == 5
    assert h.reconstructions.entries()[0].applied
    # Carry accepted surface edits across later windows; never restore raw artifacts.
    next_target = h.reconstructions.plan(add(h, "Another thought.", speaker="UU"))
    assert next_target.en_text == target.en_text + " Another thought."
    calls = asyncio.run(
        reconstruct(
            h,
            next_target,
            [
                {"end_tokens": [7, 9], "join_cleanup": []},
                batch("容量が必要です。", "別の考えです。"),
            ],
        )
    )
    assert json.loads(calls[0]["input"])["FULL_ENGLISH"] == corrected + " Another thought."
    assert json.loads(calls[1]["input"])["TARGETS"][0]["text"] == corrected
    assert h.reconstructions.effective_blocks()[0].display_en_text == corrected


def test_partial_tail_source_offsets_and_bad_model_edit_is_only_a_warning():
    h = TranslationHistory()
    add(h, "Keep this. We need,")
    target = h.reconstructions.plan(add(h, "More capacity."))
    calls = asyncio.run(
        reconstruct(
            h,
            target,
            [
                {
                    "end_tokens": [1, 5],
                    "join_cleanup": [edit(4, True, True), edit(100, True, True)],
                },
                batch("これを残してください。", "もっと容量が必要です。"),
            ],
        )
    )
    assert len(calls) == 2
    assert h.reconstructions.entries()[0].cleanup_rejections
    assert h.reconstructions.entries()[0].applied
    target = h.reconstructions.plan(add(h, "For our users."))
    assert target.en_text == "We need, More capacity. For our users."
    assert fragment_start_tokens(target) == (2, 4)
    assert render_english(target.en_text, h.reconstructions.base_edits_for(target.unit_id)) == (
        "We need more capacity. For our users."
    )
