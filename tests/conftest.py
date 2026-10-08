import pytest


@pytest.fixture(autouse=True)
def no_default_diarization_api(monkeypatch):
    # Existing Realtime tests must never send microphone fixtures to an external API.
    monkeypatch.setattr("realtime_subtitles.realtime_api.DIARIZATION_ENABLED", False)


@pytest.fixture(autouse=True)
def isolated_autosave_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "realtime_subtitles.autosave.default_directory", lambda: tmp_path / "autosave"
    )


@pytest.fixture(autouse=True)
def isolated_audio_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "realtime_subtitles.audio_recording.default_directory", lambda: tmp_path / "recordings"
    )


def make_unit(history, event, session):
    source = history.record_segment(event, session)
    return (
        history.emit_unit([source], "test_fixture", round(history.clock() * 1000))
        if source
        else None
    )


@pytest.fixture(autouse=True)
def no_external_history_reconstruction(monkeypatch):
    class KeepOriginal:
        def __init__(self, key, history):
            pass

        async def translate(self, *args, **kwargs):
            raise RuntimeError("Reconstruction API disabled in offline tests")

        async def close(self):
            pass

    monkeypatch.setattr(
        "realtime_subtitles.history_reconstruction.ReconstructionTranslator", KeepOriginal
    )
