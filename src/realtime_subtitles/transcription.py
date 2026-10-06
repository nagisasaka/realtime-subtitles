"""Independent English connection: bounded audio queue, its own retry lifecycle."""

import asyncio
import base64
import contextlib
import json
import queue
import time

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from .audio import LatestQueue, SpeechPause

TRANSCRIPTION_ENDPOINT = "wss://api.openai.com/v1/realtime?intent=transcription"


def transcription_update(noise_reduction="far_field"):
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {
                        "model": "gpt-live-transcribe",
                        "languages": ["en"],
                        "delay": "low",
                    },
                    "noise_reduction": {"type": noise_reduction},
                    "turn_detection": None,
                }
            },
        },
    }


class TranscriptionLink:
    def __init__(
        self,
        on_delta,
        *,
        endpoint=TRANSCRIPTION_ENDPOINT,
        journal=None,
        noise_reduction="far_field",
        retry_base=0.5,
        max_retries=8,
    ):
        self.on_delta = on_delta
        self.endpoint = endpoint
        self.journal = journal
        self.noise_reduction = noise_reduction
        self.retry_base = retry_base
        self.max_retries = max_retries
        self.frames = LatestQueue(3)
        self.state = "CONNECTING"
        self.error = ""
        self.session_id = None
        self.stop_requested = asyncio.Event()
        self.sent_frames = 0
        self.commits = 0
        self.final_mismatches = 0
        self._items = {}
        self._samples_in_turn = 0
        self._pending_commits = 0

    def snapshot(self):
        return {
            "english_connection": self.state,
            "english_error": self.error,
            "english_session_id": self.session_id,
            "english_queue": self.frames.qsize(),
            "english_dropped": self.frames.dropped,
            "english_sent_frames": self.sent_frames,
            "english_commits": self.commits,
            "english_final_mismatches": self.final_mismatches,
        }

    def _receive_event(self, raw):
        if self.journal:
            self.journal.record(
                raw, session_id=self.session_id, model="gpt-live-transcribe", stream="english"
            )
        event = json.loads(raw)
        kind = event.get("type", "")
        if kind in {"session.created", "transcription_session.created"}:
            self.session_id = event.get("session", {}).get("id")
        if kind == "error":
            # Error codes are sufficient for diagnosis; never include request/header content.
            raise ValueError(f"Transcription API: {event.get('error', {}).get('code', 'unknown')}")
        item = event.get("item_id")
        if kind == "conversation.item.input_audio_transcription.delta":
            delta = event.get("delta", "")
            self._items[item] = self._items.get(item, "") + delta
            if delta:
                self.on_delta(
                    delta, event.get("elapsed_ms"), self.session_id, event.get("event_id")
                )
        elif kind == "conversation.item.input_audio_transcription.completed":
            self._pending_commits = max(0, self._pending_commits - 1)
            partial = self._items.pop(item, "")
            final = event.get("transcript", "")
            if final.startswith(partial):
                remainder = final[len(partial) :]
                if remainder:
                    self.on_delta(
                        remainder, event.get("elapsed_ms"), self.session_id, event.get("event_id")
                    )
            elif final.strip() != partial.strip():
                self.final_mismatches += 1  # Full authoritative completion remains in raw journal.
        elif kind == "conversation.item.input_audio_transcription.failed":
            raise ValueError(
                f"Transcription item failed: {event.get('error', {}).get('code', 'unknown')}"
            )
        return kind

    async def _receive(self, ws):
        async for raw in ws:
            self._receive_event(raw)
        raise ConnectionError("English socket closed")

    async def _send(self, ws):
        detector = SpeechPause()
        self._samples_in_turn = 0
        while not self.stop_requested.is_set() or not self.frames.empty():
            try:
                captured_at, frame = self.frames.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if time.monotonic() - captured_at > 0.8:
                self.frames.dropped += 1
                continue
            quiet_before = detector.quiet_samples
            detector.feed(frame)
            async with asyncio.timeout(2):
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(frame).decode("ascii"),
                        }
                    )
                )
                self.sent_frames += 1
                self._samples_in_turn += len(frame) // 2
                ended = (
                    detector.heard_speech
                    and quiet_before < detector.minimum <= detector.quiet_samples
                )
                # Periodic commits also bound continuous speech items without interrupting audio.
                if ended or self._samples_in_turn >= 20 * 24000:
                    await self._commit(ws)

    async def _commit(self, ws):
        if self._samples_in_turn:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            self._samples_in_turn = 0
            self.commits += 1
            self._pending_commits += 1

    async def run(self, key):
        attempts = 0
        while not self.stop_requested.is_set():
            self.state = "CONNECTING" if attempts == 0 else "RECONNECTING"
            tasks = []
            try:
                async with connect(
                    self.endpoint,
                    additional_headers={"Authorization": f"Bearer {key}"},
                    open_timeout=10,
                    close_timeout=1,
                    ping_interval=20,
                    ping_timeout=10,
                    max_queue=16,
                    compression=None,
                    proxy=None,
                ) as ws:
                    self._items.clear()
                    self._pending_commits = 0
                    async with asyncio.timeout(10):
                        while self._receive_event(await ws.recv()) not in {
                            "session.created",
                            "transcription_session.created",
                        }:
                            pass
                        await ws.send(json.dumps(transcription_update(self.noise_reduction)))
                        while self._receive_event(await ws.recv()) not in {
                            "session.updated",
                            "transcription_session.updated",
                        }:
                            pass
                    self.state, self.error = "RUNNING", ""
                    receive = asyncio.create_task(self._receive(ws))
                    send = asyncio.create_task(self._send(ws))
                    tasks = [receive, send]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                    if self.stop_requested.is_set():
                        await self._commit(ws)
                        # Keep receiver alive briefly for the final committed item.
                        async with asyncio.timeout(3):
                            while self._items or self._pending_commits:
                                await asyncio.sleep(0.05)
                        return
            except asyncio.CancelledError:
                raise
            except (ValueError, InvalidStatus) as exc:
                self.error = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else f"English WebSocket HTTP {exc.response.status_code}"
                )
                self.state = "ERROR"
                return  # A bad EN configuration must never stop Japanese translation.
            except (OSError, TimeoutError, ConnectionError) as exc:
                self.error = f"English connection: {type(exc).__name__}"
            except Exception as exc:
                self.error = f"English connection: {type(exc).__name__}"
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.stop_requested.is_set():
                    self.state = "STOPPED"
            if self.stop_requested.is_set():
                break
            if attempts >= self.max_retries:
                self.state = "ERROR"
                return
            attempts += 1
            self.state = "RECONNECTING"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.stop_requested.wait(), min(8, self.retry_base * 2 ** (attempts - 1))
                )
        self.state = "STOPPED"
