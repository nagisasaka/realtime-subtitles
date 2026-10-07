"""Only mono PCM16 sample-rate conversion; never enhance/normalize/remove audio."""

import argparse
import shutil
import wave

import numpy as np
import soxr

from .common import MAX_SECONDS, ROOT, load, save, sha256


def wav_info(path):
    with wave.open(str(path), "rb") as audio:
        return {
            "sample_rate": audio.getframerate(),
            "channels": audio.getnchannels(),
            "sample_width": audio.getsampwidth(),
            "frames": audio.getnframes(),
            "duration": audio.getnframes() / audio.getframerate(),
        }


def convert(source, destination):
    before = wav_info(source)
    if before["channels"] != 1 or before["sample_width"] != 2:
        raise ValueError("Require single-channel PCM16 original; no implicit mixing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if before["sample_rate"] == 16000:
        shutil.copyfile(source, destination)
    else:
        with wave.open(str(source), "rb") as audio:
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        pcm = soxr.resample(samples, before["sample_rate"], 16000, quality="HQ").astype("<i2")
        with wave.open(str(destination), "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            out.writeframes(pcm.tobytes())
    after = wav_info(destination)
    if abs(after["duration"] - before["duration"]) > 1 / 16000:
        raise ValueError("Duration changed")
    return before, after


def prepare(root=ROOT):
    selection = load(root / "selection.json")
    for m in selection["meetings"]:
        src = root / m["original_audio"]
        if sha256(src) != m["original_audio_sha256"]:
            raise ValueError("Original checksum changed")
        dst = root / "prepared" / (m["meeting_id"] + ".wav")
        before, after = convert(src, dst)
        m.update(
            original_format=before,
            prepared_format=after,
            prepared_audio=str(dst.relative_to(root)),
            prepared_audio_sha256=sha256(dst),
            gt_transcription_sha256=sha256(
                root / "references" / m["meeting_id"] / "gt_transcription.json"
            ),
            meeting_metadata_sha256=sha256(
                root / "metadata" / m["meeting_id"] / "gt_meeting_metadata.json"
            ),
            devices_sha256=sha256(root / "metadata" / m["meeting_id"] / "devices.json"),
        )
    total = sum(m["prepared_format"]["duration"] for m in selection["meetings"])
    if total > MAX_SECONDS:
        raise ValueError("Audio duration exceeds limit")
    selection["total_seconds"] = total
    save(root / "prepared.json", selection)
    print("Prepared", len(selection["meetings"]), "meetings, seconds:", total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=type(ROOT), default=ROOT)
    prepare(parser.parse_args().root)
