"""Conservative local checks, not semantic proofreading or ASR correction."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from difflib import SequenceMatcher


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    message: str


# Canonical dimensions and scales. No exchange rates or ambiguous binary byte conversions.
UNIT_GROUPS = {
    "USD": ("us$", "us dollars", "dollars", "dollar", "usd", "米ドル", "ドル", "$"),
    "EUR": ("euros", "euro", "eur", "ユーロ", "€"),
    "GBP": ("pounds", "pound", "gbp", "ポンド", "£"),
    "JPY": ("yen", "jpy", "円", "¥"),
    "%": ("percent", "per cent", "パーセント", "%"),
    "tokens": ("tokens", "token", "トークン"),
    "seconds": ("seconds", "second", "secs", "sec", "秒", "s"),
    "minutes": ("minutes", "minute", "mins", "min", "分"),
    "hours": ("hours", "hour", "hrs", "hr", "時間"),
    "milliseconds": ("milliseconds", "millisecond", "msec", "ms", "ミリ秒"),
    "meters": ("meters", "meter", "metres", "metre", "メートル", "m"),
    "kilometers": ("kilometers", "kilometer", "km", "キロメートル"),
    "centimeters": ("centimeters", "centimeter", "cm", "センチメートル"),
    "grams": ("grams", "gram", "g", "グラム"),
    "kilograms": ("kilograms", "kilogram", "kg", "キログラム"),
    "people": ("people", "persons", "person", "人"),
}
UNITS = {word: unit for unit, words in UNIT_GROUPS.items() for word in words}
CONVERSIONS = {
    "minutes": ("seconds", Decimal(60)),
    "hours": ("seconds", Decimal(3600)),
    "milliseconds": ("seconds", Decimal("0.001")),
    "kilometers": ("meters", Decimal(1000)),
    "centimeters": ("meters", Decimal("0.01")),
    "kilograms": ("grams", Decimal(1000)),
}
CURRENCIES = {"USD", "EUR", "GBP", "JPY"}
SCALES = {
    "thousand": 1000,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
    "百": 100,
    "千": 1000,
    "万": 10**4,
    "億": 10**8,
    "兆": 10**12,
}
UNIT_PATTERN = "|".join(
    re.escape(x) + (r"(?![a-z])" if x.isascii() and x.isalpha() else "")
    for x in sorted(UNITS, key=len, reverse=True)
)
NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<currency>US\$|USD|EUR|GBP|JPY|[$€£¥])?\s*"
    r"(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<scale>(?:\s*(?:trillion|billion|million|thousand|[百千万億兆]))*)"
    r"\s*(?P<unit>" + UNIT_PATTERN + r")?",
    re.IGNORECASE,
)
SCALE_PATTERN = re.compile(r"trillion|billion|million|thousand|[百千万億兆]", re.IGNORECASE)


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str | None
    literal_value: Decimal = field(compare=False)


def normalize(text):
    return unicodedata.normalize("NFKC", text).replace("−", "-")


def quantities(text):
    text = normalize(text)
    parsed = []
    for match in NUMBER.finditer(text):
        # GPT-6, v2.1 etc. are identifiers, not confidently parsed quantities.
        before = text[: match.start()].rstrip()
        if before.endswith("-") and len(before) > 1 and before[-2].isalpha():
            continue
        value = Decimal(match["number"].replace(",", ""))
        for scale in SCALE_PATTERN.findall(match["scale"]):
            value *= SCALES[scale.lower()]
        unit = UNITS.get((match["currency"] or match["unit"] or "").lower())
        # Bare "pounds" can mean weight, not currency. Never guess GBP from it.
        if not match["currency"] and (match["unit"] or "").lower() in {"pound", "pounds"}:
            unit = None
        # A hyphen immediately between numbers is a range separator, not a
        # negative upper endpoint. An explicit second minus remains negative.
        if parsed and match["number"].startswith("-"):
            previous_match, _ = parsed[-1]
            if (
                not text[previous_match.end() : match.start()].strip()
                and not previous_match.group()[-1].isspace()
            ):
                value = abs(value)
        parsed.append((match, Quantity(value, unit, value)))
    # In "40 to 50 minutes" the trailing unit applies to both endpoints.
    # Only explicit adjacent ranges share a unit; never infer one from context.
    for i in range(1, len(parsed)):
        left_match, left = parsed[i - 1]
        right_match, right = parsed[i]
        gap = text[left_match.end() : right_match.start()].strip()
        if (
            not gap
            and right_match["number"].startswith("-")
            and not left_match.group()[-1].isspace()
        ):
            gap = "-"
        if re.fullmatch(r"to|から|[~〜～–—-]", gap, re.IGNORECASE):
            if left.unit is None and right.unit is not None:
                parsed[i - 1] = left_match, replace(left, unit=right.unit)
            elif right.unit is None and left.unit is not None:
                parsed[i] = right_match, replace(right, unit=left.unit)
    result = []
    for _, quantity in parsed:
        if quantity.unit in CONVERSIONS:
            unit, multiplier = CONVERSIONS[quantity.unit]
            quantity = replace(quantity, value=quantity.value * multiplier, unit=unit)
        result.append(quantity)
    return result


def unmatched_quantities(source, translated):
    """Match exact quantities first, then equally written numbers with omitted units.

    A bare ASR "10 to 15" versus JA "10〜15分" is uncertain unit evidence,
    not a confident 15 -> 900 numeric error. Preserve that uncertainty as a
    warning; explicit incompatible units and changed values remain errors.
    """
    source, translated = list(source), list(translated)
    for q in source[:]:
        if q in translated:
            source.remove(q)
            translated.remove(q)
    omitted = False
    for q in source[:]:
        for j, candidate in enumerate(translated):
            one_unit_missing = (q.unit is None) != (candidate.unit is None)
            if one_unit_missing and q.literal_value == candidate.literal_value:
                source.remove(q)
                translated.pop(j)
                omitted = True
                break
    return source, translated, omitted


def compact(text):
    return "".join(c.lower() for c in normalize(text) if c.isalnum())


class TranslationValidator:
    def validate(self, target, output, *, context=(), previous_translations=()):
        issues = []

        def add(code, severity, message):
            if not any(i.code == code and i.message == message for i in issues):
                issues.append(ValidationIssue(code, severity, message))

        if not isinstance(output, str) or not output.strip():
            return [ValidationIssue("invalid_output", "error", "Translation is empty.")]
        if len(output) > max(2000, len(target) * 8):
            return [ValidationIssue("invalid_output", "error", "Translation is excessively long.")]
        if any(
            (unicodedata.category(c) in {"Cc", "Cs", "Cf"} and c not in "\n\r\t\u200d")
            or c == "\ufffd"
            for c in output
        ):
            add("invalid_output", "error", "Invalid control or replacement character.")
        if "```" in output:
            add("invalid_output", "error", "Return subtitle text, without a code fence.")
        source = target + " " + " ".join(x.get("text", "") for x in context)
        allowed_letters = ("LATIN", "CJK", "HIRAGANA", "KATAKANA", "IDEOGRAPHIC", "GREEK")
        if any(
            c.isalpha()
            and c not in source
            and not unicodedata.name(c, "").startswith(allowed_letters)
            for c in output
        ):
            add(
                "unexpected_script",
                "error",
                "Unexpected writing system absent from source/context.",
            )
        # Observed corrupt token, not a blanket ban on Latin technical terms.
        for token in re.findall(r"[A-Za-z]+", output):
            if token.lower() == "cuntegn" and token.lower() not in source.lower():
                add("invalid_output", "error", "Observed corrupt Latin token in Japanese output.")
        tail = re.search(r"[ぁ-ん一-鿿]([a-z]{5,})$", output.strip())
        if tail and tail[1].lower() not in source.lower():
            add("invalid_output", "warning", "Unverified Latin suffix; may be a legitimate term.")

        en, ja = quantities(target), quantities(output)
        expected_currencies = {q.unit for q in en if q.unit in CURRENCIES}
        # Written-number currencies remain detectable without checking their numeric value.
        for currency in expected_currencies:
            if not any(
                re.search(
                    re.escape(word) + (r"\b" if word.isascii() and word.isalpha() else ""),
                    normalize(output),
                    re.IGNORECASE,
                )
                for word in UNIT_GROUPS[currency]
            ):
                add("currency_mismatch", "error", f"Preserve the source currency {currency}.")
        if en and not ja:
            add(
                "number_mismatch", "warning", "Numbers may use unsupported written-number notation."
            )
        elif en and ja:
            en, ja, omitted = unmatched_quantities(en, ja)
            if omitted:
                add(
                    "unit_omitted",
                    "warning",
                    "Matching numbers have a unit on only one side; unit equivalence is uncertain.",
                )
            ev, jv = Counter(q.value for q in en), Counter(q.value for q in ja)
            # Mixed Japanese compound magnitudes (1億2000万) and unsupported units are ambiguous.
            compound = bool(re.search(r"[億兆万]\s*\d", normalize(output)))
            unsupported = bool(
                re.search(
                    r"°|fahrenheit|celsius|bytes|\b[kmgt]b\b|バイト|摂氏|華氏|[一二三四五六七八九十]",
                    target + output,
                    re.IGNORECASE,
                )
            )
            unsupported = unsupported or bool(
                re.search(r"\d\s*[KMB]\b|\d+:\d+|(?i:\bpounds?\b|\blbs?\b)|割", target + output)
            )
            if ev != jv:
                severity = (
                    "error"
                    if len(en) == len(ja) and not compound and not unsupported
                    else "warning"
                )
                add(
                    "number_mismatch",
                    severity,
                    "Numeric values differ after supported normalization.",
                )
            if ev == jv:
                for q in en:
                    if q.unit and q.unit not in CURRENCIES and q not in ja and not unsupported:
                        add("unit_mismatch", "error", f"Preserve quantity unit {q.unit}.")
        # Unknown numerical syntax isn't guessed or silently repaired.
        sentences = [compact(x) for x in re.split(r"[。！？\n]", output) if len(compact(x)) >= 8]
        repeated = any(n >= 3 for n in Counter(sentences).values())
        if repeated or re.search(r"(.{6,60}?)\1\1", compact(output)):
            source_repeats = bool(re.search(r"(.{6,60}?)\1\1", compact(target)))
            add(
                "suspicious_repetition",
                "warning" if source_repeats else "error",
                "Repeated phrase; preserve only repetition actually in TARGET.",
            )
        rendered = compact(output)
        for previous_en, previous_ja in previous_translations:
            prior = compact(previous_ja or "")
            if len(prior) < 24 or len(rendered) < max(40, len(target) * 2):
                continue
            if prior in rendered and len(prior) >= len(rendered) * 0.45:
                similarity = SequenceMatcher(
                    None, compact(target), compact(previous_en), autojunk=False
                ).ratio()
                if similarity < 0.45:
                    add(
                        "possible_context_leak",
                        "error",
                        "A long previous translation was repeated.",
                    )
                    break
        return issues


def serious(issues):
    return any(issue.severity == "error" for issue in issues)


def retry_instructions(issues):
    # Only trusted validator messages are inserted into instructions; never echo candidate text.
    return (
        "A previous candidate failed local validation: "
        + " ".join(i.message for i in issues if i.severity == "error")
        + " Translate the identical TARGET only. Preserve its numbers, currencies and units. "
        "Do not silently repair suspected ASR errors. Return only clean Japanese subtitles."
    )
