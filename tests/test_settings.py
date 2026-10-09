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
        live_english_size=36,
        live_japanese_size=21,
        live_english_weight="normal",
        live_japanese_weight="bold",
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
                "live_english_size": -200,
                "live_japanese_size": 1000,
                "subtitle_layout_version": 3,
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
    assert settings.english_weight == "normal" and settings.japanese_weight == "normal"
    assert settings.live_english_size == 8 and settings.live_japanese_size == 64
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
    assert Settings.load(path).subtitle_layout_version == 3


def test_independent_font_migration_preserves_visible_sizes_and_weights(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "subtitle_layout_version": 2,
                "english_size": 40,
                "japanese_size": 23,
                "english_weight": "bold",
                "japanese_weight": "bold",
            }
        )
    )
    saved = Settings.load(path)
    assert (saved.live_english_size, saved.live_japanese_size) == (40, 23)
    assert (saved.english_size, saved.japanese_size) == (36, 23)
    assert saved.english_weight == "normal" and saved.live_english_weight == "bold"
    assert saved.japanese_weight == saved.live_japanese_weight == "bold"
    saved.english_size = 32
    saved.live_english_size = 24
    saved.save(path)
    assert Settings.load(path) == saved  # migration runs once, no accumulating 90% scaling


def test_geometry_records_dpi_without_changing_logical_font_size(tmp_path):
    path = tmp_path / "settings.json"
    Settings(geometry="1800x900+3300+180", geometry_dpi=240, english_size=20).save(path)
    saved = Settings.load(path)
    assert saved.geometry_dpi == 240 and saved.english_size == 20
    path.write_text('{"geometry_dpi": -1}')
    assert Settings.load(path).geometry_dpi == 0
