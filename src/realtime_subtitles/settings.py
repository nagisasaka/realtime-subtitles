import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def settings_path():
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "RealtimeSubtitles" / "settings.json"


@dataclass
class Settings:
    microphone: str = ""
    input_source: str = "microphone"
    audio_file: str = ""
    audio_monitor: bool = False
    geometry: str = ""
    english_size: int = 30
    japanese_size: int = 18
    subtitle_layout_version: int = 2
    english_weight: str = "bold"
    japanese_weight: str = "normal"
    transparency: int = 25
    always_on_top: bool = True

    @classmethod
    def load(cls, path=None):
        try:
            data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        result = cls()
        if not isinstance(data, dict):
            return result
        for field in fields(cls):
            value = data.get(field.name)
            if type(value) is type(getattr(result, field.name)):
                setattr(result, field.name, value)
        result.english_size = max(8, min(64, result.english_size))
        result.japanese_size = max(8, min(64, result.japanese_size))
        version = data.get("subtitle_layout_version", 1)
        if not isinstance(version, int) or version < 2:
            # The previous UI used Japanese as a full-size standalone caption.
            result.japanese_size = max(
                8, min(result.japanese_size, round(result.english_size * 0.6))
            )
        result.subtitle_layout_version = 2
        result.transparency = max(0, min(70, result.transparency))
        if result.english_weight not in {"normal", "bold"}:
            result.english_weight = "bold"
        if result.japanese_weight not in {"normal", "bold"}:
            result.japanese_weight = "normal"
        if result.input_source not in {"microphone", "audio_file"}:
            result.input_source = "microphone"
        if not re.fullmatch(r"\d{3,5}x\d{3,5}[+-]\d{1,6}[+-]\d{1,6}", result.geometry):
            result.geometry = ""
        return result

    def save(self, path=None):
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
