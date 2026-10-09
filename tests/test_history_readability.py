import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from benchmarks.history_readability.judge import Verdict, check_verdict
from benchmarks.history_readability.prepare import digest, replay, select
from benchmarks.history_readability.runner import Budget, Runner
from realtime_subtitles.history_reconstruction import BatchTranslation, EnglishSplit


def event(text, i, *, kind="AddSegment", speaker="S1"):
    return {
        "meeting_id": "m",
        "session_id": "s",
        "event_type": kind,
        "received_monotonic_ms": 1000 + i * 1000,
        "raw_event": {
            "message": kind,
            "segment": {"transcript": text, "speaker": speaker},
            "metadata": {"start_time": i, "end_time": i + 1},
        },
    }


def test_replay_uses_production_pairs_context_and_immutable_raw():
    rows = [event(f"Source {i}.", i) for i in range(12)] + [event("", 12, kind="EndOfTranscript")]
    original = deepcopy(rows)
    a = replay(rows)
    assert rows == original
    assert len(a) == 5
    assert a[0]["english"] == " ".join(f"Source {i}." for i in range(4))
    assert a[0]["source_ids"] == tuple(range(4))
    assert a[1]["context"] == [
        {"speaker": "S1", "text": f"Source {i}. Source {i + 1}."} for i in range(0, 2, 2)
    ]
    assert len(a[0]["raw_sources"]) == 4


def test_partial_speaker_change_flushes_and_unknown_not_joined():
    rows = [
        event("a", 0),
        event("b", 1, kind="AddPartialSegment", speaker="S2"),
        event("b", 2, speaker="S2"),
        event("c", 3, speaker="S2"),
        event("d", 4, speaker="S2"),
        event("e", 5, speaker="S2"),
        event("x", 6, speaker="UU"),
        event("x", 7, speaker="UU"),
        event("", 8, kind="EndOfTranscript"),
    ]
    windows = replay(rows)
    assert [w["english"] for w in windows] == ["b c d e"]
    assert windows[0]["context"][0]["text"] == "a"


def test_missing_eos_rejected():
    with pytest.raises(ValueError, match="EOS"):
        replay([event("a", 0)])


def test_holdout_context_leak_rejected():
    rows = [event(str(i), i) for i in range(12)] + [event("", 12, kind="EndOfTranscript")]
    with pytest.raises(ValueError, match="leakage"):
        select(replay(rows), [("m", 0, "dev", "x"), ("m", 6, "holdout", "x")])


def test_budget_persistent_and_reserved_before_failure(tmp_path):
    path = tmp_path / "ledger.json"
    Budget(path, 1).reserve("call")
    with pytest.raises(RuntimeError, match="budget"):
        Budget(path, 1).reserve("another")


def fake_client(calls, error=False):
    async def parse(**kwargs):
        calls.append(kwargs)
        if error:
            raise ConnectionError("sensitive detail must not be logged")
        d = (
            BatchTranslation(translations=[{"id": 0, "ja": "はい。"}])
            if kwargs["text_format"] is BatchTranslation
            else EnglishSplit(end_tokens=[0])
        )
        return SimpleNamespace(
            status="completed",
            output_parsed=d,
            usage=None,
            model_dump=lambda **kw: {"status": "completed"},
        )

    return SimpleNamespace(responses=SimpleNamespace(parse=parse))


def test_cache_keys_cover_prompt_context_schema_and_regeneration(tmp_path):
    async def run():
        calls = []
        r = Runner(tmp_path, fake_client(calls))
        await r.call("w", "p", {"c": "a"}, EnglishSplit)
        await r.call("w", "p", {"c": "a"}, EnglishSplit)
        assert len(calls) == 1
        await r.call("w", "p2", {"c": "a"}, EnglishSplit)
        await r.call("w", "p", {"c": "b"}, EnglishSplit)
        await r.call("w", "p", {"c": "a"}, Verdict)
        await r.call("w", "p", {"c": "a"}, EnglishSplit, nonce="repeat")
        assert len(calls) == 5
        await r.call("w", "p", {"c": "a"}, EnglishSplit, model="gpt-6-astra", effort="low")
        await r.call("w", "p", {"c": "a"}, EnglishSplit, model="gpt-6-astra", effort="medium")
        assert len(calls) == 7
        assert calls[-2]["model"] == "gpt-6-astra"
        assert calls[-2]["reasoning"] == {"effort": "low"}
        assert calls[0]["max_output_tokens"] == 5000

    asyncio.run(run())


def test_deadline_no_new_api_and_failure_saved(tmp_path):
    async def run():
        now, calls = [10], []
        r = Runner(tmp_path, fake_client(calls, error=True), seconds=1, clock=lambda: now[0])
        failed = await r.call("w", "p", {}, EnglishSplit)
        assert failed["error"] == "ConnectionError"
        assert "sensitive" not in str(failed)
        assert len(list((tmp_path / "cache").glob("*.json"))) == 1
        now[0] = 12
        timed = await r.call("w", "different", {}, EnglishSplit)
        assert timed["status"] == "deadline" and len(calls) == 1

    asyncio.run(run())


