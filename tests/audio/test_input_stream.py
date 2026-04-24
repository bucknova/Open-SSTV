# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.audio.input_stream``.

We never open a real PortAudio input stream — that would need a physical
microphone on every CI runner and flake constantly. Instead we patch
``sd.InputStream`` with a mock, exercise the worker's start/stop/overflow
paths, and invoke the audio callback directly to simulate PortAudio
delivering frames.

These tests are marked ``gui`` because ``InputStreamWorker`` is a
``QObject`` and needs a ``QApplication`` — the ``qapp`` fixture supplied
by pytest-qt handles that.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import sounddevice as sd

from open_sstv.audio.devices import AudioDevice
from open_sstv.audio.input_stream import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_SAMPLE_RATE,
    InputStreamWorker,
)

pytestmark = pytest.mark.gui


class _FakeCallbackFlags:
    """Stand-in for ``sd.CallbackFlags`` with tweakable boolean attrs."""

    def __init__(
        self, overflow: bool = False, underflow: bool = False
    ) -> None:
        self.input_overflow = overflow
        self.input_underflow = underflow


class _FakeStream:
    """Minimal stand-in for ``sd.InputStream``.

    Records how it was constructed, what was called on it, and stashes
    the real callback so tests can invoke it directly (simulating a
    PortAudio frame delivery) without spinning up a real audio thread.
    """

    last_instance: "_FakeStream | None" = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped_count = 0
        self.closed_count = 0
        self.callback = kwargs["callback"]
        self.finished_callback = kwargs.get("finished_callback")
        _FakeStream.last_instance = self

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped_count += 1

    def close(self) -> None:
        self.closed_count += 1

    def deliver(self, samples: np.ndarray, overflow: bool = False) -> None:
        """Pretend PortAudio just handed us a block of frames."""
        indata = samples.reshape(-1, 1).astype(np.float32)
        flags = _FakeCallbackFlags(overflow=overflow)
        self.callback(indata, indata.shape[0], None, flags)

    def finish(self) -> None:
        """Simulate PortAudio calling the finished_callback (e.g. on device loss)."""
        if self.finished_callback is not None:
            self.finished_callback()


