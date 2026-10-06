"""A background asyncio loop owns WebSocket send/receive and session lifecycle."""

import asyncio
import base64
import contextlib
import json
import os
import queue
import re
import threading
import time
import uuid
from enum import StrEnum

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .audio import AudioError, Microphone, SpeechPause
from .diarization import DIARIZATION_ENABLED, DiarizationSidecar
from .event_log import EventJournal
from .subtitle_buffer import TranscriptHistory
from .transcription import TRANSCRIPTION_ENDPOINT, TranscriptionLink

ENDPOINT = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"


class State(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


TRANSITIONS = {
    State.STOPPED: {State.CONNECTING},
    State.CONNECTING: {State.RUNNING, State.RECONNECTING, State.STOPPING, State.ERROR},
    State.RUNNING: {State.RECONNECTING, State.STOPPING, State.ERROR},
    State.RECONNECTING: {State.RUNNING, State.STOPPING, State.ERROR},
    State.STOPPING: {State.STOPPED, State.ERROR},
    State.ERROR: {State.CONNECTING, State.STOPPING},
}


def session_update(noise_reduction="far_field", source_model="gpt-live-transcribe"):
    if noise_reduction not in {"far_field", "near_field"}:
        raise ValueError("Unknown noise reduction mode")
    # The translation CLIENT schema has transcription.model, but no language field.
    # Source language is auto-detected; the application is intended for English input.
    return {
        "type": "session.update",
        "session": {
            "audio": {
                "input": {
                    "transcription": {"model": source_model} if source_model else None,
                    "noise_reduction": {"type": noise_reduction},
                },
                "output": {"language": "ja"},
            }
        },
    }


def safe_error(error, key=""):
    message = str(error)
    if key:
        message = message.replace(key, "[REDACTED]")
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"(?i)authorization[^\r\n]*", "[REDACTED HEADER]", message)
    return message[:700]


class APIError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class TranscriptHealth:
    """Report source-only delays; never restart a healthy translation session."""

    def __init__(self):
        self.reset_session()

    def reset_session(self):
        self.last = {"en": None, "ja": None}
        self.counts = {"en": 0, "ja": 0}
        self.first_target = None

    def received(self, language, now):
        if language == "ja" and self.first_target is None:
            self.first_target = now
        self.last[language] = now
        self.counts[language] += 1

    def delayed(self, now, threshold=10.0):
        baseline = self.last["en"] if self.last["en"] is not None else self.first_target
        return (
            baseline is not None
            and now - baseline >= threshold
            and self.last["ja"] is not None
            and now - self.last["ja"] < 5
        )

    def snapshot(self, now):
        return {
            "transcript_events": dict(self.counts),
            "transcript_age_seconds": {
                lang: round(now - stamp, 1) if stamp is not None else None
                for lang, stamp in self.last.items()
            },
            "source_delayed": self.delayed(now),
        }