def test_judge_coverage_is_not_silently_counted_as_zero():
    empty = {
        "boundaries": [],
        "major_translation_errors": [],
        "alignment_errors": [],
        "overmerging": [],
    }
    v = Verdict(A=empty, B=empty, readability="tie", fidelity="tie", alignment="tie", reason="x")
    with pytest.raises(ValueError, match="boundary"):
        check_verdict(v, [{}, {}], [{}])
    check_verdict(v, [{}], [{}])
    assert digest({"context": ["a"]}) != digest({"context": ["b"]})


def test_wall_timeout_and_concurrency_cap(tmp_path):
    async def run():
        active, maximum = 0, 0

        async def parse(**kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(10)
            finally:
                active -= 1

        r = Runner(tmp_path, SimpleNamespace(responses=SimpleNamespace(parse=parse)), seconds=0.03)
        outputs = await asyncio.gather(
            *(r.call(str(i), str(i), {}, EnglishSplit) for i in range(4))
        )
        assert maximum == 2 and active == 0
        assert len(r.budget.data["attempts"]) == 2
        assert sum(x["status"] == "deadline" for x in outputs) == 2
        assert sum(x["status"] == "error" for x in outputs) == 2

    asyncio.run(run())


def test_validator_retries_once_without_changing_source(tmp_path):
    async def run():
        requests = []

        async def parse(**kwargs):
            requests.append(kwargs)
            d = (
                EnglishSplit(end_tokens=[1])
                if kwargs["text_format"] is EnglishSplit
                else BatchTranslation(translations=[{"id": 0, "ja": "200万トークンです。"}])
            )
            return SimpleNamespace(
                status="completed",
                output_parsed=d,
                usage=None,
                model_dump=lambda **kw: {"status": "completed"},
            )

        r = Runner(tmp_path, SimpleNamespace(responses=SimpleNamespace(parse=parse)))
        w = {"id": "currency", "english": "$2 million", "context": []}
        original = deepcopy(w)
        result = await r.translate(w, "Translate only source")
        assert result["status"] == "validation_failed" and result["retry_count"] == 1
        assert len(requests) == 3
        assert requests[1]["input"] == requests[2]["input"] and w == original
        assert requests[1]["instructions"] != requests[2]["instructions"]

    asyncio.run(run())


def test_aggregation_accounts_for_missing_and_label_order():
    from benchmarks.history_readability.report import metrics

    empty = {
        "boundaries": [],
        "major_translation_errors": [],
        "alignment_errors": [],
        "overmerging": [],
    }
    parsed = {
        "A": empty,
        "B": dict(
            empty, boundaries=[{"after_paragraph": 0, "grade": "unnecessary", "reason": "x"}]
        ),
        "readability": "A",
        "fidelity": "tie",
        "alignment": "tie",
        "reason": "x",
    }
    result = metrics(
        {
            "results": {},
            "judgments": {
                "w": {"status": "completed", "candidate_label": "A", "call": {"parsed": parsed}},
                "missing": {"status": "judge_invalid"},
            },
        }
    )
    assert result["judged"] == 1 and result["missing_judgments"] == 1
    assert result["readability"]["win"] == 1
    assert result["baseline"]["unnecessary_boundaries"] == 1
    assert result["candidate"]["unnecessary_boundaries"] == 0


def test_resume_retains_invocation_and_cached_calls(tmp_path):
    from benchmarks.history_readability.prepare import read_json, write_json
    from benchmarks.history_readability.run import execute

    write_json(
        tmp_path / "manifest.json",
        {
            "pipeline": "split_then_batch_translate_v1",
            "baseline_prompt": "test",
            "translation_prompt": "translate",
            "windows": [{"id": "w", "split": "dev", "english": "Yes.", "context": []}],
        },
    )
    args = SimpleNamespace(
        output=tmp_path,
        split="dev",
        ids=None,
        prompt=None,
        name="baseline",
        nonce="",
        compare=None,
        reverse=False,
        seconds=30,
    )
    calls = []

    async def run():
        first = await execute(args, fake_client(calls))
        assert read_json(first)["status"] == "finished"
        second = await execute(args, fake_client(calls))
        assert len(read_json(second)["previous_invocations"]) == 1
        assert len(calls) == 2
        args.seconds = -1
        args.name = "deadline"
        args.nonce = "new"
        result = read_json(await execute(args, fake_client(calls)))
        assert result["status"] == "incomplete" and len(calls) == 2

    asyncio.run(run())


def test_cross_version_comparison_verifies_actual_inputs_and_provenance():
    from benchmarks.history_readability.compare_saved import checked_windows

    old = {
        "source_hashes": {"events.jsonl": "fixed"},
        "windows": [
            {
                "id": "w",
                "english": "Original English.",
                "context": [],
                "raw_sources": [
                    {
                        "en_text": "Original English.",
                        "received_at": "old replay",
                        "received_monotonic_ms": 42,
                    }
                ],
            }
        ],
    }
    new = deepcopy(old)
    new["windows"][0]["raw_sources"][0]["received_at"] = "new replay"
    new["pipeline"] = "split_then_batch_translate_v1"

    def output(manifest):
        return {"inputs": {"manifest_hash": digest(manifest)}, "results": {"w": {}}}

    assert len(checked_windows(old, new, output(old), output(new))) == 1
    for key, value in [("english", "Changed English"), ("context", [{"text": "different"}])]:
        changed = deepcopy(new)
        changed["windows"][0][key] = value
        with pytest.raises(ValueError, match="inputs"):
            checked_windows(old, changed, output(old), output(changed))
    changed = deepcopy(new)
    changed["windows"][0]["raw_sources"][0]["received_monotonic_ms"] = 43
    with pytest.raises(ValueError, match="inputs"):
        checked_windows(old, changed, output(old), output(changed))
    with pytest.raises(ValueError, match="manifest"):
        checked_windows(old, new, output(new), output(new))
    changed = deepcopy(new)
    changed["source_hashes"]["events.jsonl"] = "different"
    with pytest.raises(ValueError, match="hashes"):
        checked_windows(old, changed, output(old), output(changed))
    with pytest.raises(ValueError, match="missing"):
        checked_windows(old, new, {"inputs": output(old)["inputs"], "results": {}}, output(new))


def test_saved_comparison_retains_manifests_and_marks_rejected_drafts(tmp_path):
    from benchmarks.history_readability.compare_saved import compare
    from benchmarks.history_readability.prepare import read_json, write_json

    old_manifest = {"source_hashes": {}, "windows": [{"id": "w", "english": "Yes.", "context": []}]}
    new_manifest = dict(old_manifest, pipeline="new")
    paragraph = {"en": "Yes.", "ja": "はい。", "en_start": 0, "en_end": 4}
    old = {
        "inputs": {"manifest_hash": digest(old_manifest)},
        "results": {"w": {"status": "structural_failure", "paragraphs": [paragraph]}},
    }
    new = {
        "inputs": {"manifest_hash": digest(new_manifest)},
        "results": {"w": {"status": "valid", "paragraphs": [paragraph]}},
    }
    for name, value in [
        ("old_manifest", old_manifest),
        ("manifest", new_manifest),
        ("old", old),
        ("current", new),
    ]:
        write_json(tmp_path / (name + ".json"), value)
    args = SimpleNamespace(
        **{k: tmp_path / (k + ".json") for k in ["old_manifest", "manifest", "old", "current"]},
        directory=tmp_path,
        output=tmp_path / "comparison.json",
        ids=None,
        reverse=False,
        seconds=30,
    )
    requests = []

    async def parse(**kwargs):
        requests.append(kwargs)
        empty = {
            "boundaries": [],
            "major_translation_errors": [],
            "alignment_errors": [],
            "overmerging": [],
        }
        verdict = Verdict(
            A=empty, B=empty, readability="tie", fidelity="tie", alignment="tie", reason="x"
        )
        return SimpleNamespace(
            status="completed", output_parsed=verdict, usage=None, model_dump=lambda **kw: {}
        )

    result = asyncio.run(compare(args, SimpleNamespace(responses=SimpleNamespace(parse=parse))))
    assert len(requests) == 1
    assert result["comparison_status"] == "finished"
    assert result["judgments"]["w"]["draft_only"] is True
    assert read_json(args.old) == old and read_json(args.current) == new
    assert result["inputs"]["manifest_hash"] == digest(new_manifest)
    assert result["comparison"]["historical_manifest_hash"] == digest(old_manifest)


def test_tail_eval_replay_and_seed_keep_original_raw_sources():
    from benchmarks.history_readability.tail_review import add_saved_unit, seed, source_histories
    from realtime_subtitles.translation_history import TranslationHistory

    rows = [event(f"Source {i}.", i) for i in range(8)] + [event("", 8, kind="EndOfTranscript")]
    originals = deepcopy(rows)
    source = source_histories(rows)["s"]
    assert rows == originals and len(source.segments()) == 4
    h = TranslationHistory()
    for unit in source.segments()[:3]:
        add_saved_unit(h, unit)
    english = " ".join(u.en_text for u in h.segments())
    cut = english.index("Source 5.")
    w = {"unit_ids": [0, 1, 2], "english": english}
    result = {
        "status": "valid",
        "chunks": [
            {"id": 0, "en_start": 0, "en_end": cut - 1},
            {"id": 1, "en_start": cut, "en_end": len(english)},
        ],
        "paragraphs": [
            {"en_start": 0, "en_end": cut - 1, "ja": "前半"},
            {"en_start": cut, "en_end": len(english), "ja": "最後"},
        ],
    }
    seed(h, w, result)
    assert len(h.sources()) == 6
    target = h.reconstructions.plan(add_saved_unit(h, source.segments()[3]))
    assert target.en_text == "Source 5. Source 6. Source 7."
    assert h.reconstructions.effective_blocks()[0].ja_text == "前半"
    with pytest.raises(ValueError, match="EOS"):
        source_histories(rows[:-1])
