# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.4.1 stability-audit regression tests (highs #1, #2, #7).

Covers the fixes that live in main_window plumbing: deferred rig
teardown while TX unwinds, rig leaks on cancelled connects, and the
rigctld stderr drain.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.radio.base import ManualRig
from open_sstv.ui.main_window import (
    MainWindow,
    _drain_subprocess_stderr,
    _RigConnectWorker,
)

pytestmark = pytest.mark.gui


class FakeRig:
    """Recording rig double for teardown/connect tests."""

    name = "fake"

    def __init__(self, *, ping_side_effect=None) -> None:
        self.calls: list[str] = []
        self._ping_side_effect = ping_side_effect

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def ping(self) -> None:
        self.calls.append("ping")
        if self._ping_side_effect is not None:
            self._ping_side_effect()

    def get_freq(self) -> int:
        return 14_230_000

    def get_mode(self):
        return ("USB", 2400)

    def get_strength(self) -> int:
        return -73

    def set_ptt(self, on: bool) -> None:
        self.calls.append(f"ptt:{on}")

    def set_freq(self, hz: int) -> None:
        pass

    def set_mode(self, mode: str, passband_hz: int) -> None:
        pass


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "open_sstv.ui.workers.encode",
        MagicMock(return_value=np.zeros(100, dtype=np.int16)),
    )
    monkeypatch.setattr(
        "open_sstv.ui.workers.output_stream.play_blocking", MagicMock()
    )
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    yield


def _make_window(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> MainWindow:
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    return w


class TestDeferredRigTeardown:
    """Audit high #1: an involuntary disconnect mid-TX must not close
    the backend the TX worker's unkey retries are using."""

    def test_teardown_deferred_while_tx_unwinding(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        fake = FakeRig()
        window._rig = fake  # type: ignore[assignment]
        # Simulate a TX in flight: the worker's idle flag is clear.
        window._tx_worker._idle_event.clear()
        try:
            window._on_radio_disconnected()
            assert "close" not in fake.calls, (
                "backend must not be closed while the unkey path owns it"
            )
            assert window._deferred_rig_teardown is fake
        finally:
            window._tx_worker._idle_event.set()
        # TX completion path finishes the deferred teardown.
        window._unlock_rig_controls()
        assert "close" in fake.calls
        assert window._deferred_rig_teardown is None

    def test_teardown_immediate_when_idle(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        fake = FakeRig()
        window._rig = fake  # type: ignore[assignment]
        window._on_radio_disconnected()
        assert "close" in fake.calls
        assert window._deferred_rig_teardown is None

    def test_finish_is_idempotent(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._finish_deferred_rig_teardown()  # nothing deferred — no-op
        assert window._deferred_rig_teardown is None


class TestConnectCancelClosesRig:
    """Audit high #7: a cancelled/timed-out connect must close the rig
    it opened — a leaked exclusive COM handle blocks reconnects until
    app restart."""

    def test_cancel_after_open_closes(self, qtbot) -> None:
        cancel = threading.Event()
        cancel.set()  # cancel already won the race before run()
        fake = FakeRig()
        worker = _RigConnectWorker(fake, cancel)  # type: ignore[arg-type]
        results: list[object] = []
        worker.succeeded.connect(results.append)
        worker.run()
        assert fake.calls == ["open", "close"]
        assert results == []

    def test_cancel_after_ping_closes(self, qtbot) -> None:
        cancel = threading.Event()
        fake = FakeRig(ping_side_effect=cancel.set)  # timeout lands mid-ping
        worker = _RigConnectWorker(fake, cancel)  # type: ignore[arg-type]
        results: list[object] = []
        worker.succeeded.connect(results.append)
        worker.run()
        assert fake.calls == ["open", "ping", "close"]
        assert results == []

    def test_uncancelled_connect_does_not_close(self, qtbot) -> None:
        cancel = threading.Event()
        fake = FakeRig()
        worker = _RigConnectWorker(fake, cancel)  # type: ignore[arg-type]
        results: list[object] = []
        worker.succeeded.connect(results.append)
        worker.run()
        assert fake.calls == ["open", "ping"]
        assert results == [fake]


class TestStderrDrain:
    """Audit high #2: a child writing far past the OS pipe buffer must
    never wedge — the drain thread keeps the pipe empty."""

    def test_chatty_child_completes_and_lines_reach_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        # ~200 KB of stderr — triple the typical 64 KB pipe buffer.
        # Without a drain this child blocks on write(2) forever.
        code = (
            "import sys\n"
            "for i in range(2000):\n"
            "    sys.stderr.write(f'hamlib chatter line {i:04d} ' + 'x'*80 + '\\n')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with caplog.at_level(logging.INFO, logger="open_sstv.ui.main_window"):
            _drain_subprocess_stderr(proc, "fakectl")
            assert proc.wait(timeout=10) == 0, "child wedged — pipe not drained"
        # Give the pump a beat to flush the tail.
        import time

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any("chatter line 1999" in r.message for r in caplog.records):
                break
            time.sleep(0.05)
        assert any("fakectl: hamlib chatter line 0000" in r.message for r in caplog.records)
        assert any("chatter line 1999" in r.message for r in caplog.records)

    def test_no_stderr_pipe_is_a_noop(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _drain_subprocess_stderr(proc, "fakectl")  # must not raise
        assert proc.wait(timeout=5) == 0