class RealtimeClient:
    def __init__(
        self,
        history=None,
        *,
        endpoint=ENDPOINT,
        microphone_factory=Microphone,
        retry_base=0.5,
        retry_cap=8.0,
        max_retries=8,
        close_timeout=10.0,
        source_model="gpt-live-transcribe",
        event_log=None,
        source_mode="separate",
        transcription_endpoint=TRANSCRIPTION_ENDPOINT,
        diarization_enabled=None,
    ):
        if source_mode not in {"sidecar", "separate"}:
            raise ValueError("Unknown source transcription mode")
        self.history = history if history is not None else TranscriptHistory()
        self._diarization = DiarizationSidecar(
            self.history,
            enabled=DIARIZATION_ENABLED if diarization_enabled is None else diarization_enabled,
        )
        self.endpoint = endpoint
        self.microphone_factory = microphone_factory
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self.max_retries = max_retries
        self.close_timeout = close_timeout
        self.source_model = source_model
        self.source_mode = source_mode
        self.transcription_endpoint = transcription_endpoint
        self._english = None
        self.event_log = event_log
        self._journal = None
        self._lock = threading.RLock()
        self._state = State.STOPPED
        self._error = ""
        self._ws_state = "closed"
        self._thread = None
        self._loop = None
        self._stop_async = None
        self._stop_requested = threading.Event()
        self._microphone = None
        self._last_diagnostics = {}
        self._session_id = None
        self._key = ""
        self._transcripts = TranscriptHealth()
        self._pause_detector = SpeechPause()
        self._paragraph_pending = {"en": False, "ja": False}

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self):
        with self._lock:
            return self._state

    def _transition(self, state):
        with self._lock:
            if state == self._state:
                return
            if state not in TRANSITIONS[self._state]:
                raise ValueError(f"Invalid transition: {self._state} -> {state}")
            self._state = state

    def _set_error(self, message):
        with self._lock:
            self._error = safe_error(message, self._key)

    def snapshot(self):
        with self._lock:
            mic = self._microphone
            return {
                "state": self._state.value,
                "error": self._error,
                "websocket": self._ws_state,
                "session_id": self._session_id,
                "source_model": self.source_model,
                "source_mode": self.source_mode,
                **(self._english.snapshot() if self._english else {}),
                **self._diarization.snapshot(),
                "raw_event_log": str(self.event_log) if self.event_log else None,
                "raw_event_log_dropped": self._journal.dropped if self._journal else 0,
                "raw_event_log_error": self._journal.error if self._journal else "",
                **self._transcripts.snapshot(time.monotonic()),
                **(mic.diagnostics() if mic else self._last_diagnostics),
            }

    def start(self, device_index=None, noise_reduction="far_field"):
        with self._lock:
            if self.active or self._state not in {State.STOPPED, State.ERROR}:
                return False
            self._key = os.environ.get("OPENAI_API_KEY", "").strip()
            self._error = ""
            self._last_diagnostics = {}
            self._session_id = None
            self._transcripts = TranscriptHealth()
            self._stop_requested.clear()
            self._transition(State.CONNECTING)
            if not self._key:
                self._set_error("OPENAI_API_KEY が設定されていません。環境変数を設定してください。")
                self._transition(State.ERROR)
                return False
            self._thread = threading.Thread(
                target=self._thread_main,
                args=(device_index, noise_reduction),
                name="realtime-network",
                daemon=False,
            )
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            if self._state in {State.STOPPED, State.STOPPING}:
                return
            self._stop_requested.set()
            self._transition(State.STOPPING)
            if not self.active:
                self._transition(State.STOPPED)
            elif self._loop is not None and self._stop_async is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._stop_async.set)

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.active

    def _thread_main(self, device_index, noise_reduction):
        try:
            self._journal = EventJournal(self.event_log) if self.event_log else None
            asyncio.run(self._run(device_index, noise_reduction))
        except Exception as exc:
            self._set_error(f"{type(exc).__name__}: {exc}")
            with self._lock:
                if self._state != State.ERROR:
                    self._transition(State.ERROR)
        finally:
            if self._journal:
                self._journal.close()
            with self._lock:
                self._loop = None
                self._stop_async = None
                self._ws_state = "closed"
                self._key = ""
                if self._state == State.STOPPING:
                    self._transition(State.STOPPED)

    async def _run(self, device_index, noise_reduction):
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._stop_async = asyncio.Event()
            if self._stop_requested.is_set():
                self._stop_async.set()
        stop_task = asyncio.create_task(self._stop_async.wait())
        retries = 0
        try:
            while not self._stop_async.is_set():
                started = time.monotonic()
                session_task = asyncio.create_task(self._one_session(device_index, noise_reduction))
                await asyncio.wait([session_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
                if stop_task.done():
                    session_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await session_task
                    break
                try:
                    await session_task
                    raise ConnectionError("翻訳セッションが終了しました。再接続します。")
                except (AudioError, ValueError) as exc:
                    raise APIError(str(exc)) from None
                except InvalidStatus as exc:
                    code = exc.response.status_code
                    message = f"WebSocket HTTP {code}: APIキー、モデル利用権限、利用上限を確認。"
                    if code not in {408, 429} and code < 500:
                        raise APIError(message) from None
                    self._set_error(message)
                except APIError as exc:
                    if not exc.retryable:
                        raise
                    self._set_error(exc)
                except (ConnectionClosed, ConnectionError, OSError, TimeoutError) as exc:
                    self._set_error(f"接続が切れました ({type(exc).__name__})。再接続します。")
                # A long healthy session resets the consecutive failure budget.
                if time.monotonic() - started > 30:
                    retries = 0
                if retries >= self.max_retries:
                    raise APIError("再接続の上限に達しました。接続を確認してStartしてください。")
                with self._lock:
                    if self._stop_requested.is_set():
                        break
                    self._transition(State.RECONNECTING)
                delay = min(self.retry_cap, self.retry_base * 2**retries)
                retries += 1
                try:
                    await asyncio.wait_for(self._stop_async.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task

    async def _one_session(self, device_index, noise_reduction):
        mic = self.microphone_factory(device_index)
        tasks = []
        ws = None
        prepared = False
        english_task = None
        try:
            # Native device operations happen off both the Tk and network threads.
            # Shield setup so cancellation cannot leave an opening device orphaned.
            await self._device_call(mic.prepare)
            prepared = True
            self._microphone = mic
            self._ws_state = "connecting"
            ws = await connect(
                self.endpoint,
                additional_headers={"Authorization": f"Bearer {self._key}"},
                open_timeout=10,
                close_timeout=1,
                ping_interval=20,
                ping_timeout=10,
                max_queue=16,
                max_size=2**20,
                write_limit=16_384,
                compression=None,
                proxy=None,
            )
            self._ws_state = "open / configuring"
            self._session_id = uuid.uuid4().hex
            with self._lock:
                self._transcripts.reset_session()
            self._pause_detector = SpeechPause()
            self._paragraph_pending = {"en": False, "ja": False}
            await self._wait_event(ws, "session.created")
            await ws.send(
                json.dumps(
                    session_update(
                        noise_reduction,
                        self.source_model if self.source_mode == "sidecar" else None,
                    )
                )
            )
            await self._wait_event(ws, "session.updated")
            if self._stop_requested.is_set():
                return
            if self.source_mode == "separate":
                self._english = TranscriptionLink(
                    lambda delta, elapsed, session, event: self._accept_transcript(
                        "en", delta, elapsed, session, event
                    ),
                    endpoint=self.transcription_endpoint,
                    journal=self._journal,
                    noise_reduction=noise_reduction,
                    retry_base=self.retry_base,
                    max_retries=self.max_retries,
                )
                mic.frames.sinks = (self._english.frames,)
                english_task = asyncio.create_task(self._english.run(self._key))
            self._diarization.attach(mic, self._key, self._session_id)
            await self._device_call(mic.start)
            with self._lock:
                if self._stop_requested.is_set():
                    return
                self._transition(State.RUNNING)
                self._error = ""
                self._ws_state = "open / streaming"
            tasks = [
                asyncio.create_task(self._send_audio(ws, mic)),
                asyncio.create_task(self._receive(ws)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            self._diarization.detach()
            if prepared:
                try:
                    await self._device_call(mic.stop)
                except Exception as exc:
                    self._set_error(f"音声停止エラー: {exc}")
                finally:
                    self._last_diagnostics = mic.diagnostics()
                    self._microphone = None
            if english_task:
                self._english.stop_requested.set()
                try:
                    await asyncio.wait_for(asyncio.shield(english_task), 4)
                except (TimeoutError, asyncio.CancelledError):
                    english_task.cancel()
                    await asyncio.gather(english_task, return_exceptions=True)
                mic.frames.sinks = ()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if ws is not None:
                self._ws_state = "closing"
                try:
                    if self._stop_requested.is_set():
                        async with asyncio.timeout(self.close_timeout):
                            # Send the final padded frame before requesting a server flush.
                            while not mic.frames.empty():
                                _, frame = mic.frames.get_nowait()
                                await self._append_audio(ws, frame)
                            await ws.send(json.dumps({"type": "session.close"}))
                            await self._wait_event(ws, "session.closed", timeout=None)
                except (TimeoutError, ConnectionClosed, APIError):
                    if self._stop_requested.is_set():
                        self._set_error(
                            "終了応答を受け取れませんでした。末尾の字幕が未確定の可能性があります。"
                        )
                finally:
                    await ws.close()
            if prepared:
                self._last_diagnostics = mic.diagnostics()
            self._ws_state = "closed"

    @staticmethod
    async def _device_call(function):
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _wait_event(self, ws, expected, timeout=10):
        async with asyncio.timeout(timeout):
            while True:
                event = self._decode_event(await ws.recv())
                self._handle_event(event)
                if event.get("type") == expected:
                    return
                if event.get("type") == "session.closed":
                    raise ConnectionError("Session closed before configuration")

    def _decode_event(self, message):
        if self._journal:
            self._journal.record(message, session_id=self._session_id, model=self.source_model)
        return json.loads(message)

    def _handle_event(self, event):
        kind = event.get("type")
        if kind == "error":
            error = event.get("error", {})
            code = error.get("code", "unknown")
            raise APIError(
                f"API {code}: {error.get('message', 'Unknown error')}",
                retryable=code in {"server_error", "rate_limit_exceeded"},
            )
        if kind == "session.created":
            self._session_id = event.get("session", {}).get("id", self._session_id)
        languages = {
            "session.input_transcript.delta": "en",
            "session.output_transcript.delta": "ja",
        }
        if kind in languages:
            language = languages[kind]
            if language == "en" and self.source_mode == "separate":
                return
            self._accept_transcript(
                language,
                event.get("delta", ""),
                event.get("elapsed_ms"),
                self._session_id,
                event.get("event_id"),
            )
        # session.output_audio.delta is deliberately ignored: no playback or storage.

    def _accept_transcript(self, language, delta, elapsed_ms, session_id, event_id):
        if delta:
            with self._lock:
                self._transcripts.received(language, time.monotonic())
            if self._paragraph_pending[language]:
                self.history.paragraph(language, elapsed_ms, session_id)
                self._paragraph_pending[language] = False
        self.history.append(language, delta, elapsed_ms, session_id, event_id)

    async def _receive(self, ws):
        async for message in ws:
            event = self._decode_event(message)
            self._handle_event(event)
            if event.get("type") == "session.closed":
                return

    async def _append_audio(self, ws, frame):
        await ws.send(
            json.dumps(
                {
                    "type": "session.input_audio_buffer.append",
                    "audio": base64.b64encode(frame).decode("ascii"),
                }
            )
        )
        if self._pause_detector.feed(frame):
            self._paragraph_pending = {"en": True, "ja": True}
        self._diarization.note_sent(frame)

    async def _send_audio(self, ws, mic):
        while True:
            mic.check_health()
            try:
                captured_at, frame = mic.frames.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if time.monotonic() - captured_at > 0.8:
                mic.frames.dropped += 1
                continue
            # A stalled TCP send triggers reconnect; queue bounds alone cannot bound TCP lag.
            async with asyncio.timeout(2):
                await self._append_audio(ws, frame)
