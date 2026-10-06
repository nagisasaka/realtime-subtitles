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
    geometry: str = ""
    english_size: int = 18
    japanese_size: int = 28
    english_weight: str = "normal"
    japanese_weight: str = "bold"
    transparency: int = 25
    always_on_top: bool = True
    noise_reduction: str = "far_field"

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
        result.transparency = max(0, min(70, result.transparency))
        if result.english_weight not in {"normal", "bold"}:
            result.english_weight = "normal"
        if result.japanese_weight not in {"normal", "bold"}:
            result.japanese_weight = "bold"
        if result.noise_reduction not in {"near_field", "far_field"}:
            result.noise_reduction = "far_field"
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
