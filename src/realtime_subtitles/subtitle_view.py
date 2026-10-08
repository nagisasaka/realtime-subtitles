"""Bounded projection of authoritative history, independent of Tk and translation."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SubtitleViewState:
    en_text: str = ""
    ja_text: str = ""
    unit_id: int | None = None
    history_latest_id: int = -1
    speaker: str | None = None
    speaker_changed: bool = False
    translation_status: str = ""


def project_subtitles(unit, pending, partial, partial_speaker, advanced_unit_id=-1):
    """Keep a completed unit live until the next partial/final begins.

    Held raw sources are shown immediately together with their continuation;
    this only projects assembler ownership, it never assembles translation jobs.
    """
    selected = (
        unit if unit and unit.unit_id > advanced_unit_id and not pending and not partial else None
    )
    text = (
        " ".join(s.en_text.strip() for s in pending)
        if pending
        else selected.en_text
        if selected
        else ""
    )
    if partial:
        text = " ".join(part for part in (text, partial) if part)
    speaker = pending[-1].speaker if pending else unit.speaker if unit else None
    changed = pending[0].break_before if pending else unit.break_before if unit else False
    known = {None, "", "UU", "SU"}
    if partial and partial_speaker not in known:
        changed = changed or (speaker not in known and speaker != partial_speaker)
        speaker = partial_speaker
    ja = selected.ja_text or "" if selected and selected.translation_status == "completed" else ""
    return SubtitleViewState(
        en_text=text,
        ja_text=ja,
        unit_id=selected.unit_id if selected else None,
        history_latest_id=(unit.unit_id - bool(selected)) if unit else -1,
        speaker=speaker if speaker not in known else None,
        speaker_changed=bool(changed),
        translation_status=selected.translation_status if selected else "",
    )


def translation_caption(text, status):
    if text:
        return text
    if not status:
        return ""
    if status in {"pending", "translating", "retrying"}:
        return "翻訳待ち…"
    return "翻訳検証エラー" if status == "validation_failed" else "未翻訳"


def wrap_subtitle(text, measure, width, max_lines=2):
    """Pixel wrapping with word boundaries; omit old lines without changing history.

    English words are never split. An unfit single token is represented by an
    ellipsis, not a misleading fragment. CJK wraps between glyphs.
    """
    if width <= 0 or max_lines <= 0:
        return ""
    # Layout cost is bounded even if a server supplies an anomalously huge partial.
    truncated = len(text) > 6000
    text = text[-6000:]
    if truncated and " " in text:
        text = text.split(" ", 1)[1]
    tokens = re.findall(r"[\w'’/+-]+|\s+|[^\w\s]", text, re.UNICODE)
    # Keep Latin/numeric words whole; Japanese has no whitespace word boundaries.
    pieces = []
    for token in tokens:
        if any("\u2e80" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff" for c in token):
            pieces.extend(token)
        else:
            pieces.append(token)
    lines, current = [], ""
    for token in pieces:
        if token.isspace():
            if current and not current.endswith(" "):
                current += " "
            continue
        if measure(token) > width:
            token = "…" if measure("…") <= width else ""
        candidate = current + token
        if current and measure(candidate.rstrip()) > width:
            if token in "、。，．！？）」』】〉》" and len(current.rstrip()) > 1:
                value = current.rstrip()
                lines.append(value[:-1])
                current = value[-1] + token
            else:
                lines.append(current.rstrip())
                current = token
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines[-max_lines:])
