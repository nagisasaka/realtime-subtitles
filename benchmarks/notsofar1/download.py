"""Selective public HF downloads: metadata catalog first, one distant WAV per meeting."""

import argparse
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .common import (
    DATASET,
    MEETINGS,
    PREFIX,
    REVISION,
    ROOT,
    categories,
    load,
    save,
    sha256,
)


def fetch(url, path, max_bytes=500_000_000):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as out:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Download size limit exceeded")
                out.write(chunk)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def remote(mid, filename):
    return f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{PREFIX}/{mid}/{filename}"


def choose_device(devices):
    candidates = [
        d
        for d in devices
        if d["is_close_talk"] is False
        and d["is_mc"] is False
        and d["channels_num"] == 1
        and d["wav_file_names"].startswith("sc_")
    ]
    if not candidates:
        raise ValueError("No single-channel distant recording")
    return min(candidates, key=lambda d: (d["device_name"] != "meetup_0", d["device_name"]))


def download(root=ROOT):
    root.mkdir(parents=True, exist_ok=True)
    catalog = root / "catalog.json"
    if not catalog.exists():
        url = f"https://huggingface.co/api/datasets/{DATASET}/tree/{REVISION}/{PREFIX}?limit=1000"
        with urllib.request.urlopen(url, timeout=30) as response:
            rows = json.load(response)
        # This fixed split has 36 directories (< API page limit).
        if len(rows) >= 1000:
            raise ValueError("Implement pagination before a larger catalog is used")
        mids = [r["path"].split("/")[-1] for r in rows if r["type"] == "directory"]

        def metadata(mid):
            for fn in ["gt_meeting_metadata.json", "devices.json"]:
                fetch(remote(mid, fn), root / "metadata" / mid / fn, 1_000_000)
            return load(root / "metadata" / mid / "gt_meeting_metadata.json")

        with ThreadPoolExecutor(max_workers=4) as pool:
            save(catalog, {"revision": REVISION, "meetings": list(pool.map(metadata, mids))})
    if load(catalog)["revision"] != REVISION:
        raise ValueError("Cached catalog revision differs")
    chosen = []
    for mid in MEETINGS:
        meta = load(root / "metadata" / mid / "gt_meeting_metadata.json")
        device = choose_device(load(root / "metadata" / mid / "devices.json"))
        filename = device["wav_file_names"]
        fetch(
            remote(mid, "gt_transcription.json"),
            root / "references" / mid / "gt_transcription.json",
            5_000_000,
        )
        audio = root / "recordings" / mid / filename
        fetch(remote(mid, filename), audio, 100_000_000)
        chosen.append(
            {
                "meeting_id": mid,
                "metadata": meta,
                "categories": categories(meta),
                "device": device,
                "device_model": "Logitech MeetUp"
                if device["device_name"].startswith("meetup")
                else None,
                "device_model_source": "https://www.chimechallenge.org/workshops/chime2024/papers/NOTSOFAR_alon_slides.pdf",
                "original_audio": str(audio.relative_to(root)),
                "device_reason": (
                    "Prefer meetup_0 single-channel distant recording consistently; "
                    "no close-talk or mixing"
                ),
                "original_audio_sha256": sha256(audio),
            }
        )
        print(mid, meta["Hashtags"], filename, audio.stat().st_size, flush=True)
    if sum(m["metadata"]["MeetingDurationSec"] for m in chosen) > 1800:
        raise ValueError("Selected duration exceeds 30 minutes")
    save(root / "selection.json", {"revision": REVISION, "meetings": chosen})
    print(
        "Selected audio MB:", sum((root / m["original_audio"]).stat().st_size for m in chosen) / 1e6
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=type(ROOT), default=ROOT)
    download(parser.parse_args().root)
