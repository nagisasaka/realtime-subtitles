"""Default controller: one Windows microphone, Speechmatics STT, Luna text translation."""

import os
import queue
import threading
import time

from .agent_stt import AgentSttClient
from .audio import AudioError, Microphone
from .audio_file import AudioFileSource
from .audio_monitor import AudioMonitor
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
        file_factory=AudioFileSource,
        monitor_factory=AudioMonitor,
    ):
        self.history = history if history is not None else TranslationHistory()
        self.recorder_factory = recorder_factory
        self.recorder = None
        self.recording_error = ""
        self.assembler = TranslationUnitAssembler(self._emit_unit, clock=self.history.clock)
        self.microphone_factory = microphone_factory
        self.file_factory = file_factory
        self.monitor_factory = monitor_factory
        self.monitor = None
        self.input_source = "microphone"
        self.playback_state = "Idle"
        self.speechmatics_factory = speechmatics_factory
        self.translation_factory = translation_factory
        self.reconstruction = None
        self.reconstruction_error = ""
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

    def start(self, device_index=None, *, audio_file=None, audio_monitor=False):
        if self.active:
            return False
        self.error = ""
        self.input_source = "audio_file" if audio_file is not None else "microphone"
        for key in ("SPEECHMATICS_API_KEY", "OPENAI_API_KEY"):
            if not os.environ.get(key, "").strip():
                self.state, self.error = State.ERROR, f"{key} が未設定です。"
                self.playback_state = "Error"
                return False
        self.stop_requested.clear()
        self.playback_state = "Connecting"
        self.history.clear_display()
        self.frames_dispatched = 0
        self.state = State.CONNECTING
        self.thread = threading.Thread(
            target=self._run,
            args=(device_index, audio_file, audio_monitor),
            name="live-subtitles",
            daemon=False,
        )
        self.thread.start()
        return True

    def _emit_unit(self, sources, reason, now_ms):
        unit = self.history.emit_unit(sources, reason, now_ms)
        if self.translation:
            self.translation.submit(unit)
            if self.reconstruction:
                try:
                    target = self.history.reconstructions.plan(unit)
                    if target:
                        self.reconstruction.submit(target)
                except Exception as exc:
                    self.reconstruction_error = type(exc).__name__
        else:
            self.history.update_translation(unit.sequence_id, "cancelled")

    def _session_changed(self, identity):
        self.assembler.flush("session_change_or_eos")
        self.history.set_partial("")
        if identity is not None:
            self.history.begin_session(identity, self._input_metadata())

    def _input_metadata(self):
        return (
            dict(self.mic.info)
            if self.input_source == "audio_file" and self.mic
            else {"input_source": "microphone"}
        )

    def _receive(self, event, words):
        sm = self.speechmatics
        if event.get("message") == "AddPartialSegment":
            segment = event.get("segment") or {}
            text = segment.get("transcript")
            if isinstance(text, str):
                if text.strip():
                    self.assembler.note_speaker(segment.get("speaker"), sm.session_id)
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
        if self.monitor:
            self.monitor.stop()
        self.assembler.flush("stop")
        if self.active:
            self.state = State.STOPPING
            self.playback_state = "Stopping / Draining"
            if self.speechmatics and not self.speechmatics.session_ready:
                self.speechmatics.stop()

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return not self.active

    def _dispatch(self):
        while True:
            if self.input_source == "audio_file" and (
                not self.speechmatics.session_ready or self.speechmatics.frames.full()
            ):
                break
            try:
                frame = self.mic.frames.get_nowait()
            except queue.Empty:
                break
            if self.speechmatics.session_ready:
                self.history.note_audio_frame(
                    self.speechmatics.session_id,
                    frame[0],
                    len(frame[1]) // 2,
                    healthy=not (
                        self.mic.frames.dropped
                        or self.speechmatics.frames.dropped
                        or getattr(getattr(self.mic, "raw", None), "dropped", 0)
                    ),
                )
                self.speechmatics.frames.put_latest(frame)
                if self.input_source == "audio_file":
                    self.mic.meter(frame[1])
                self.frames_dispatched += 1

    def _run(self, device, audio_file=None, audio_monitor=False):
        self.mic = None
        self.monitor = None
        self.recorder = None
        self.recording_error = ""
        self.translation = self.speechmatics = self.reconstruction = None
        self.reconstruction_error = ""
        failed = False
        eof = False
        capture_started = False
        try:
            self.mic = (
                self.file_factory(audio_file)
                if audio_file is not None
                else self.microphone_factory(device)
            )
            self.mic.prepare()
            self.translation = self.translation_factory(self.history, os.environ["OPENAI_API_KEY"])
            self.translation.start()
            try:
                from .history_reconstruction import make_reconstruction_worker

                self.reconstruction = make_reconstruction_worker(
                    self.history.reconstructions, os.environ["OPENAI_API_KEY"]
                )
                self.reconstruction.start()
            except Exception as exc:
                self.reconstruction_error = type(exc).__name__
                self.reconstruction = None
            self.speechmatics = self.speechmatics_factory(
                on_event=self._receive, on_session=self._session_changed
            )
            if audio_file is not None:
                self.speechmatics.file_input = True
                self.speechmatics.on_audio_sent = self.mic.note_sent
                if audio_monitor:
                    self.monitor = self.monitor_factory()
                    self.monitor.start()
                    self.speechmatics.on_audio_frame = self.monitor.submit
            if not self.speechmatics.start():
                raise RuntimeError("Speechmatics startup")
            while not self.stop_requested.is_set() and not self.speechmatics.session_ready:
                if not self.speechmatics.active:
                    raise RuntimeError("Speechmatics startup")
                self.state = State(self.speechmatics.state)
                self.stop_requested.wait(0.02)
            if self.stop_requested.is_set():
                return
            if self.recorder_factory and audio_file is None:
                try:
                    self.recorder = self.recorder_factory()
                    self.mic.frames.sinks = (*self.mic.frames.sinks, self.recorder)
                except Exception as exc:
                    self.recording_error = type(exc).__name__
            if self.monitor:
                until = time.monotonic() + 3
                while not self.monitor.ready.is_set() and not self.stop_requested.is_set():
                    if time.monotonic() >= until:
                        self.monitor.error = (
                            "音声出力の準備がタイムアウトしました。字幕は継続します。"
                        )
                        self.monitor.stop()
                        break
                    self.stop_requested.wait(0.02)
            if self.stop_requested.is_set():
                return
            self.mic.start()
            capture_started = True
            while not self.stop_requested.is_set():
                self.mic.check_health()
                self._dispatch()
                if not self.speechmatics.active:
                    raise RuntimeError("Speechmatics stopped")
                self.state = State(self.speechmatics.state)
                self.playback_state = (
                    "Playing" if self.state == State.RUNNING else self.state.value.title()
                )
                if audio_file is not None and self.mic.finished and self.mic.frames.empty():
                    eof = True
                    break
                self.stop_requested.wait(0.01)
        except Exception as exc:
            failed = True
            self.error = (
                self.speechmatics.error
                if self.speechmatics and self.speechmatics.error
                else str(exc)
                if isinstance(exc, AudioError)
                else type(exc).__name__
            )
        finally:
            self.state = State.STOPPING
            self.playback_state = "Stopping / Draining"
            if not eof:
                self.assembler.flush("stopping")
            try:
                if self.mic:
                    self.mic.stop()
            except Exception as exc:
                failed, self.error = True, type(exc).__name__
            if self.speechmatics:
                self._dispatch()
                until = time.monotonic() + (5 if audio_file is not None else 0.8)
                while (
                    self.speechmatics.session_ready
                    and (not self.speechmatics.frames.empty() or not self.mic.frames.empty())
                    and time.monotonic() < until
                ):
                    self._dispatch()
                    time.sleep(0.02)
                self.speechmatics.stop()
                self.speechmatics.join()
                if (
                    audio_file is not None
                    and capture_started
                    and (self.speechmatics.error or not self.speechmatics.eos_received)
                ):
                    failed = True
                    self.error = self.speechmatics.error or "正常なEOSを確認できませんでした。"
            if self.monitor:
                self.monitor.close(drain=eof and not self.stop_requested.is_set())
            if self.recorder:
                try:
                    self.recorder.close()
                except Exception as exc:
                    self.recording_error = type(exc).__name__
            self.assembler.flush("eos")
            # EOS may emit additional finals; only now stop accepting translation jobs.
            if self.translation:
                self.translation.finish()
            if self.reconstruction:
                self.reconstruction.finish()
            if self.translation:
                self.translation.join()
            if self.reconstruction:
                self.reconstruction.join()
            self.history.set_partial("")
            self.state = State.ERROR if failed else State.STOPPED
            self.playback_state = "Error" if failed else "Finished" if eof else "Idle"
            self.history.record_input_end(self.playback_state)

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
            "audio_monitor": self.monitor.snapshot() if self.monitor else {},
            "input_source": self.input_source,
            "playback_state": self.playback_state,
            "error": self.error or sm.get("error", ""),
            "translation_error": self.translation.error if self.translation else "",
            "translation_model": "gpt-6-luna",
            "reasoning_effort": "none",
            "speechmatics": sm,
            "translation_status": counts,
            "assembler": self.assembler.snapshot(),
            "history_reconstruction": {
                "status": self.history.reconstructions.statistics(),
                "queue": self.reconstruction.jobs.qsize() if self.reconstruction else 0,
                "in_flight": self.reconstruction.in_flight if self.reconstruction else 0,
                "error": self.reconstruction_error
                or (self.reconstruction.error if self.reconstruction else ""),
            },
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
