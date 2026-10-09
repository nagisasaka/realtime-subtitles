"""Non-destructive, source-indexed surface edits at ASR fragment joins."""

import re
from dataclasses import dataclass

from pydantic import BaseModel, StrictBool, StrictInt


class JoinCleanup(BaseModel):
    start_token: StrictInt
    remove_previous_punctuation: StrictBool
    lowercase_initial: StrictBool


@dataclass(frozen=True)
class EnglishEdit:
    start: int
    end: int
    replacement: str


def render_english(english, edits=(), start=0, end=None):
    """Apply validated edits to a projection, never mutate source text/offsets."""
    end = len(english) if end is None else end
    cursor, pieces = start, []
    for edit in sorted(edits, key=lambda e: e.start):
        if not cursor <= edit.start < edit.end <= end:
            raise ValueError("Invalid English surface edit range")
        pieces.extend((english[cursor : edit.start], edit.replacement))
        cursor = edit.end
    pieces.append(english[cursor:end])
    return "".join(pieces)


def retained_ranges(start, end, edits):
    """Raw ranges and display offsets, split at deletions to preserve reading marks.

    Capitalization is length-preserving. Only validated one-character deletions
    shorten a projection; source/timestamp coordinates are never shifted.
    """
    cursor, display = start, 0
    for edit in sorted(edits, key=lambda e: e.start):
        if edit.replacement:
            continue
        if cursor < edit.start:
            yield cursor, edit.start, display
            display += edit.start - cursor
        cursor = edit.end
    if cursor < end:
        yield cursor, end, display


def fragment_start_tokens(target):
    """Use actual AddSegment boundaries, including those inside paired units."""
    raw = [s.en_text.strip() for s in target.raw_source_segments]
    full = " ".join(raw)
    if not raw or not full.endswith(target.en_text):
        return ()  # No provenance: no surface corrections are eligible.
    cut = len(full) - len(target.en_text)
    starts, cursor = set(), 0
    for text in raw:
        starts.add(cursor - cut)
        cursor += len(text) + 1
    return tuple(
        i
        for i, token in enumerate(re.finditer(r"\S+", target.en_text))
        if i and token.start() in starts
    )


def join_candidates(english, starts, base_edits=()):
    tokens = list(re.finditer(r"\S+", english))
    candidates = []
    existing = {e.start for e in base_edits}
    for i in starts:
        if not 0 < i < len(tokens):
            continue
        previous, current = tokens[i - 1], tokens[i]
        # Never remove numeric separators, URLs, times, or punctuation-only tokens.
        punctuation = (
            EnglishEdit(previous.end() - 1, previous.end(), "")
            if re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*[,:]", previous.group())
            and previous.group().lower()
            not in {"http:", "https:", "ftp:", "file:", "mailto:", "urn:", "data:"}
            and previous.end() - 1 not in existing
            else None
        )
        # Acronyms, mixed-case names (OpenAI, mTLS) and I/I'm must retain case.
        word = re.fullmatch(r"[\"'“‘(\[]*([A-Z][a-z]+)(?:[.,:;!?\"'”’)\]]*)", current.group())
        lowercase = (
            EnglishEdit(
                current.start(0) + word.start(1),
                current.start(0) + word.start(1) + 1,
                word.group(1)[0].lower(),
            )
            if word
            and not previous.group().rstrip("\"'”’)]").endswith((".", "?", "!"))
            and current.start() + word.start(1) not in existing
            else None
        )
        if punctuation or lowercase:
            candidates.append((i, punctuation, lowercase))
    return tuple(candidates)


def candidate_payload(english, starts, base_edits=()):
    return [
        {
            "start_token": i,
            "can_remove_previous_punctuation": p is not None,
            "can_lowercase_initial": c is not None,
        }
        for i, p, c in join_candidates(english, starts, base_edits)
    ]


def apply_join_cleanup(english, chunks, decisions, starts, base_edits=()):
    """Ignore unsafe proposals locally; a bad edit must not stop retranslation."""
    from dataclasses import replace

    tokens = list(re.finditer(r"\S+", english))
    eligible = {i: (p, c) for i, p, c in join_candidates(english, starts, base_edits)}
    edits, rejected, seen = {e.start: e for e in base_edits}, [], set()
    for decision in decisions:
        i = decision.start_token
        if i in seen or i not in eligible:
            rejected.append({"start_token": i, "reason": "not_an_editable_fragment_join"})
            continue
        seen.add(i)
        if not any(
            c.en_start <= tokens[i - 1].start() and tokens[i].end() <= c.en_end for c in chunks
        ):
            rejected.append({"start_token": i, "reason": "separate_paragraphs"})
            continue
        punctuation, lowercase = eligible[i]
        for requested, edit, reason in (
            (decision.remove_previous_punctuation, punctuation, "protected_punctuation"),
            (decision.lowercase_initial, lowercase, "protected_case"),
        ):
            if requested:
                if edit is None:
                    rejected.append({"start_token": i, "reason": reason})
                else:
                    edits[edit.start] = edit
    return tuple(
        replace(
            chunk,
            edits=tuple(
                e
                for e in sorted(edits.values(), key=lambda e: e.start)
                if chunk.en_start <= e.start < e.end <= chunk.en_end
            ),
        )
        for chunk in chunks
    ), tuple(rejected)
