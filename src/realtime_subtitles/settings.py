import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
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
    geometry_dpi: int = 0
    english_size: int = 27
    japanese_size: int = 18
    live_english_size: int = 30
    live_japanese_size: int = 18
    subtitle_layout_version: int = 3
    english_weight: str = "normal"
    japanese_weight: str = "normal"
    live_english_weight: str = "bold"
    live_japanese_weight: str = "normal"
    transparency: int = 25
    always_on_top: bool = True
    summary_logs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path=None):
        try:
            data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        result = cls()
        if not isinstance(data, dict):
            return result
        for item in fields(cls):
            value = data.get(item.name)
            if type(value) is type(getattr(result, item.name)):
                setattr(result, item.name, value)
        if not 48 <= result.geometry_dpi <= 768:
            result.geometry_dpi = 0
        result.english_size = max(8, min(64, result.english_size))
        result.japanese_size = max(8, min(64, result.japanese_size))
        version = data.get("subtitle_layout_version", 1)
        legacy = type(version) is not int or version < 3
        if legacy and type(data.get("english_size")) is not int:
            result.english_size = 30
        if not isinstance(version, int) or version < 2:
            # The previous UI used Japanese as a full-size standalone caption.
            result.japanese_size = max(
                8, min(result.japanese_size, round(result.english_size * 0.6))
            )
        if legacy:
            # Previously EN history was 90% of the shared size, always normal.
            # Preserve the visible sizes while giving all four fonts their own controls.
            result.live_english_size = result.english_size
            result.live_japanese_size = result.japanese_size
            result.live_english_weight = data.get("english_weight", "bold")
            result.live_japanese_weight = result.japanese_weight
            result.english_size = max(8, round(result.english_size * 0.9))
            result.english_weight = "normal"
        result.subtitle_layout_version = 3
        for name in ("english_size", "japanese_size", "live_english_size", "live_japanese_size"):
            setattr(result, name, max(8, min(64, getattr(result, name))))
        result.transparency = max(0, min(70, result.transparency))
        for name in (
            "english_weight",
            "japanese_weight",
            "live_english_weight",
            "live_japanese_weight",
        ):
            if getattr(result, name) not in ("normal", "bold"):
                setattr(result, name, "bold" if name == "live_english_weight" else "normal")
        if result.input_source not in {"microphone", "audio_file"}:
            result.input_source = "microphone"
        result.summary_logs = list(
            dict.fromkeys(p for p in result.summary_logs if isinstance(p, str) and p.strip())
        )
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
