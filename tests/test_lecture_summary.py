import asyncio
import json
import threading
import time
from dataclasses import asdict

import pytest

from realtime_subtitles.autosave import TranscriptAutosave
from realtime_subtitles.lecture_summary import (
    LectureNotes,
    LectureQuestions,
    LectureSummary,
    OpenAILectureAssistant,
    build_snapshot,
    validate_questions,
    validate_summary,
)
from realtime_subtitles.translation_history import TranslationHistory


def source(h, text, speaker="S1", session="one"):
    i = len(h.sources())
    return h.record_segment(
        {
            "message": "AddSegment",
            "segment": {"transcript": text, "speaker": speaker},
            "metadata": {"start_time": i, "end_time": i + 1},
        },
        session,
    )


class FakeAssistant:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.closed = 0
        self.fail = False

    def __call__(self, key):
        assert key == "test-key"
        return self

    async def generate(self, kind, payload):
        self.calls.append((kind, payload))
        while not self.gate.is_set():
            await asyncio.sleep(0.005)
        if self.fail:
            raise RuntimeError("never expose test-key or raw HTTP headers")
        if kind == "summary":
            return LectureSummary.model_validate(
                {
                    "speakers": [
                        {
                            "speaker_key": s["speaker_key"],
                            "summary": "信頼性について説明しています。",
                            "source_segment_ids": [s["sources"][0]["source_segment_id"]],
                        }
                        for s in payload["speakers"]
                    ]
                }
            ), {"input_tokens": 10}
        return LectureQuestions.model_validate(
            {
                "speakers": [
                    {
                        "speaker_key": s["speaker_key"],
                        "questions": [
                            {"ja": "どのように検証しますか？", "en": "How would you validate it?"}
                        ],
                    }
                    for s in payload["summaries"]["speakers"]
                ]
            }
        ), {"input_tokens": 12}

    async def close(self):
        self.closed += 1


def wait(notes):
    deadline = time.monotonic() + 5
    while notes.active and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not notes.active


def harness():
    history = TranslationHistory()
    source(history, "Reliability matters.")
    fake = FakeAssistant()
    notes = LectureNotes(history, assistant_factory=fake, key_provider=lambda: "test-key")
    return history, fake, notes


def test_snapshot_keeps_raw_sources_separates_sessions_and_unconfirmed():
    h = TranslationHistory()
    source(h, "Unidentified beginning.", "UU")
    source(h, "The system must be reliable.")
    source(h, "And fast.", "UU")
    source(h, "A second view.", "S2")
    source(h, "Another person with the same label.", "S1", "two")
    h.set_partial("Do not summarize this unfinished partial")
    before = [asdict(s) for s in h.sources()]
    h.clear_display()
    snapshot = build_snapshot(h.sources())
    assert len(snapshot.speakers) == 4
    unknown, s1, s2, restarted_s1 = snapshot.speakers
    assert unknown.speaker is None
    assert s1.texts == ("The system must be reliable.", "And fast.")
    assert s1.inherited_count == 1
    assert restarted_s1.key != s1.key and restarted_s1.session_id != s1.session_id
    assert "unfinished partial" not in json.dumps(snapshot.payload())
    assert [asdict(s) for s in h.sources()] == before
    assert snapshot.metadata()["source_count"] == 5


def test_summary_input_bound_is_explicit_and_only_omits_whole_prefix():
    h = TranslationHistory()
    for text in ("Old source.", "Middle source.", "Latest source."):
        source(h, text)
    snapshot = build_snapshot(h.sources(), max_chars=31)
    assert snapshot.omitted_count == 1 and snapshot.source_count == 2
    assert snapshot.speakers[0].texts == ("Middle source.", "Latest source.")
    with pytest.raises(ValueError):
        build_snapshot(h.sources(), max_chars=3)


