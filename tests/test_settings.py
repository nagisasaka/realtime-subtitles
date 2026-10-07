import json

from realtime_subtitles.settings import Settings


def test_roundtrip_and_no_secrets(tmp_path):
    path = tmp_path / "config" / "settings.json"
    settings = Settings(
        microphone="Windows WASAPI|Mic",
        geometry="1000x460-1920+600",
        input_source="audio_file",
        audio_file="C:/recordings/talk.wav",
        always_on_top=False,
        transparency=40,
        english_weight="bold",
        japanese_weight="normal",
    )
    settings.save(path)
    assert Settings.load(path) == settings
    assert "key" not in path.read_text()


def test_corrupt_and_out_of_range_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("broken json")
    assert Settings.load(path) == Settings()
    path.write_text(
        json.dumps(
            {
                "english_size": 900,
                "japanese_size": -100,
                "always_on_top": "false",
                "geometry": "invalid",
                "input_source": "invalid",
                "microphone": 4,
                "transparency": 200,
                "english_weight": "invalid",
                "japanese_weight": "invalid",
            }
        )
    )
    settings = Settings.load(path)
    assert settings.english_size == 64 and settings.japanese_size == 8
    assert settings.english_weight == "bold" and settings.japanese_weight == "normal"
    assert settings.transparency == 70
    assert settings.always_on_top is True
    assert settings.geometry == "" and settings.microphone == ""
    assert settings.input_source == "microphone"
