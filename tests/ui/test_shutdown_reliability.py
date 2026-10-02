# SPDX-License-Identifier: GPL-3.0-or-later
"""Shutdown fixes from the 2026-10 stability audit.

* TX is stopped first.  A local transmission used to be stopped only after
  the remote-server stop, the connect abort and the offline-worker drain,
  so quitting mid-TX could keep PTT keyed for about 30 s.
* No terminate().  A worker that won't stop is detached and the app then
  leaves with os._exit().  Measured with a thread busy in numpy,
  terminate() never stopped it (the process aborted with exit 134, or hung
  on the GIL), and detaching alone still aborted at interpreter shutdown.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import QObject, QThread, Slot

from open_sstv.config.schema import AppConfig
from open_sstv.radio.base import ManualRig
from open_sstv.ui import main_window as mw
from open_sstv.ui.main_window import MainWindow


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "open_sstv.ui.workers.encode",
        MagicMock(return_value=np.zeros(100, dtype=np.int16)),
    )
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", MagicMock())
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    yield


@pytest.fixture
def window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio) -> MainWindow:
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    return w


def test_local_tx_is_stopped_before_the_slow_teardown_steps(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_stop = window._tx_worker.request_stop
    monkeypatch.setattr(
        window._tx_worker, "request_stop",
        lambda: (order.append("tx_stop"), real_stop())[1],
    )
    for step in ("_stop_remote_server", "_abort_connect", "_abort_offline_workers"):
        real = getattr(window, step)
        monkeypatch.setattr(
            window, step, (lambda name, fn: lambda: (order.append(name), fn())[1])(step, real)
        )

    window.close()

    assert order, "no teardown steps recorded"
    assert order[0] == "tx_stop", (
        f"TX must be stopped before any slow teardown step; order was {order}"
    )


class _BlockingWorker(QObject):
    """Stands in for an offline encode/decode that won't finish in time."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release

    @Slot()
    def run(self) -> None:
        self._release.wait(30)


def test_stuck_offline_worker_is_detached_not_terminated(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    thread = QThread(window)
    worker = _BlockingWorker(release)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    terminated: list[bool] = []
    monkeypatch.setattr(thread, "terminate", lambda: terminated.append(True))
    detached = getattr(mw, "_DETACHED_AT_SHUTDOWN", [])
    before = len(detached)
    # Everything from thread.start() on sits inside the try.  If any line
    # fails, the finally must still release and stop the thread.  A QThread
    # still running when the window fixture is destroyed is a qFatal that
    # aborts the whole test process, not a failed test.  An earlier version
    # of this test did exactly that when run against the old code.
    try:
        thread.start()
        window._offline_decode_thread = thread
        window._offline_decode_worker = worker
        t0 = time.monotonic()
        window._abort_offline_workers()
        elapsed = time.monotonic() - t0

        assert terminated == [], "terminate() must not be used"
        assert len(mw._DETACHED_AT_SHUTDOWN) == before + 1
        assert mw._DETACHED_AT_SHUTDOWN[-1] == (thread, worker)
        assert thread.parent() is None, "detached thread must leave the window"
        assert elapsed < 5.0, f"drain blocked for {elapsed:.1f} s"
        assert window._offline_decode_thread is None
    finally:
        release.set()
        thread.quit()
        thread.wait(5000)
        del detached[before:]


def test_exit_is_immediate_only_when_a_thread_was_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_sstv import app

    exits: list[int] = []
    monkeypatch.setattr("os._exit", lambda rc: exits.append(rc))
    monkeypatch.setattr("logging.shutdown", lambda: None)

    monkeypatch.setattr(mw, "_DETACHED_AT_SHUTDOWN", [])
    app._exit_now_if_threads_were_detached(0)
    assert exits == [], "a normal shutdown must return normally"

    monkeypatch.setattr(mw, "_DETACHED_AT_SHUTDOWN", [(object(), object())])
    app._exit_now_if_threads_were_detached(3)
    assert exits == [3], "a detached thread must force os._exit with the same code"
