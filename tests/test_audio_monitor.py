import threading
import time

import numpy as np

from realtime_subtitles.audio_monitor import AudioMonitor


class Output:
    def __init__(self):
        self.data = []
        self.stopped = self.closed = False

    def write(self, pcm):
        self.data.append(pcm)

    def stop(self):
        self.stopped = True

    def abort(self):
        pass

    def close(self):
        self.closed = True


def test_resample_native_stereo_and_eof_drain():
    output = Output()
    monitor = AudioMonitor(output_factory=lambda: (output, 48000, 2))
    monitor.start()
    pcm = (np.sin(np.arange(4800) * 2 * np.pi * 440 / 24000) * 10000).astype("<i2").tobytes()
    monitor.submit(pcm)
    monitor.close(drain=True)
    samples = np.frombuffer(b"".join(output.data), dtype="<i2").reshape(-1, 2)
    assert samples.shape == (9600, 2)
    assert np.array_equal(samples[:, 0], samples[:, 1])
    assert output.stopped and output.closed
    assert monitor.played_samples == 4800 and not monitor.error


def test_failure_is_isolated_and_error_omits_exception_details():
    def unavailable():
        raise RuntimeError("private device details")

    monitor = AudioMonitor(output_factory=unavailable)
    monitor.start()
    monitor.thread.join(1)
    monitor.submit(bytes(9600))
    monitor.close()
    assert "RuntimeError" in monitor.error and "private" not in monitor.error
    assert monitor.frames.empty()


def test_bounded_queue_does_not_block_sender_and_stop_discards_pending():
    entered, release = threading.Event(), threading.Event()
    output = Output()

    def open_late():
        entered.set()
        assert release.wait(1)
        return output, 24000, 1

    monitor = AudioMonitor(output_factory=open_late)
    monitor.start()
    assert entered.wait(1)
    start = time.monotonic()
    for _ in range(100):
        monitor.submit(bytes(9600))
    assert time.monotonic() - start < 0.1
    assert monitor.frames.qsize() == 3 and monitor.frames.dropped == 97
    monitor.stop()
    release.set()
    monitor.close()
    assert not output.data and output.closed


def test_restart_uses_fresh_output_and_no_old_audio():
    outputs = []
    for value in (11, 22):
        output = Output()
        outputs.append(output)
        monitor = AudioMonitor(output_factory=lambda output=output: (output, 24000, 1))
        monitor.start()
        monitor.submit(np.full(4800, value, dtype="<i2").tobytes())
        monitor.close(drain=True)
        assert set(np.frombuffer(b"".join(output.data), dtype="<i2")) == {value}
    assert all(x.closed for x in outputs)
