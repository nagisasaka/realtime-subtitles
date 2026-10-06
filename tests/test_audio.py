import numpy as np
import pytest

from realtime_subtitles.audio import AudioConverter, LatestQueue, mono_float, pcm16


def test_stereo_downmix():
    np.testing.assert_allclose(mono_float([[0.8, 0.2], [-1, 1]]), [0.5, 0])


def test_pcm_clipping_and_little_endian():
    result = pcm16(np.array([-2, -1, 0, 0.5, 1, 2, np.nan, np.inf]))
    assert np.frombuffer(result, dtype="<i2").tolist() == [
        -32768,
        -32768,
        0,
        16384,
        32767,
        32767,
        0,
        32767,
    ]
    assert result[6:8] == b"\x00\x40"


@pytest.mark.parametrize("rate", [24000, 44100, 48000, 96000])
def test_streaming_resampling_and_200ms_frames(rate):
    t = np.arange(rate, dtype=np.float32) / rate
    signal = 0.5 * np.sin(2 * np.pi * 1000 * t)
    converter = AudioConverter(rate)
    chunks = []
    for block in np.array_split(signal, 37):
        chunks.extend(converter.feed(block))
    chunks.extend(converter.finish())
    assert len(chunks) == 5
    assert all(len(chunk) == 9600 for chunk in chunks)
    output = np.frombuffer(b"".join(chunks), dtype="<i2").astype(float) / 32768
    expected = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)
    assert np.max(np.abs(output[100:-100] - expected[100:-100])) < 0.003


def test_chunk_boundaries_and_silence():
    converter = AudioConverter(24000)
    assert converter.feed(np.zeros(4799)) == []
    assert converter.feed(np.zeros(1)) == [b"\0" * 9600]
    assert converter.feed(np.zeros(2)) == []
    assert converter.finish() == [b"\0" * 9600]


def test_bounded_queue_discards_oldest():
    queue = LatestQueue(2)
    for i in range(5):
        queue.put_latest(i)
    assert queue.qsize() == 2
    assert queue.dropped == 3
    assert [queue.get_nowait(), queue.get_nowait()] == [3, 4]


def test_one_second_speech_pause_does_not_drop_or_change_audio():
    from realtime_subtitles.audio import SpeechPause

    detector = SpeechPause()
    silence = bytes(9600)
    speech = pcm16(np.full(4800, 0.1))
    for _ in range(10):
        assert not detector.feed(silence)
    assert not detector.feed(speech)  # No leading blank paragraph.
    for _ in range(4):
        assert not detector.feed(silence)
    assert not detector.feed(speech)  # 800 ms isn't a paragraph.
    for _ in range(5):
        assert not detector.feed(silence)
    assert detector.feed(speech)
    assert not detector.feed(speech)
    assert len(speech) == len(silence) == 9600


def test_pcm_fanout_queues_are_independent_and_bit_identical():
    primary, english = LatestQueue(3), LatestQueue(3)
    primary.sinks = (english,)
    frames = [bytes([i]) * 9600 for i in range(8)]
    for frame in frames:
        primary.put_latest((0, frame))
        assert english.get_nowait()[1] is frame
    assert primary.dropped == 5
    assert english.dropped == 0
    assert [primary.get_nowait()[1] for _ in range(3)] == frames[-3:]
