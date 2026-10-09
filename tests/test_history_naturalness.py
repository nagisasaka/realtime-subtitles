import asyncio
from types import SimpleNamespace

import pytest

from benchmarks.history_readability.naturalness import (
    IDEAS,
    checked_verdict,
    execute,
    judge_data,
    metrics,
    variant_config,
)
from benchmarks.history_readability.prepare import digest, read_json, write_json


def grade(**changes):
    return {
        "boundaries": [],
        "major_translation_errors": [],
        "alignment_errors": [],
        "overmerging": [],
        "naturalness": 3,
        "ease": 3,
        "style_findings": [],
        **changes,
    }


def verdict(**changes):
    return {
        "A": grade(),
        "B": grade(),
        "readability": "A",
        "fidelity": "tie",
        "alignment": "tie",
        "reason": "根拠",
        **changes,
    }


def test_five_distinct_ideas_change_one_stage_only():
    assert len(IDEAS) >= 5
    assert len({str(v) for v in IDEAS.values()}) == len(IDEAS)
    for idea in IDEAS.values():
        assert bool(idea["split"]) != bool(idea["translation"])


def test_followup_cannot_replace_original_trial_or_change_frozen_input(tmp_path):
    manifest = {"variants": {"baseline": {"split": "s", "translation": "t"}}}
    path = tmp_path / "followup.json"
    value = {
        "parent_manifest_hash": digest(manifest),
        "id": "new",
        "config": {"split": "new split", "translation": "t"},
    }
    write_json(path, value)
    assert variant_config(manifest, "new", path) == value["config"]
    with pytest.raises(ValueError, match="identity"):
        variant_config(manifest, "wrong", path)
    write_json(path, dict(value, id="baseline"))
    with pytest.raises(ValueError, match="replace"):
        variant_config(manifest, "baseline", path)
    write_json(path, dict(value, parent_manifest_hash="changed"))
    with pytest.raises(ValueError, match="identity"):
        variant_config(manifest, "new", path)


def test_judge_sees_newest_first_without_reversing_paragraph_text():
    p = [{"en": "Old.", "ja": "古い。"}, {"en": "New.", "ja": "新しい。"}]
    data = judge_data({"paragraphs": p})
    assert data["DISPLAY_TOP_TO_BOTTOM"] == [1, 0]
    assert data["paragraphs"][0]["en"] == "Old."
    assert data["INTERNAL_BOUNDARIES"][0]["right"]["ja"] == "新しい。"
    assert all("paragraph" not in item for item in p)


def test_invalid_judge_not_counted_as_success():
    with pytest.raises(ValueError):
        checked_verdict(verdict(A=grade(naturalness=6)), [{}], [{}])
    finding = {"paragraph": 2, "source_quote": "x", "output_quote": "y", "reason": "z"}
    with pytest.raises(ValueError, match="Style"):
        checked_verdict(verdict(A=grade(style_findings=[finding])), [{}], [{}])
    with pytest.raises(ValueError, match="boundary"):
        checked_verdict(verdict(), [{}, {}], [{}])


def test_equal_error_counts_still_require_candidate_review_and_labels_are_resolved():
    finding = {"paragraph": 0, "source_quote": "x", "output_quote": "y", "reason": "z"}
    parsed = verdict(
        A=grade(major_translation_errors=[finding]),
        B=grade(major_translation_errors=[finding], naturalness=4),
    )
    report = {
        "results": {},
        "judgments": {
            "w": {"status": "completed", "candidate_label": "B", "call": {"parsed": parsed}},
            "missing": {"status": "judge_invalid"},
        },
    }
    m = metrics(report)
    assert m["candidate_major_windows"] == ["w"]
    assert m["baseline_major_windows"] == ["w"]
    assert m["readability"] == {"loss": 1}
    assert m["naturalness_delta"] == 1
    assert (m["judge_complete"], m["judge_missing"]) == (1, 1)


def test_budget_failure_is_persisted_without_cancelling_other_cases(tmp_path):
    manifest = {
        "variants": {"baseline": {"split": "test", "translation": "translate"}},
        "source_hashes": {},
        "source": str(tmp_path),
        "request_limit": 0,
        "windows": [
            {"id": str(i), "split": "dev", "english": "Yes.", "context": []} for i in range(2)
        ],
    }
    args = SimpleNamespace(
        variant="baseline",
        split="dev",
        ids=None,
        nonce="",
        reverse=False,
        output=tmp_path,
        seconds=30,
    )
    path = asyncio.run(execute(args, manifest, None))
    report = read_json(path)
    assert report["status"] == "incomplete"
    assert len(report["results"]) == 2
    assert all(r["status"] == "error" for r in report["results"].values())
    asyncio.run(execute(args, manifest, None))
    assert len(read_json(path)["previous_invocations"]) == 1


def test_sequential_trial_uses_real_tail_planner_and_preserves_raw_and_prefix():
    from conftest import make_unit

    from benchmarks.history_readability.naturalness_tail import arm
    from realtime_subtitles.translation_history import TranslationHistory

    source = TranslationHistory()
    for i, text in enumerate(
        ("Keep this.", "The system", "is fast.", "And stable.", "Next topic.")
    ):
        make_unit(
            source,
            {
                "message": "AddSegment",
                "segment": {"transcript": text, "speaker": "S1"},
                "metadata": {"start_time": i, "end_time": i + 1},
            },
            "source",
        )
    units = source.segments()
    english = units[0].en_text + " " + units[1].en_text
    window = {"id": "test", "unit_ids": [0, 1], "english": english}
    initial = {
        "status": "valid",
        "chunks": [
            {"id": 0, "en_start": 0, "en_end": len(units[0].en_text)},
            {"id": 1, "en_start": len(units[0].en_text) + 1, "en_end": len(english)},
        ],
        "paragraphs": [
            {"en_start": 0, "en_end": len(units[0].en_text), "ja": "維持。"},
            {"en_start": len(units[0].en_text) + 1, "en_end": len(english), "ja": "システムは"},
        ],
    }
    jobs = []

    class FakeRunner:
        async def translate(self, job, prompt, **kwargs):
            jobs.append((job, kwargs))
            return {
                "status": "valid",
                "chunks": [{"id": 0, "en_start": 0, "en_end": len(job["english"])}],
                "paragraphs": [{"en_start": 0, "en_end": len(job["english"]), "ja": "訳。"}],
            }

    result, history = asyncio.run(
        arm(
            FakeRunner(),
            units,
            window,
            initial,
            units[2:],
            {"split": "split", "translation": "translate"},
        )
    )
    assert result["status"] == "valid"
    assert result["source_preserved"] and result["prefix_preserved"]
    assert jobs[0][0]["english"] == "The system is fast."
    assert jobs[0][1]["protected_prefix_chars"] == len("The system")
    assert len(jobs) == 3
    assert history.reconstructions.effective_blocks()[0].ja_text == "維持。"
