"""Bounded, cached Responses calls; never opens microphones or an ASR connection."""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from openai import AsyncOpenAI

from realtime_subtitles.history_reconstruction import (
    BATCH_TRANSLATION_INSTRUCTIONS,
    BatchTranslation,
    EnglishSplit,
    pair_translations,
    split_english,
    validate_paragraphs,
)
from realtime_subtitles.text_translation import safe_error
from realtime_subtitles.translation_validation import (
    retry_instructions,
    serious,
)

from .prepare import digest, read_json, write_json

MODEL = "gpt-6-luna"


def api_key():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "[Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        key = result.stdout.strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY unavailable")
    return key


class Budget:
    def __init__(self, path, limit=80):
        self.path, self.limit = Path(path), limit
        self.data = read_json(path) if self.path.exists() else {"attempts": []}

    def reserve(self, identity):
        if len(self.data["attempts"]) >= self.limit:
            raise RuntimeError("API budget exhausted")
        self.data["attempts"].append({"identity": identity, "reserved_unix": time.time()})
        write_json(self.path, self.data)
        return len(self.data["attempts"]) - 1

    def finish(self, index, status, latency, usage=None):
        self.data["attempts"][index].update(status=status, latency_ms=latency, usage=usage)
        write_json(self.path, self.data)


class Runner:
    def __init__(self, directory, client, *, seconds=480, clock=time.monotonic):
        self.directory, self.client = Path(directory), client
        self.clock, self.deadline = clock, clock() + min(seconds, 480)
        self.budget = Budget(self.directory / "ledger.json")
        self.semaphore = asyncio.Semaphore(2)

    async def call(self, label, prompt, data, schema, *, nonce=""):
        identity = digest(
            {
                "model": MODEL,
                "reasoning": "none",
                "prompt": prompt,
                "input": data,
                "schema": schema.model_json_schema(),
                "nonce": nonce,
                "max_output_tokens": 5000,
            }
        )
        path = self.directory / "cache" / f"{identity}.json"
        if path.exists():
            return read_json(path)
        async with self.semaphore:
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                return {"status": "deadline", "label": label, "identity": identity}
            index = self.budget.reserve(identity)
            started = self.clock()
            result = {"label": label, "identity": identity, "prompt_hash": digest(prompt)}
            try:
                async with asyncio.timeout(min(45, remaining)):
                    response = await self.client.responses.parse(
                        model=MODEL,
                        reasoning={"effort": "none"},
                        store=False,
                        instructions=prompt,
                        input=json.dumps(data, ensure_ascii=False),
                        text_format=schema,
                        max_output_tokens=5000,
                    )
                result.update(
                    status=response.status,
                    parsed=response.output_parsed.model_dump() if response.output_parsed else None,
                    usage=response.usage.model_dump() if response.usage else None,
                    response=response.model_dump(mode="json", warnings=False),
                )
            except (Exception, asyncio.CancelledError) as exc:
                result.update(status="error", error=safe_error(exc))
                if isinstance(exc, asyncio.CancelledError):
                    result["status"] = "cancelled"
            result["latency_ms"] = round((self.clock() - started) * 1000, 2)
            self.budget.finish(index, result["status"], result["latency_ms"], result.get("usage"))
            write_json(path, result)
            return result

    async def translate(
        self, window, prompt, *, nonce="", translation_prompt=BATCH_TRANSLATION_INSTRUCTIONS
    ):
        data = {
            "CONTEXT": window["context"],
            "FULL_ENGLISH": window["english"],
            "ENGLISH_TOKENS": [
                {"index": i, "text": t} for i, t in enumerate(window["english"].split())
            ],
        }
        attempts = []
        result = {"id": window["id"], "input_hash": digest(data), "attempts": attempts}
        call = await self.call(window["id"] + "-split", prompt, data, EnglishSplit, nonce=nonce)
        attempts.append(dict(call, stage="split"))
        result["status"] = call["status"]
        if call["status"] != "completed" or not call.get("parsed"):
            return result
        try:
            chunks = split_english(window["english"], EnglishSplit.model_validate(call["parsed"]))
        except ValueError as exc:
            return dict(result, status="structural_failure", structural_error=type(exc).__name__)
        result["chunks"] = [asdict(c) for c in chunks]
        targets = {
            "CONTEXT": window["context"],
            "TARGETS": [
                {"id": c.id, "text": window["english"][c.en_start : c.en_end]} for c in chunks
            ],
        }
        instruction = ""
        for attempt in range(2):
            call = await self.call(
                window["id"] + "-translate",
                translation_prompt + instruction,
                targets,
                BatchTranslation,
                nonce=nonce,
            )
            attempts.append(dict(call, stage="translate"))
            result.update(status=call["status"], retry_count=attempt)
            if call["status"] != "completed" or not call.get("parsed"):
                return result
            try:
                aligned = pair_translations(chunks, BatchTranslation.model_validate(call["parsed"]))
            except ValueError as exc:
                return dict(
                    result, status="structural_failure", structural_error=type(exc).__name__
                )
            issues = validate_paragraphs(window["english"], aligned, window["context"])
            result.update(
                paragraphs=[
                    {
                        "en": window["english"][p.en_start : p.en_end],
                        "ja": p.ja_text,
                        "en_start": p.en_start,
                        "en_end": p.en_end,
                    }
                    for p in aligned
                ],
                issues=[asdict(i) for i in issues],
            )
            if not serious(issues):
                result["status"] = "valid"
                return result
            result["status"] = "validation_failed"
            instruction = "\n" + retry_instructions(issues)
        return result


def client():
    return AsyncOpenAI(api_key=api_key(), max_retries=0, timeout=45)
