"""Default controller: one Windows microphone, Speechmatics STT, Luna text translation."""

import os
import queue
import threading
import time

from .agent_stt import AgentSttClient
from .audio import Microphone
from .audio_recording import AudioRecorder
from .realtime_api import State
from .text_translation import TranslationWorker
from .translation_assembler import TranslationUnitAssembler
from .translation_history import TranslationHistory


class LiveClient:
    def __init__(
        self,
        *,
        microphone_factory=Microphone,
        speechmatics_factory=AgentSttClient,
        translation_factory=TranslationWorker,
        history=None,
        recorder_factory=AudioRecorder,
    ):
        self.history = history if history is not None else TranslationHistory()
        self.recorder_factory = recorder_factory
        self.recorder = None
        self.recording_error = ""
        self.assembler = TranslationUnitAssembler(self._emit_unit, clock=self.history.clock)
        self.microphone_factory = microphone_factory
        self.speechmatics_factory = speechmatics_factory
        self.translation_factory = translation_factory
        self.state = State.STOPPED
        self.error = ""
        self.mic = self.speechmatics = self.translation = None
        self.thread = None
        self.stop_requested = threading.Event()
        self.frames_dispatched = 0
        self._retry_lock = threading.Lock()

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, device_index=None, noise_reduction=None):
        if self.active:
            return False
        self.error = ""
        for key in ("SPEECHMATICS_API_KEY", "OPENAI_API_KEY"):
            if not os.environ.get(key, "").strip():
                self.state, self.error = State.ERROR, f"{key} が未設定です。"
                return False
        self.stop_requested.clear()
        self.state = State.CONNECTING
        self.thread = threading.Thread(
            target=self._run, args=(device_index,), name="live-subtitles", daemon=False
        )
        self.thread.start()
        return True

    def _emit_unit(self, sources, reason, now_ms):
        unit = self.history.emit_unit(sources, reason, now_ms)
        if self.translation:
            self.translation.submit(unit)
        else:
            self.history.update_translation(unit.sequence_id, "cancelled")

    def _session_changed(self, identity):
        self.assembler.flush("session_change_or_eos")
        self.history.set_partial("")

    def _receive(self, event, words):
        sm = self.speechmatics
        if event.get("message") == "AddPartialSegment":
            segment = event.get("segment") or {}
            text = segment.get("transcript")
            if isinstance(text, str):
                self.history.set_partial(text, segment.get("speaker"))
        elif event.get("message") == "AddSegment":
            source = self.history.record_segment(event, sm.session_id)
            if source is not None:
                self.history.set_partial("")
                self.assembler.accept(source)
                if self.stop_requested.is_set():
                    self.assembler.flush("stopping")
        elif event.get("message") == "AddTranscript":
            self.history.record_word_metadata(event, sm.session_id)

    def stop(self):
        self.stop_requested.set()
        self.assembler.flush("stop")
        if self.active:
            self.state = State.STOPPING
            if self.speechmatics and not self.speechmatics.session_ready:
                self.speechmatics.stop()

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return not self.active

    def _dispatch(self):
        while True:
            try:
                frame = self.mic.frames.get_nowait()
            except queue.Empty:
                break
            if self.speechmatics.session_ready:
                self.speechmatics.frames.put_latest(frame)
                self.frames_dispatched += 1

    def _run(self, device):
        self.mic = None
        self.recorder = None
        self.recording_error = ""
        self.translation = self.speechmatics = None
        failed = False
        try:
            self.mic = self.microphone_factory(device)
            self.mic.prepare()
            self.translation = self.translation_factory(self.history, os.environ["OPENAI_API_KEY"])
            self.translation.start()
            self.speechmatics = self.speechmatics_factory(
                on_event=self._receive, on_session=self._session_changed
            )
            if not self.speechmatics.start():
                raise RuntimeError("Speechmatics startup")
            while not self.stop_requested.is_set() and not self.speechmatics.session_ready:
                if not self.speechmatics.active:
                    raise RuntimeError("Speechmatics startup")
                self.state = State(self.speechmatics.state)
                self.stop_requested.wait(0.02)
            if self.stop_requested.is_set():
                return
            if self.recorder_factory:
                try:
                    self.recorder = self.recorder_factory()
                    self.mic.frames.sinks = (*self.mic.frames.sinks, self.recorder)
                except Exception as exc:
                    self.recording_error = type(exc).__name__
            self.mic.start()
            while not self.stop_requested.is_set():
                self.mic.check_health()
                self.assembler.tick()
                self._dispatch()
                if not self.speechmatics.active:
                    raise RuntimeError("Speechmatics stopped")
                self.state = State(self.speechmatics.state)
                self.stop_requested.wait(0.01)
        except Exception as exc:
            failed = True
            self.error = (
                self.speechmatics.error
                if self.speechmatics and self.speechmatics.error
                else type(exc).__name__
            )
        finally:
            self.state = State.STOPPING
            self.assembler.flush("stopping")
            try:
                if self.mic:
                    self.mic.stop()
            except Exception as exc:
                failed, self.error = True, type(exc).__name__
            if self.speechmatics:
                self._dispatch()
                until = time.monotonic() + 0.8
                while (
                    self.speechmatics.session_ready
                    and not self.speechmatics.frames.empty()
                    and time.monotonic() < until
                ):
                    time.sleep(0.02)
                self.speechmatics.stop()
                self.speechmatics.join()
            if self.recorder:
                try:
                    self.recorder.close()
                except Exception as exc:
                    self.recording_error = type(exc).__name__
            self.assembler.flush("eos")
            # EOS may emit additional finals; only now stop accepting translation jobs.
            if self.translation:
                self.translation.finish()
                self.translation.join()
            self.history.set_partial("")
            self.state = State.ERROR if failed else State.STOPPED

    def retry_translations(self):
        """Queue only failed/skipped/cancelled slots; never retranslate completed finals."""
        if not self.translation or not self.translation.accepting:
            return
        with self._retry_lock:
            self.translation.error = ""
            for segment in self.history.segments():
                if segment.translation_status in {"failed", "skipped", "cancelled"}:
                    self.history.update_translation(segment.sequence_id, "pending")
                    if not self.translation.submit(segment):
                        break

    def snapshot(self):
        sm = self.speechmatics.snapshot() if self.speechmatics else {}
        counts = self.history.statistics()
        return {
            "backend": "speechmatics-agent-stt + openai-text",
            "state": self.state.value,
            "error": self.error or sm.get("error", ""),
            "translation_error": self.translation.error if self.translation else "",
            "translation_model": "gpt-6-luna",
            "reasoning_effort": "none",
            "speechmatics": sm,
            "translation_status": counts,
            "assembler": self.assembler.snapshot(),
            "translation_queue": self.translation.jobs.qsize() if self.translation else 0,
            "translations_in_flight": self.translation.in_flight if self.translation else 0,
            "frames_dispatched": self.frames_dispatched,
            "recording": (
                {**self.recorder.snapshot(), "error": self.recording_error or self.recorder.error}
                if self.recorder
                else {"error": self.recording_error}
            ),
            **(self.mic.diagnostics() if self.mic else {}),
        }
