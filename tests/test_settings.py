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


def test_ruby_layout_migrates_old_ja_size_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"english_size": 30, "japanese_size": 29}))
    settings = Settings.load(path)
    assert settings.japanese_size == 18
    settings.japanese_size = 22
    settings.save(path)
    assert Settings.load(path).japanese_size == 22
    path.write_text(json.dumps({"subtitle_layout_version": "broken"}))
    assert Settings.load(path).subtitle_layout_version == 2


def test_geometry_records_dpi_without_changing_logical_font_size(tmp_path):
    path = tmp_path / "settings.json"
    Settings(geometry="1800x900+3300+180", geometry_dpi=240, english_size=20).save(path)
    saved = Settings.load(path)
    assert saved.geometry_dpi == 240 and saved.english_size == 20
    path.write_text('{"geometry_dpi": -1}')
    assert Settings.load(path).geometry_dpi == 0
