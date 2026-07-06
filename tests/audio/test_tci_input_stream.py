# SPDX-License-Identifier: GPL-3.0-or-later
"""TciInputStreamWorker — v0.4.1 audit medium #12 (stall watchdog) and
low #15 (dropped-chunk counter race)."""
from __future__ import annotations

import numpy as np
import pytest

from open_sstv.audio.tci_input_stream import _STALL_TIMEOUT_S, TciInputStreamWorker

pytestmark = pytest.mark.gui  # QObject signals need a QApplication


class FakeConnection:
    sample_rate = 48_000

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.callbacks: list = []
        self.subscribed: bool | None = None

    def register_audio_callback(self, fn) -> None:
        self.callbacks.append(fn)

    def unregister_audio_callback(self, fn) -> None:
        if fn in self.callbacks:
            self.callbacks.remove(fn)

    def send(self, cmd: str) -> None:
        self.sent.append(cmd)

    def mark_rx_audio_subscribed(self, flag: bool) -> None:
        self.subscribed = flag


class FakeRig:
    def __init__(self) -> None:
        self.connection = FakeConnection()


@pytest.fixture
def worker(qtbot) -> TciInputStreamWorker:
    w = TciInputStreamWorker(FakeRig())
    yield w
    if w.is_running:
        w.stop()


class TestStallWatchdog:
    def test_silence_past_timeout_emits_stream_error_once(self, qtbot, worker) -> None:
        errors: list[str] = []
        worker.stream_error.connect(errors.append)
        worker.start()
        # Backdate the last-chunk clock past the stall budget, then run
        # the drain handler directly (the timer would do the same).
        worker._last_chunk_monotonic -= _STALL_TIMEOUT_S + 1.0
        worker._drain_queue()
        worker._drain_queue()  # second tick must NOT re-emit
        assert len(errors) == 1
        assert "stalled" in errors[0]

    def test_flowing_audio_never_trips(self, qtbot, worker) -> None:
        errors: list[str] = []
        worker.stream_error.connect(errors.append)
        worker.start()
        worker._last_chunk_monotonic -= _STALL_TIMEOUT_S + 1.0
        # A chunk arrives before the tick — the drain refreshes the
        # clock and no error fires.
        worker._audio_callback(np.zeros(480, dtype=np.float32), 48_000)
        worker._drain_queue()
        assert errors == []

    def test_not_armed_before_start(self, qtbot, worker) -> None:
        errors: list[str] = []
        worker.stream_error.connect(errors.append)
        worker._drain_queue()  # not running — nothing should fire
        assert errors == []


class TestDropCounter:
    def test_overflow_counted_and_reported_at_stop(self, qtbot, worker) -> None:
        worker.start()
        chunk = np.zeros(16, dtype=np.float32)
        for _ in range(260):  # queue maxsize is 256 → 4 drops
            worker._audio_callback(chunk, 48_000)
        errors: list[str] = []
        worker.error.connect(errors.append)
        worker.stop()
        assert any("dropped 4 chunks" in e for e in errors)

    def test_counter_reset_between_sessions(self, qtbot, worker) -> None:
        worker.start()
        chunk = np.zeros(16, dtype=np.float32)
        for _ in range(260):
            worker._audio_callback(chunk, 48_000)
        worker.stop()
        errors: list[str] = []
        worker.error.connect(errors.append)
        worker.start()
        worker.stop()  # clean session — no stale overflow report
        assert errors == []
