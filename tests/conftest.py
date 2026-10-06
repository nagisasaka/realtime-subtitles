import pytest


@pytest.fixture(autouse=True)
def no_default_diarization_api(monkeypatch):
    # Existing Realtime tests must never send microphone fixtures to an external API.
    monkeypatch.setattr("realtime_subtitles.realtime_api.DIARIZATION_ENABLED", False)