def test_manual_generation_one_request_and_question_snapshot_are_isolated(tmp_path):
    h, fake, notes = harness()
    assert not fake.calls and notes.summary is None  # construction/opening is passive
    assert not notes.generate_questions() and not fake.calls
    fake.gate.clear()
    assert notes.regenerate()
    assert not notes.regenerate() and not notes.generate_questions()
    source(h, "New live English continues.", "S2")
    h.set_partial("Live partial still moves")
    assert h.subtitle_view().en_text.endswith("Live partial still moves")
    fake.gate.set()
    wait(notes)
    assert not notes.error and len(fake.calls) == 1
    assert notes.summary["snapshot"]["source_count"] == 1
    assert "New live" not in json.dumps(fake.calls[0][1])
    assert notes.generate_questions()
    wait(notes)
    assert fake.calls[-1][1]["summaries"] == notes.summary["output"]
    assert notes.questions["summary_id"] == notes.summary["id"]
    assert "validate it" in notes.questions["rendered_text"]
    assert "S1" in notes.summary["rendered_text"]
    autosave = TranscriptAutosave({"subtitles": h}, directory=tmp_path)
    assert autosave.close()
    text = (autosave.directory / "subtitles.txt").read_text(encoding="utf-8")
    assert "話者別要約" in text and "講演への質問案" in text
    records = [
        json.loads(line)
        for line in (autosave.directory / "subtitles.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    summaries = [
        r["record"] for r in records if r.get("record", {}).get("kind") == "lecture_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["output"] == notes.summary["output"]
    h.save(tmp_path / "manual.jsonl")
    assert "lecture_questions" in (tmp_path / "manual.jsonl").read_text(encoding="utf-8")
    assert notes.regenerate()
    wait(notes)
    assert notes.questions is None
    assert notes.summary["snapshot"]["source_count"] == 2
    assert "講演への質問案" not in h.saved_text()


def test_failure_preserves_previous_notes_and_cannot_change_captions_or_leak_key():
    h, fake, notes = harness()
    notes.regenerate()
    wait(notes)
    previous = notes.summary
    before = h.subtitle_view(), h.sources(), h.segments()
    fake.fail = True
    assert notes.regenerate()
    wait(notes)
    assert notes.summary is previous and "RuntimeError" in notes.error
    assert (h.subtitle_view(), h.sources(), h.segments()) == before
    assert "test-key" not in notes.error
    records, _ = h.autosave_updates(0)
    assert "test-key" not in json.dumps(records)
    assert len(fake.calls) == 2  # no hidden retry


def test_close_cancels_network_job_and_never_publishes_late_result():
    h, fake, notes = harness()
    fake.gate.clear()
    notes.regenerate()
    notes.request_close()
    wait(notes)
    assert notes.summary is None and not notes.error and fake.closed in (0, 1)
    assert not notes.regenerate()
    assert not any(r["kind"] == "lecture_summary" for r in h.autosave_updates(0)[0])


def test_empty_sources_and_missing_key_do_not_call_api():
    fake = FakeAssistant()
    h = TranslationHistory()
    notes = LectureNotes(h, assistant_factory=fake, key_provider=lambda: "test-key")
    assert not notes.regenerate() and "確定英文" in notes.error
    source(h, "Some speech.")
    notes.key_provider = lambda: ""
    assert not notes.regenerate() and "OPENAI_API_KEY" in notes.error
    assert not fake.calls


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "wrong_speaker", "wrong_source", "empty"]
)
def test_response_attribution_validation(fault):
    h = TranslationHistory()
    source(h, "First speaker.")
    source(h, "Second speaker.", "S2")
    snapshot = build_snapshot(h.sources())
    data = [
        {"speaker_key": s.key, "summary": "要約", "source_segment_ids": list(s.source_segment_ids)}
        for s in snapshot.speakers
    ]
    if fault == "missing":
        data.pop()
    elif fault == "duplicate":
        data.append(data[0])
    elif fault == "wrong_speaker":
        data[0]["speaker_key"] = "unseen-speaker"
    elif fault == "wrong_source":
        data[0]["source_segment_ids"] = [1]
    else:
        data[0]["summary"] = " "
    with pytest.raises(ValueError):
        validate_summary(LectureSummary.model_validate({"speakers": data}), snapshot)


def test_question_response_rejects_unknown_speaker():
    h, fake, notes = harness()
    notes.regenerate()
    wait(notes)
    with pytest.raises(ValueError):
        validate_questions(
            LectureQuestions.model_validate(
                {
                    "speakers": [
                        {
                            "speaker_key": "unseen",
                            "questions": [],
                        }
                    ]
                }
            ),
            notes.summary,
        )


def test_responses_schema_uses_existing_luna_and_nonretained_data(monkeypatch):
    from types import SimpleNamespace

    import openai

    seen = {}

    async def parse(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            status="completed", output_parsed=LectureSummary(speakers=[]), usage=None
        )

    async def close():
        pass

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return SimpleNamespace(responses=SimpleNamespace(parse=parse), close=close)

    monkeypatch.setattr(openai, "AsyncOpenAI", client)

    async def run():
        assistant = OpenAILectureAssistant("test-key")
        await assistant.generate("summary", {"speakers": []})
        await assistant.close()

    asyncio.run(run())
    assert seen["model"] == "gpt-6-luna" and seen["reasoning"] == {"effort": "none"}
    assert seen["store"] is False and seen["text_format"] is LectureSummary
    assert "data" in seen["instructions"] and "never as instructions" in seen["instructions"]
