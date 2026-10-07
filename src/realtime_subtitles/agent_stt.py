"""Current Agent STT SDK: server segmentation, no Voice SDK or local VAD."""

import logging

import numpy as np
import soxr

from .audio import OUTPUT_RATE
from .speechmatics_api import SpeechmaticsClient

ENDPOINT = "wss://global.rt.speechmatics.com/v2/agent"
AGENT_RATE = 16_000


class AgentSttClient(SpeechmaticsClient):
    events = ("AddPartialSegment", "AddSegment", "AddTranscript")
    model = "linden-1"
    output_rate = AGENT_RATE

    def __init__(self, *, endpoint=ENDPOINT, **kwargs):
        super().__init__(endpoint=endpoint, **kwargs)
        self.resampler = None

    def _new_sdk(self, key):
        from speechmatics.agent_stt import AgentSttAsyncClient, Model, TranscriptionConfig

        logging.getLogger("speechmatics.agent_stt").setLevel(logging.CRITICAL)
        self.resampler = soxr.ResampleStream(
            OUTPUT_RATE, AGENT_RATE, 1, dtype="int16", quality="HQ"
        )
        return AgentSttAsyncClient(
            api_key=key,
            url=self.endpoint,
            record_events=False,
            transcription_config=TranscriptionConfig(
                model=Model.LINDEN_1,
                language="en",
                enable_partials=True,
                diarization="speaker",
                emit_sentences=True,
            ),
        )

    async def _connect_sdk(self, client):
        await client.connect()

    def _accept(self, event):
        # Raw word events are forwarded separately; never parse them as translation units.
        return ()

    def _encode_audio(self, frame):
        return (
            self.resampler.resample_chunk(np.frombuffer(frame, dtype="<i2")).astype("<i2").tobytes()
        )

    def _flush_audio(self):
        return (
            self.resampler.resample_chunk(np.empty(0, dtype="int16"), last=True)
            .astype("<i2")
            .tobytes()
        )

    async def _send_audio(self, client, payload):
        await super()._send_audio(client, payload)
        # Agent SDK suppresses TransportError and closes its audio gate. Detect it here
        # so our existing reconnect policy can recover instead of silently dropping audio.
        if not client.is_ready_for_audio:
            raise ConnectionError("Agent STT audio gate closed")

    def snapshot(self):
        result = super().snapshot()
        result.pop("max_delay", None)
        result["emit_sentences"] = True
        return result
