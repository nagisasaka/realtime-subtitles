"""Keep sample time, network receipt time and authoritative character positions separate."""

import re
import statistics
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher

ALIGNMENT_SEARCH_RADIUS_SEC = 3  # Japanese audio timestamps only.
BOUNDARY_MERGE_RADIUS_SEC = 2
MIN_TEXT_SIMILARITY = 0.65
MIN_BOUNDARY_CONFIDENCE = 0.78
MAX_ALIGNMENT_DISTANCE_MS = 1500
FILLERS = {"um", "uh", "erm", "hmm"}


@dataclass(frozen=True)
class SpeakerSegment:
    speaker: str
    start_ms: int
    end_ms: int
    text: str | None = None


@dataclass(frozen=True)
class Anchor:
    sequence: int
    offset: int
    char_offset: int
    match_end: int
    confidence: float
    text_similarity: float | None = None
    time_ms: int | None = None
    received_monotonic_ms: int | None = None
    timing: str = "text"


@dataclass(frozen=True)
class SpeakerBoundary:
    id: str
    audio_time_ms: int
    speaker_before: str | None
    speaker_after: str | None
    confidence: float = 0.0
    en_char_offset: int | None = None
    timeline_id: str = ""
    text_hint: str = ""
    text_before: str = ""
    window_start_ms: int = 0
    window_end_ms: int = 0
    observations: int = 1
    positions: dict[str, Anchor] = field(default_factory=dict)

    @property
    def time_ms(self):
        return self.audio_time_ms


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"}))
    text = text.replace("'", "")
    text = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    return " ".join(word for word in text.split() if word not in FILLERS)


def similarity(left, right):
    return SequenceMatcher(
        None, normalize_text(left).split(), normalize_text(right).split(), autojunk=False
    ).ratio()


def segment_boundaries(segments, timeline_id, window_start_ms, window_end_ms):
    result = []
    ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        # Simultaneous speech is not an unambiguous paragraph transition.
        if previous.speaker == current.speaker or current.start_ms < previous.end_ms - 200:
            continue
        if not current.text or not normalize_text(current.text):
            continue
        result.append(
            SpeakerBoundary(
                id=uuid.uuid4().hex,
                audio_time_ms=current.start_ms,
                speaker_before=previous.speaker,
                speaker_after=current.speaker,
                timeline_id=timeline_id,
                text_hint=current.text,
                text_before=previous.text or "",
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )
        )
    return result


def _text_map(records, language, timeline_id):
    selected = [
        r for r in records if r["language"] == language and r.get("timeline_id") == timeline_id
    ]
    text, spans = "", []
    for record in selected:
        start = len(text)
        text += record["delta"]
        spans.append((start, len(text), record))
    return text, spans


def _record_at(spans, offset):
    for start, end, record in spans:
        if start <= offset < end:
            return record, offset - start
    return None, 0


def _words(text):
    return [
        match
        for match in re.finditer(r"\w+(?:['’‘ʼ`]\w+)*", text, re.UNICODE)
        if normalize_text(match.group()) not in FILLERS
    ]


