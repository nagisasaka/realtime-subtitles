import pytest

from realtime_subtitles.translation_validation import TranslationValidator, serious


@pytest.mark.parametrize(
    ("en", "ja"),
    [
        ("$2 million", "200万ドル"),
        ("USD 2 million", "200万米ドル"),
        ("25 percent", "25%"),
        ("25%", "25パーセント"),
        ("2 minutes", "120秒"),
        ("1 hour", "60分"),
        ("1000 ms", "1秒"),
        ("1 kilometer", "1000メートル"),
        ("2 kilograms", "2000グラム"),
        ("3.14 meters", "3.14メートル"),
        ("12,500 tokens", "12,500トークン"),
        (
            "Use Kubernetes, OpenAI, MCP, Model Armor, Agent Gateway, CI/CD, JWT and mTLS.",
            "Kubernetes、OpenAI、MCP、Model Armor、Agent Gateway、CI/CD、JWT、mTLSを使います。",
        ),
        ("GPT-6 is available.", "GPT-6が利用できます。"),
        ("It is important.", "それは重要です。"),
    ],
)
def test_supported_equivalences_and_technical_terms(en, ja):
    assert not serious(TranslationValidator().validate(en, ja))


@pytest.mark.parametrize(
    ("en", "ja", "code"),
    [
        ("$2 million", "200万トークン", "currency_mismatch"),
        ("$2 million", "200万ユーロ", "currency_mismatch"),
        ("25 percent", "26%", "number_mismatch"),
        ("25 percent", "25秒", "unit_mismatch"),
        ("2 seconds", "3秒", "number_mismatch"),
        ("Use secure connections.", "安全な接続\x00を使います。", "invalid_output"),
        ("The landscape is", "状況はcuntegn", "invalid_output"),
        ("Sanitize your payload.", "サニタイズする તમારા", "unexpected_script"),
        ("Hello.", "", "invalid_output"),
        ("Hello.", "\ufffdこんにちは", "invalid_output"),
        (
            "Avoid unnecessary exposure.",
            "不要な公開は避けてください。" * 4,
            "suspicious_repetition",
        ),
    ],
)
def test_high_confidence_failures(en, ja, code):
    issues = TranslationValidator().validate(en, ja)
    assert any(i.code == code and i.severity == "error" for i in issues)


def test_unknown_or_ambiguous_forms_warn_instead_of_retrying():
    for en, ja in [
        ("2 seconds", "二秒"),
        ("120 million dollars", "1億2000万ドル"),
        ("1 KB", "1024バイト"),
    ]:
        issues = TranslationValidator().validate(en, ja)
        assert issues and not serious(issues)


def test_context_leak_conservative_and_intentional_repetition_allowed():
    prior = "このシステムは機密情報を保護するために認証とアクセス制御を組み合わせて実装しています。"
    previous = [("We combine authentication and access control to protect private data.", prior)]
    v = TranslationValidator()
    issues = v.validate("Next.", prior + "次です。", previous_translations=previous)
    assert any(i.code == "possible_context_leak" and i.severity == "error" for i in issues)
    assert not serious(
        v.validate("It is important.", "それは重要です。", previous_translations=previous)
    )
    assert not serious(
        v.validate("Never expose private information. " * 3, "秘密情報を公開しないでください。" * 3)
    )


def test_source_language_characters_are_allowed_when_actually_quoted():
    assert not serious(TranslationValidator().validate("The name is తెలుగు.", "名前はతెలుగుです。"))


@pytest.mark.parametrize(
    ("en", "ja"),
    [
        ("2M tokens", "200万トークン"),
        ("It weighs 2 pounds.", "重さは約907グラムです。"),
        ("It takes 1:30.", "1分30秒かかります。"),
        ("50 percent", "5割"),
    ],
)
def test_ambiguous_conversions_only_warn(en, ja):
    issues = TranslationValidator().validate(en, ja)
    assert not any(i.severity == "error" for i in issues)


def test_unicode_minus_and_explicit_currency_are_unambiguous():
    v = TranslationValidator()
    assert not v.validate("-20 meters", "−20メートル")
    assert any(
        i.code == "currency_mismatch" and i.severity == "error"
        for i in v.validate("USD 2 million", "200万円")
    )
