"""Paired real-time replay; no microphone, LLM, or Speechmatics Translation.

Input: 24 kHz mono PCM16 WAV (the application's automatic recording format).
Raw server JSON remains local in the ignored diagnostics directory.
"""

import argparse
import hashlib
import json
import statistics
import time
import wave
from pathlib import Path

from realtime_subtitles.agent_stt import AgentSttClient
from realtime_subtitles.speechmatics_api import SpeechmaticsClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, default=Path("diagnostics/agent-ab"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logs, clients, received = {}, {}, {"enhanced": [], "agent": []}
    started = 0
    try:
        for name, factory in (("enhanced", SpeechmaticsClient), ("agent", AgentSttClient)):
            logs[name] = (args.output / f"{name}-raw.jsonl").open("x", encoding="utf-8")

            def receive(event, words, name=name):
                logs[name].write(json.dumps(event, ensure_ascii=False) + "\n")
                logs[name].flush()
                if event.get("message") in {"AddSegment", "AddTranscript"}:
                    received[name].append((time.monotonic() - started, event))

            clients[name] = factory(on_event=receive)
            clients[name].start()
        deadline = time.monotonic() + 25
        while not all(c.session_ready for c in clients.values()):
            if time.monotonic() > deadline or any(not c.active for c in clients.values()):
                raise RuntimeError("A/B handshake failed; inspect status.json")
            time.sleep(0.02)
        sessions = {name: c.session_id for name, c in clients.items()}
        started = time.monotonic()
        samples = 0
        with wave.open(str(args.audio), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (
                24000,
                1,
                2,
            ):
                raise ValueError("Input must be 24 kHz mono PCM16 WAV")
            duration = source.getnframes() / 24000
            while pcm := source.readframes(4800):
                time.sleep(max(0, started + samples / 24000 - time.monotonic()))
                for name, client in clients.items():
                    if not client.session_ready or client.session_id != sessions[name]:
                        raise RuntimeError("A/B connection interrupted; comparison incomplete")
                    client.frames.put_latest((time.monotonic(), pcm))
                samples += len(pcm) // 2
            time.sleep(0.5)  # hand final queued frame to each SDK before EOS
        for client in clients.values():
            client.stop()
        for client in clients.values():
            if not client.join(15):
                raise RuntimeError("A/B shutdown incomplete")
        report = {
            "audio_sha256": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
            "duration_seconds": duration,
            "note": "No human reference: these metrics are segmentation/latency, not accuracy/WER.",
            "backends": {},
        }
        for name, entries in received.items():
            kind = "AddTranscript" if name == "enhanced" else "AddSegment"
            entries = [(t, e) for t, e in entries if e["message"] == kind]
            texts = [
                e["metadata"]["transcript"] if name == "enhanced" else e["segment"]["transcript"]
                for _, e in entries
            ]
            full = "".join(texts) if name == "enhanced" else " ".join(t.strip() for t in texts)
            (args.output / f"{name}.txt").write_text(full, encoding="utf-8")
            word_lengths = [len(t.split()) for t in texts]
            latencies = [t - e["metadata"]["end_time"] for t, e in entries]
            report["backends"][name] = {
                "finals": len(texts),
                "words": sum(word_lengths),
                "median_words_per_final": statistics.median(word_lengths) if texts else None,
                "one_or_two_word_finals": sum(n <= 2 for n in word_lengths),
                "median_final_delay_seconds": statistics.median(latencies) if latencies else None,
                "status": clients[name].snapshot(),
            }
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        for client in clients.values():
            client.stop()
        for client in clients.values():
            client.join(15)
        (args.output / "status.json").write_text(
            json.dumps({name: c.snapshot() for name, c in clients.items()}, indent=2),
            encoding="utf-8",
        )
        for log in logs.values():
            log.close()


if __name__ == "__main__":
    main()