def align_english(boundary, records, *, lower=0, upper=None):
    """Text-only fuzzy alignment. Audio/receipt times never become EN positions."""
    text, spans = _text_map(records, "en", boundary.timeline_id)
    hint = normalize_text(boundary.text_hint).split()[:12]
    before = normalize_text(boundary.text_before).split()[-8:]
    if len(hint) < 5:
        return None
    words = _words(text)
    candidates = []
    for index, word in enumerate(words):
        record, offset = _record_at(spans, word.start())
        if record is None:
            continue
        char_offset = record["char_start"] + offset
        if char_offset < lower or (upper is not None and char_offset >= upper):
            continue
        best, match_end = 0.0, word.end()
        # Different token counts tolerate missing articles and filler words.
        for length in range(max(5, len(hint) - 3), len(hint) + 4):
            if index + length > len(words):
                continue
            end = words[index + length - 1].end()
            score = similarity(" ".join(hint), text[word.start() : end])
            if score > best:
                best, match_end = score, end
        if best < MIN_TEXT_SIMILARITY:
            continue
        previous = (
            text[words[max(0, index - len(before))].start() : word.start()]
            if before and index
            else ""
        )
        context = similarity(" ".join(before), previous) if before else 0.0
        confidence = 0.8 * best + 0.2 * context
        if confidence < MIN_BOUNDARY_CONFIDENCE:
            continue
        end_record, end_offset = _record_at(spans, match_end - 1)
        if upper is not None and end_record["char_start"] + end_offset + 1 > upper:
            continue
        candidates.append(
            Anchor(
                record["sequence"],
                offset,
                char_offset,
                end_record["char_start"] + end_offset + 1,
                confidence,
                best,
                received_monotonic_ms=record.get("received_monotonic_ms"),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda a: a.confidence, reverse=True)
    best = candidates[0]
    # Nearby starts are alternate tokenizations. Distant repeated phrases are ambiguous.
    alternatives = [a for a in candidates[1:] if abs(a.char_offset - best.char_offset) > 50]
    if alternatives and best.confidence - alternatives[0].confidence < 0.05:
        return None
    return best


def _sentence_positions(records, language, timeline_id):
    text, spans = _text_map(records, language, timeline_id)
    pattern = r"(?:\A|[。！？!?\n])\s*" if language == "ja" else r"(?:\A|[.!?\n])\s*"
    result = []
    for match in re.finditer(pattern, text):
        record, offset = _record_at(spans, match.end())
        if record is not None and record["delta"][offset:].strip():
            result.append((record, offset))
    return result


def align_japanese(boundary, records, english):
    if english is None:
        return None
    sentences = _sentence_positions(records, "ja", boundary.timeline_id)
    candidates = []
    for record, offset in sentences:
        if record.get("timing_source") != "elapsed" or record.get("audio_time_ms") is None:
            continue
        stamp = record["audio_time_ms"]
        distance = abs(stamp - boundary.audio_time_ms)
        if distance <= MAX_ALIGNMENT_DISTANCE_MS:
            confidence = min(english.confidence, 1 - distance / 6000)
            if confidence >= MIN_BOUNDARY_CONFIDENCE:
                position = record["char_start"] + offset
                candidates.append(
                    Anchor(
                        record["sequence"],
                        offset,
                        position,
                        position,
                        confidence,
                        time_ms=stamp,
                        timing="elapsed",
                    )
                )
    if candidates:
        candidates.sort(key=lambda a: abs(a.time_ms - boundary.audio_time_ms))
        if len(candidates) > 1:
            first = abs(candidates[0].time_ms - boundary.audio_time_ms)
            second = abs(candidates[1].time_ms - boundary.audio_time_ms)
            if second - first < 200:
                return None
        return candidates[0]
    # If JA has API timing, never override a failed audio alignment with receipt time.
    if any(r.get("elapsed_ms") is not None for r, _ in sentences):
        return None
    en_sentences = _sentence_positions(records, "en", boundary.timeline_id)
    # Receipt-order pairing is merely a heuristic. Require several consistent samples.
    if len(en_sentences) != len(sentences) or len(sentences) < 4 or english.confidence < 0.9:
        return None
    pairs = list(zip(en_sentences, sentences, strict=True))
    lags = [
        ja[0]["received_monotonic_ms"] - en[0]["received_monotonic_ms"]
        for en, ja in pairs
        if en[0].get("received_monotonic_ms") is not None
        and ja[0].get("received_monotonic_ms") is not None
    ]
    if len(lags) < 4:
        return None
    lag = statistics.median(lags)
    if not 0 <= lag <= 10_000 or max(abs(value - lag) for value in lags) > 500:
        return None
    if english.received_monotonic_ms is None:
        return None
    for record, offset in sentences:
        distance = abs(record["received_monotonic_ms"] - english.received_monotonic_ms - lag)
        if distance <= 500:
            position = record["char_start"] + offset
            candidates.append(
                Anchor(
                    record["sequence"],
                    offset,
                    position,
                    position,
                    0.78,
                    received_monotonic_ms=record["received_monotonic_ms"],
                    timing="receive_sentence_estimate",
                )
            )
    return candidates[0] if len(candidates) == 1 else None


class BoundaryTimeline:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self.revision = 0

    def snapshot(self):
        with self._lock:
            return self.revision, sorted(
                self._items.values(), key=lambda b: (b.timeline_id, b.audio_time_ms)
            )

    def serializable(self):
        return [asdict(boundary) for boundary in self.snapshot()[1]]

    def merge(self, incoming):
        with self._lock:
            for old in self._items.values():
                if (
                    old.timeline_id != incoming.timeline_id
                    or old.window_start_ms == incoming.window_start_ms
                    or max(old.window_start_ms, incoming.window_start_ms)
                    >= min(old.window_end_ms, incoming.window_end_ms)
                    or abs(old.audio_time_ms - incoming.audio_time_ms)
                    > BOUNDARY_MERGE_RADIUS_SEC * 1000
                    or similarity(old.text_hint, incoming.text_hint) < MIN_TEXT_SIMILARITY
                ):
                    continue
                old_context = min(
                    old.audio_time_ms - old.window_start_ms, old.window_end_ms - old.audio_time_ms
                )
                new_context = min(
                    incoming.audio_time_ms - incoming.window_start_ms,
                    incoming.window_end_ms - incoming.audio_time_ms,
                )
                chosen = incoming if new_context >= old_context else old
                # Retain a validated EN text position until a better match is available.
                updated = replace(
                    chosen,
                    id=old.id,
                    observations=old.observations + 1,
                    positions=old.positions,
                    en_char_offset=old.en_char_offset,
                    confidence=old.confidence,
                )
                self._items[old.id] = updated
                self.revision += 1
                return updated, old
            self._items[incoming.id] = incoming
            self.revision += 1
            return incoming, None

    def align(self, boundary_id, records, *, reconsider_en=True):
        with self._lock:
            boundary = self._items[boundary_id]
            neighbors = [
                b
                for b in self._items.values()
                if b.timeline_id == boundary.timeline_id and b.id != boundary.id
            ]
        previous = [
            b
            for b in neighbors
            if b.audio_time_ms < boundary.audio_time_ms and b.en_char_offset is not None
        ]
        following = [
            b
            for b in neighbors
            if b.audio_time_ms > boundary.audio_time_ms and b.en_char_offset is not None
        ]
        lower = max((b.positions["en"].match_end for b in previous), default=0)
        upper = min((b.en_char_offset for b in following), default=None)
        old_en = boundary.positions.get("en")
        en = (
            align_english(boundary, records, lower=lower, upper=upper)
            if reconsider_en or old_en is None
            else old_en
        )
        if (
            old_en
            and old_en.char_offset >= lower
            and (upper is None or old_en.match_end <= upper)
            and (en is None or en.confidence < old_en.confidence)
        ):
            en = old_en
        ja = align_japanese(boundary, records, en)
        positions = {lang: anchor for lang, anchor in [("en", en), ("ja", ja)] if anchor}
        updated = replace(
            boundary,
            positions=positions,
            confidence=en.confidence if en else 0.0,
            en_char_offset=en.char_offset if en else None,
        )
        with self._lock:
            if self._items.get(boundary_id) == boundary and updated != boundary:
                self._items[boundary_id] = updated
                self.revision += 1
        return updated