@pytest.fixture
def fake_stream_cls(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[type[_FakeStream]]:
    _FakeStream.last_instance = None
    monkeypatch.setattr(
        "open_sstv.audio.input_stream.sd.InputStream", _FakeStream
    )
    yield _FakeStream


def _record_signals(worker: InputStreamWorker) -> dict[str, list]:
    log: dict[str, list] = {
        "chunk_ready": [],
        "started": [],
        "stopped": [],
        "error": [],
    }
    worker.chunk_ready.connect(lambda chunk: log["chunk_ready"].append(chunk))
    worker.started.connect(lambda: log["started"].append(True))
    worker.stopped.connect(lambda: log["stopped"].append(True))
    worker.error.connect(lambda msg: log["error"].append(msg))
    return log


# === start / construction ===


def test_start_opens_stream_with_default_params(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    worker = InputStreamWorker()
    log = _record_signals(worker)

    worker.start()

    assert fake_stream_cls.last_instance is not None
    stream = fake_stream_cls.last_instance
    assert stream.kwargs["samplerate"] == DEFAULT_SAMPLE_RATE
    assert stream.kwargs["blocksize"] == DEFAULT_BLOCKSIZE
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "float32"
    assert stream.kwargs["device"] is None
    assert stream.started is True
    assert worker.is_running is True
    assert log["started"] == [True]
    assert log["error"] == []

    worker.stop()


def test_start_resolves_audio_device_to_index(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    device = AudioDevice(
        index=5,
        name="Fake Mic",
        host_api="Test",
        max_input_channels=1,
        max_output_channels=0,
        default_sample_rate=48000.0,
    )
    worker = InputStreamWorker()
    worker.start(device=device)

    assert fake_stream_cls.last_instance.kwargs["device"] == 5

    worker.stop()


def test_start_passes_int_device_through(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    worker = InputStreamWorker()
    worker.start(device=3)

    assert fake_stream_cls.last_instance.kwargs["device"] == 3

    worker.stop()


def test_start_twice_emits_error_and_keeps_original_stream(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    worker = InputStreamWorker()
    log = _record_signals(worker)

    worker.start()
    first_stream = fake_stream_cls.last_instance
    worker.start()  # second call should be rejected

    # ``last_instance`` would only change if a second InputStream was
    # constructed — the error path doesn't touch sd.InputStream.
    assert fake_stream_cls.last_instance is first_stream
    assert any("already running" in msg for msg in log["error"])

    worker.stop()


def test_start_stream_construction_failure_emits_error(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("no such device")

    monkeypatch.setattr("open_sstv.audio.input_stream.sd.InputStream", boom)
    worker = InputStreamWorker()
    log = _record_signals(worker)

    worker.start()

    assert log["started"] == []
    assert log["error"] and "no such device" in log["error"][0]
    assert worker.is_running is False


def test_start_failure_emits_stopped_to_re_enable_button(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When start() fails (e.g. stale device index after replug), the UI must
    be able to re-enable the Start button.  The only signal that triggers
    ``set_capturing(False)`` on the RxPanel is ``stopped``, so we verify it
    is emitted even when the stream never opened."""

    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("device gone after replug")

    monkeypatch.setattr("open_sstv.audio.input_stream.sd.InputStream", boom)
    worker = InputStreamWorker()
    log = _record_signals(worker)

    worker.start()

    assert log["stopped"] == [True], (
        "stopped must be emitted on start failure so the UI can re-enable"
        " the Start button"
    )
    assert log["started"] == []
    assert worker.is_running is False


# === callback / drain ===


def test_callback_delivery_emits_chunk_ready_after_drain(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()
    stream = fake_stream_cls.last_instance

    # Deliver two distinct chunks, then drain.
    chunk_a = np.arange(64, dtype=np.float32) * 0.01
    chunk_b = np.arange(64, dtype=np.float32) * 0.02
    stream.deliver(chunk_a)
    stream.deliver(chunk_b)
    worker._drain_queue()

    assert len(log["chunk_ready"]) == 2
    np.testing.assert_array_equal(log["chunk_ready"][0], chunk_a)
    np.testing.assert_array_equal(log["chunk_ready"][1], chunk_b)
    # Delivered chunks are float32 mono 1-D.
    for chunk in log["chunk_ready"]:
        assert chunk.ndim == 1
        assert chunk.dtype == np.float32

    worker.stop()


def test_callback_copies_buffer_so_portaudio_reuse_is_safe(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """PortAudio reuses the indata buffer across callbacks. If the worker
    didn't copy, mutating the source would scribble over the queued chunk."""
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()
    stream = fake_stream_cls.last_instance

    buf = np.ones(32, dtype=np.float32)
    stream.deliver(buf)
    buf[:] = -7.0  # simulate PortAudio filling the same buffer with new data
    worker._drain_queue()

    assert len(log["chunk_ready"]) == 1
    np.testing.assert_array_equal(
        log["chunk_ready"][0], np.ones(32, dtype=np.float32)
    )

    worker.stop()


def test_queue_overflow_drops_and_reports_on_stop(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the consumer stalls and the queue fills up, the callback
    drops the newest chunk and increments a counter. ``stop`` surfaces
    the count via the ``error`` signal."""
    # Shrink the queue to make overflow cheap to exercise.
    monkeypatch.setattr(
        "open_sstv.audio.input_stream._QUEUE_MAXSIZE", 2
    )
    worker = InputStreamWorker()
    log = _record_signals(worker)
    # Rebuild the queue at the new size (worker was constructed before
    # the patch took effect).
    import queue as _queue

    worker._queue = _queue.Queue(maxsize=2)
    worker.start()
    stream = fake_stream_cls.last_instance

    # Fill the queue then overflow by two chunks.
    buf = np.zeros(16, dtype=np.float32)
    stream.deliver(buf)
    stream.deliver(buf)
    stream.deliver(buf)  # dropped
    stream.deliver(buf)  # dropped

    worker.stop()

    assert any("dropped 2" in msg for msg in log["error"]), log["error"]


def test_portaudio_overflow_flag_is_recorded(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """An ``input_overflow`` flag from PortAudio itself (not our queue)
    should also feed the drop counter so users learn about dropped
    samples upstream of us."""
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()
    stream = fake_stream_cls.last_instance

    stream.deliver(np.zeros(16, dtype=np.float32), overflow=True)
    worker.stop()

    assert any("dropped" in msg for msg in log["error"]), log["error"]


# === stop / lifecycle ===


def test_stop_without_start_is_noop(qapp) -> None:
    worker = InputStreamWorker()
    log = _record_signals(worker)

    worker.stop()

    assert log["stopped"] == []
    assert log["error"] == []


def test_stop_closes_stream_and_emits_stopped(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()
    stream = fake_stream_cls.last_instance

    worker.stop()

    assert stream.stopped_count == 1
    assert stream.closed_count == 1
    assert worker.is_running is False
    assert log["stopped"] == [True]


def test_stop_flushes_pending_chunks_before_emitting_stopped(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """Any chunks still in the queue at stop time should land as
    ``chunk_ready`` before ``stopped`` fires, so a late Stop click
    doesn't discard the tail of an in-flight image."""
    worker = InputStreamWorker()
    events: list[str] = []
    worker.chunk_ready.connect(lambda _c: events.append("chunk"))
    worker.stopped.connect(lambda: events.append("stopped"))

    worker.start()
    stream = fake_stream_cls.last_instance
    stream.deliver(np.zeros(16, dtype=np.float32))
    stream.deliver(np.zeros(16, dtype=np.float32))

    worker.stop()

    assert events == ["chunk", "chunk", "stopped"]


def test_stop_survives_stream_close_errors(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """If sd.InputStream.close raises, the worker must still clear its
    state and emit ``stopped`` — leaving is_running True would wedge
    the next start call."""
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()
    stream = fake_stream_cls.last_instance

    def failing_close() -> None:
        raise RuntimeError("close botched")

    stream.close = failing_close  # type: ignore[method-assign]

    worker.stop()

    assert worker.is_running is False
    assert log["stopped"] == [True]
    assert any("close botched" in msg for msg in log["error"])


# === device-loss (finished_callback / hot-unplug) ===


def test_stream_passes_finished_callback_to_portaudio(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """sd.InputStream must receive a finished_callback kwarg so PortAudio
    can notify us immediately when the device is yanked."""
    worker = InputStreamWorker()
    worker.start()
    stream = fake_stream_cls.last_instance

    assert stream.finished_callback is not None, (
        "InputStreamWorker must pass finished_callback= to sd.InputStream "
        "so hot-unplug can be detected immediately"
    )
    worker.stop()


def test_pa_finished_callback_noop_during_deliberate_stop(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """When _stopping is True, _on_pa_stream_finished must not emit
    stream_error — the finish was deliberate and the UI is already
    handling cleanup."""
    worker = InputStreamWorker()
    stream_errors: list[str] = []
    worker.stream_error.connect(stream_errors.append)
    worker.start()

    # Manually set _stopping so _on_pa_stream_finished thinks stop() is running.
    worker._stopping = True
    fake_stream_cls.last_instance.finish()

    assert stream_errors == [], (
        "_on_pa_stream_finished must be a no-op when _stopping is True"
    )

    worker._stopping = False
    worker.stop()


def test_pa_finished_callback_emits_stream_error_on_device_loss(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """Simulating an unexpected PA stream finish (device yanked) must emit
    stream_error with a clear recovery message."""
    worker = InputStreamWorker()
    stream_errors: list[str] = []
    worker.stream_error.connect(stream_errors.append)
    worker.start()

    # Simulate PortAudio calling finished_callback unexpectedly.
    fake_stream_cls.last_instance.finish()

    assert len(stream_errors) == 1
    assert "replug" in stream_errors[0].lower() or "disconnect" in stream_errors[0].lower(), (
        "stream_error message should guide the user to recover"
    )

    # Clean up — stop() was scheduled via invokeMethod; process events.
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()


def test_pa_finished_callback_schedules_stop_on_device_loss(
    qapp, fake_stream_cls: type[_FakeStream]
) -> None:
    """After an unexpected PA finish, stop() must run so the stream is
    torn down and ``stopped`` is emitted (which re-enables the Start button)."""
    worker = InputStreamWorker()
    log = _record_signals(worker)
    worker.start()

    fake_stream_cls.last_instance.finish()

    # invokeMethod queues stop() onto the event loop — process it.
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()

    assert worker.is_running is False, (
        "stream must be torn down after unexpected PA finished callback"
    )
    assert log["stopped"] == [True], (
        "stopped must be emitted so the UI can re-enable the Start button"
    )


# === PortAudio terminate/initialize reset ===


def test_pa_reset_called_by_stop_after_device_loss(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _device_lost is True, stop() must leave the flag set so the
    subsequent start() calls _pa_reset() immediately before opening the new
    stream.  This avoids the race where the OS reassigns USB device indices
    between stop() and the user clicking Start again."""
    terminate_calls: list[str] = []
    initialize_calls: list[str] = []
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate",
                        lambda: terminate_calls.append("terminate"))
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize",
                        lambda: initialize_calls.append("initialize"))

    worker = InputStreamWorker()
    worker.start()
    worker._device_lost = True  # simulate device-loss flag set by watchdog/PA

    worker.stop()

    # stop() must NOT reset PortAudio — leave the flag so start() handles it.
    assert terminate_calls == [], "stop() must not call _pa_reset(); start() must do it"
    assert initialize_calls == [], "stop() must not call _pa_reset(); start() must do it"
    assert worker._device_lost is True, "flag must remain set so start() sees it"

    # start() performs the reset right before opening the new stream.
    worker.start()
    assert terminate_calls == ["terminate"], "_terminate must be called by start() when _device_lost"
    assert initialize_calls == ["initialize"], "_initialize must be called by start() when _device_lost"
    assert worker._device_lost is False, "flag must be cleared after reset in start()"

    worker.stop()


def test_pa_reset_not_called_on_normal_stop(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-initiated stop (no device loss) must NOT call _terminate/_initialize
    — resetting PortAudio unnecessarily adds latency and could disrupt other streams."""
    terminate_calls: list[str] = []
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate",
                        lambda: terminate_calls.append("terminate"))
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize",
                        lambda: terminate_calls.append("initialize"))

    worker = InputStreamWorker()
    worker.start()
    # _device_lost is False by default — simulate normal user stop
    worker.stop()

    assert terminate_calls == [], "PA reset must not run on a normal stop"


def test_pa_reset_safety_net_in_start(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() must call _pa_reset() when _device_lost is True (the primary
    recovery path — reset happens at the last possible moment before the new
    stream opens, after the OS has stabilised device indices)."""
    terminate_calls: list[str] = []
    initialize_calls: list[str] = []
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate",
                        lambda: terminate_calls.append("terminate"))
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize",
                        lambda: initialize_calls.append("initialize"))

    worker = InputStreamWorker()
    worker._device_lost = True  # simulate left-over flag

    worker.start()

    assert terminate_calls == ["terminate"], "safety-net reset must run in start() when flag is set"
    assert initialize_calls == ["initialize"]
    assert worker._device_lost is False, "flag must be cleared after safety-net reset"

    worker.stop()


def test_pa_reset_survives_terminate_exception(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If sd._terminate() raises, _pa_reset must catch it and still call
    sd._initialize() — a partial reset is better than no reset."""
    initialize_calls: list[str] = []
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate",
                        lambda: (_ for _ in ()).throw(RuntimeError("PA internal error")))
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize",
                        lambda: initialize_calls.append("initialize"))

    worker = InputStreamWorker()
    worker._pa_reset()  # must not raise

    assert initialize_calls == ["initialize"], "_initialize must still run after _terminate raises"


def test_pa_reset_survives_initialize_exception(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If sd._initialize() raises, _pa_reset must catch it and not propagate —
    the caller (stop/start) must not be interrupted."""
    terminate_calls: list[str] = []
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate",
                        lambda: terminate_calls.append("terminate"))
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize",
                        lambda: (_ for _ in ()).throw(RuntimeError("init failed")))

    worker = InputStreamWorker()
    worker._pa_reset()  # must not raise

    assert terminate_calls == ["terminate"]


def test_watchdog_sets_device_lost_flag(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_on_watchdog_timeout must set _device_lost; stop() must leave the flag
    set so start() can call _pa_reset() at the right moment."""
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate", lambda: None)
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize", lambda: None)

    worker = InputStreamWorker()
    worker.start()

    assert worker._device_lost is False
    worker._on_watchdog_timeout()

    # stop() leaves _device_lost set; start() will clear it after _pa_reset().
    assert worker._device_lost is True, "flag must remain set for start() to call _pa_reset()"
    assert worker.is_running is False


def test_pa_finished_callback_sets_device_lost_flag(
    qapp, fake_stream_cls: type[_FakeStream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_on_pa_stream_finished must set _device_lost so the queued stop()
    knows to run the PA reset."""
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._terminate", lambda: None)
    monkeypatch.setattr("open_sstv.audio.input_stream.sd._initialize", lambda: None)

    worker = InputStreamWorker()
    worker.start()

    fake_stream_cls.last_instance.finish()  # triggers _on_pa_stream_finished

    # _device_lost is set synchronously in _on_pa_stream_finished.
    assert worker._device_lost is True, "_device_lost must be set before stop() runs"

    # Now drain the event loop so the queued stop() runs.
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()

    # stop() leaves the flag set; start() will clear it after _pa_reset().
    assert worker._device_lost is True
    assert worker.is_running is False
