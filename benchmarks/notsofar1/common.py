"""Shared paths, manifest serialization and official scoring normalization."""

import hashlib
import importlib.util
import json
from pathlib import Path

DATASET = "microsoft/NOTSOFAR"
REVISION = "ba8fd0f034ce185fe4d24f47e53b4b8194795f07"
SPLIT = "dev_set/240825.1_dev1"
PREFIX = f"benchmark-datasets/{SPLIT}/MTG"
OFFICIAL_REV = "6f58e08b008f7530ba4141f0aeb02447c70b6fd7"
ROOT = Path("testdata/external/notsofar1")
MEETINGS = ("MTG_30884", "MTG_30861", "MTG_30862", "MTG_30917")
MAX_SECONDS = 1800
NORMALIZATION = {
    "name": "NOTSOFAR chime8 EnglishTextNormalizer",
    "source_revision": OFFICIAL_REV,
    "standardize_numbers_rev": True,
    "remove_fillers": True,
    "identical_for_reference_and_hypothesis": True,
}
SCORING = {
    "tool": "meeteval",
    "metrics": ["tcpwer", "tcorcwer"],
    "collar_seconds": 5,
    "reference": "utterance text and timestamps; preserve overlaps",
    "hypothesis": "AddSegment transcript and timestamps; retain speaker streams",
    "word_timing": "original GT retained; scoring uses default pseudo word timing",
}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def sha256(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def normalizer():
    # Exact pinned, licensed official implementation. No ASR/LLM inference.
    directory = Path(__file__).parent / "vendor" / "chime8"
    for name, digest in load(directory / "hashes.json").items():
        if sha256(directory / name) != digest:
            raise ValueError("Normalizer checksum mismatch")
    spec = importlib.util.spec_from_file_location(
        "_notsofar_text_normalizer",
        directory / "__init__.py",
        submodule_search_locations=[str(directory)],
    )
    import sys

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.get_txt_norm("chime8")


def categories(meta):
    tags = meta["Hashtags"]
    result = []
    for tag, label in [
        ("#TransientNoise=high", "TransientNoise"),
        ("#TalkNearWhiteboard", "TalkNearWhiteboard"),
        ("#DebateOverlaps", "DebateOverlaps"),
        ("#TurnsNoOverlap", "Low-overlap"),
    ]:
        if tag in tags:
            result.append(label)
    return result or ["Other"]
