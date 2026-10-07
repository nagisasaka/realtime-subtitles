"""Bounded projection of authoritative history, independent of Tk and translation."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SubtitleViewState:
    confirmed_en: str = ""
    live_en_partial: str = ""
    ja_text: str = ""
    confirmed_unit_id: int | None = None
    ja_unit_id: int | None = None
    speaker: str | None = None
    speaker_changed: bool = False
    translation_status: str = ""


def project_subtitles(unit, pending, partial, partial_speaker):
    text = " ".join(s.en_text.strip() for s in pending) if pending else unit.en_text if unit else ""
    speaker = pending[-1].speaker if pending else unit.speaker if unit else None
    changed = pending[0].break_before if pending else unit.break_before if unit else False
    known = {None, "", "UU", "SU"}
    if partial and partial_speaker not in known:
        changed = changed or (speaker not in known and speaker != partial_speaker)
        speaker = partial_speaker
    selected = unit if unit and not pending else None
    ja = selected.ja_text or "" if selected and selected.translation_status == "completed" else ""
    return SubtitleViewState(
        text,
        partial,
        ja,
        selected.unit_id if selected else None,
        selected.unit_id if selected and ja else None,
        speaker if speaker not in known else None,
        bool(changed),
        selected.translation_status if selected else "",
    )


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
