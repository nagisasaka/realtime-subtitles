"""Default controller: one Windows microphone, Speechmatics STT, Luna text translation."""

import os
import queue
import threading
import time

from .audio import Microphone
from .final_history import FinalHistory
from .realtime_api import State
from .speechmatics_api import SpeechmaticsClient
from .text_translation import TranslationWorker


class LiveClient:
    def __init__(
        self,
        *,
        microphone_factory=Microphone,
        speechmatics_factory=SpeechmaticsClient,
        translation_factory=TranslationWorker,
        history=None,
    ):
        self.history = history if history is not None else FinalHistory()
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

    def _session_changed(self, identity):
        self.history.set_partial("")

    def _receive(self, event, words):
        sm = self.speechmatics
        if event.get("message") == "AddPartialTranscript":
            _, _, partials = sm.history.snapshot()
            pending = partials["en"]
            speaker = next(
                (w.speaker for w in pending if w.speaker not in {None, "", "UU", "SU"}), None
            )
            self.history.set_partial("".join(w.text for w in pending), speaker)
        elif event.get("message") == "AddTranscript":
            segment = self.history.add_final(event, sm.session_id)
            _, _, partials = sm.history.snapshot()
            self.history.set_partial("".join(w.text for w in partials["en"]))
            if segment is not None:
                self.translation.submit(segment)

    def stop(self):
        self.stop_requested.set()
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
            self.mic.start()
            while not self.stop_requested.is_set():
                self.mic.check_health()
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
            "backend": "speechmatics-stt + openai-text",
            "state": self.state.value,
            "error": self.error or sm.get("error", ""),
            "translation_error": self.translation.error if self.translation else "",
            "translation_model": "gpt-6-luna",
            "reasoning_effort": "none",
            "speechmatics": sm,
            "translation_status": counts,
            "translation_queue": self.translation.jobs.qsize() if self.translation else 0,
            "translations_in_flight": self.translation.in_flight if self.translation else 0,
            "frames_dispatched": self.frames_dispatched,
            **(self.mic.diagnostics() if self.mic else {}),
        }
