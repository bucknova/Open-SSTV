# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.6.14 audit regression tests for MainWindow's rig / TX plumbing.

T-M1, T-L1, T-L3, UI-H2, UI-M1, UI-M7 and the low-severity rig-UI items.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import QThread

from open_sstv.config.schema import AppConfig
from open_sstv.radio.base import ManualRig
from open_sstv.ui.main_window import MainWindow, _RigPollWorker
from tests.ui.test_stability_fixes import FakeRig

pytestmark = pytest.mark.gui


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "open_sstv.ui.workers.encode", MagicMock(return_value=np.zeros(100, dtype=np.int16))
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


# --- T-L3 / T-M1: lock from request until the worker is idle ---


def test_disconnect_locked_from_request_until_finished(window: MainWindow) -> None:
    panel = window._radio_panel
    window._tx_worker.note_tx_requested = lambda: None  # type: ignore[method-assign]
    window._request_test_tone = MagicMock()  # keep the worker out of it
    window._on_test_tone_requested()
    assert not panel._connect_btn.isEnabled(), "Disconnect must lock at request time"
    # An error (worker possibly still unkeying) must NOT unlock…
    window._on_tx_error("Could not key rig: timeout")
    assert not panel._connect_btn.isEnabled()
    window._on_tx_aborted()
    assert not panel._connect_btn.isEnabled()
    # …only the terminal, post-idle signal does.
    window._on_tx_finished()
    assert panel._connect_btn.isEnabled()
    assert window._tx_requests_in_flight == 0


def test_error_only_then_finished_clears_error_pending(window: MainWindow) -> None:
    window._tx_requests_in_flight = 1
    window._on_tx_error("Encode failed")
    window._on_tx_finished()
    assert window._tx_error_pending is False


def test_real_tx_round_trip_unlocks(qtbot, window: MainWindow) -> None:
    window._tx_worker._ptt_delay_s = 0
    with qtbot.waitSignal(window._tx_worker.transmission_finished, timeout=3000):
        window._on_test_tone_requested()
    qtbot.waitUntil(lambda: window._radio_panel._connect_btn.isEnabled(), timeout=2000)
    assert window._tx_requests_in_flight == 0


# --- UI-M7 ---


def test_remote_tx_failing_before_keying_returns_plane_to_idle(window: MainWindow) -> None:
    calls: list[str] = []
    window._remote_control.on_tx_finished = lambda: calls.append("idle")  # type: ignore[method-assign]
    window._last_tx_was_remote = True
    window._tx_requests_in_flight = 1
    window._on_tx_error("Could not key rig")  # only error, no complete/aborted
    window._on_tx_finished()
    assert calls == ["idle"]


# --- T-M1: user disconnect while TX unwinding defers teardown ---


def test_user_disconnect_defers_teardown_while_tx_unwinding(window: MainWindow) -> None:
    fake = FakeRig()
    window._rig = fake  # type: ignore[assignment]
    killed: list[int] = []
    window._kill_rigctld = lambda: killed.append(1)  # type: ignore[method-assign]
    window._tx_worker._idle_event.clear()
    try:
        window._on_rig_disconnect()
        assert "close" not in fake.calls
        assert killed == []
        assert window._deferred_rig_teardown is fake
    finally:
        window._tx_worker._idle_event.set()
    window._unlock_rig_controls()
    assert "close" in fake.calls and killed == [1]


def test_user_disconnect_survives_oserror_on_close(window: MainWindow) -> None:
    fake = FakeRig()

    def _boom() -> None:
        raise OSError(6, "Device not configured")

    fake.close = _boom  # type: ignore[method-assign]
    window._rig = fake  # type: ignore[assignment]
    window._radio_panel.set_connected(True)
    window._on_rig_disconnect()
    assert isinstance(window._rig, ManualRig)
    assert not window._radio_panel.connected


# --- UI-M1 ---


def test_single_poll_error_keeps_connected_state(window: MainWindow) -> None:
    panel = window._radio_panel
    panel.set_connected(True)
    window._on_poll_error()
    assert panel._connect_btn.text() == "Disconnect"
    assert panel.connected
    assert "No response" in panel._status_label.text()
    panel.update_rig_status(14_230_000, "USB", -73)
    assert panel._status_label.text() == "Connected"


# --- poll pile-up / poll during TX ---


def test_poll_slot_claimed_once_until_poll_runs() -> None:
    w = _RigPollWorker()
    assert w.claim_poll()
    assert not w.claim_poll(), "a second tick must not queue another poll"
    w.poll()
    assert w.claim_poll()


def test_suspended_poll_does_not_touch_the_rig() -> None:
    w = _RigPollWorker()
    rig = MagicMock()
    w.set_rig(rig)
    w.set_suspended(True)
    w.poll()
    rig.get_freq.assert_not_called()
    w.set_suspended(False)
    rig.get_freq.return_value = 1
    rig.get_mode.return_value = ("USB", 0)
    rig.get_strength.return_value = 0
    w.poll()
    rig.get_freq.assert_called_once()


def test_tx_request_suspends_polling(window: MainWindow) -> None:
    window._request_test_tone = MagicMock()
    window._tx_worker.note_tx_requested = lambda: None  # type: ignore[method-assign]
    window._on_test_tone_requested()
    assert window._rig_poll_worker._suspended.is_set()
    window._on_tx_finished()
    assert not window._rig_poll_worker._suspended.is_set()


# --- UI-H2 / double-click ---


def test_connect_refused_while_previous_attempt_alive(window: MainWindow) -> None:
    window._config.rig_connection_mode = "rigctld"
    started: list[int] = []
    window._connect_rigctld = lambda: started.append(1)  # type: ignore[method-assign]
    window._connect_thread = QThread(window)  # stand-in: attempt still owned
    window._on_rig_connect()
    assert started == []
    window._connect_thread = None
    window._on_rig_connect()
    assert started == [1]


def test_stale_connect_thread_finish_does_not_clobber_new_attempt(
    qtbot, window: MainWindow
) -> None:
    """The first (timed-out) attempt's ``finished`` arriving after a new
    attempt started must leave the new attempt's state alone."""
    release = threading.Event()

    class _Slow(FakeRig):
        def open(self) -> None:
            release.wait(5)

    window._CONNECT_TIMEOUT_S = 0.05  # type: ignore[misc]
    errors: list[str] = []
    window._start_rig_connect_thread(_Slow(), lambda r: None, errors.append)
    old_thread = window._connect_thread
    old_done: list[int] = []
    old_thread.finished.connect(lambda: old_done.append(1))
    qtbot.waitUntil(lambda: errors != [], timeout=2000)  # timed out
    # Simulate the race the guard now prevents: a second attempt's state.
    new_thread = QThread(window)
    window._connect_thread = new_thread
    release.set()
    qtbot.waitUntil(lambda: old_done == [1], timeout=3000)
    qtbot.wait(100)
    assert window._connect_thread is new_thread
    window._connect_thread = None


def test_relay_closes_rig_that_succeeds_after_timeout(qtbot) -> None:
    """v0.6.14 sweep: the worker emitted succeeded, then the timeout won
    the race before the queued slot ran — the opened rig must be closed."""
    from PySide6.QtCore import QTimer

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = threading.Event()
    adopted: list[object] = []
    relay = _RigConnectRelay(adopted.append, lambda m: None, QThread(), QTimer(), cancel)
    cancel.set()  # timeout fired first
    rig = MagicMock()
    relay.on_succeeded(rig)
    qtbot.waitUntil(lambda: rig.close.called, timeout=2000)
    assert adopted == []


def test_timed_out_blocked_attempt_does_not_lock_out_connect(
    qtbot, window: MainWindow
) -> None:
    """v0.6.14 sweep: a timed-out attempt whose thread is still blocked in
    open() must not refuse new connects; its late success is closed and
    its finished leaves the new attempt alone."""
    release = threading.Event()
    closed: list[int] = []

    class _Slow(FakeRig):
        def open(self) -> None:
            release.wait(5)

        def close(self) -> None:
            closed.append(1)

    window._CONNECT_TIMEOUT_S = 0.05  # type: ignore[misc]
    errors: list[str] = []
    window._start_rig_connect_thread(_Slow(), lambda r: None, errors.append)
    old_thread = window._connect_thread
    old_done: list[int] = []
    old_thread.finished.connect(lambda: old_done.append(1))
    qtbot.waitUntil(lambda: errors != [], timeout=2000)  # timed out
    assert not window._connect_in_progress()

    # A second attempt starts while the first is still blocked.
    successes: list[object] = []
    fast = FakeRig()
    window._start_rig_connect_thread(fast, successes.append, errors.append)
    new_thread = window._connect_thread
    assert new_thread is not old_thread
    assert any(t is old_thread for t, _w, _r in window._retired_connects)
    qtbot.waitUntil(lambda: successes == [fast], timeout=2000)

    release.set()  # the stale attempt's open() now "succeeds"
    qtbot.waitUntil(lambda: old_done == [1], timeout=3000)
    qtbot.waitUntil(lambda: closed == [1], timeout=2000)
    qtbot.waitUntil(lambda: window._retired_connects == [], timeout=2000)
    assert successes == [fast]


def test_set_connecting_keeps_button_disabled_during_tx(window: MainWindow) -> None:
    """v0.6.14 sweep: the rigctld auto-launch path calls set_connecting
    again after the 500 ms delay — it must not re-enable Cancel mid-TX."""
    panel = window._radio_panel
    panel.set_tx_active(True)
    panel.set_connecting()
    assert not panel._connect_btn.isEnabled()
    panel.set_tx_active(False)
    assert panel._connect_btn.isEnabled()
    assert panel._connect_btn.text() == "Cancel"


def test_connect_completing_mid_tx_is_adopted_after_tx(
    qtbot, window: MainWindow
) -> None:
    """v0.6.14 sweep: no tx_worker.set_rig swap under an in-flight TX."""
    window._tx_worker.note_tx_requested = lambda: None  # type: ignore[method-assign]
    window._request_test_tone = MagicMock()  # keep the worker out of it
    window._on_test_tone_requested()  # one TX dispatched
    assert window._tx_requests_in_flight == 1
    set_rig = MagicMock()
    window._tx_worker.set_rig = set_rig  # type: ignore[method-assign]
    adopted: list[object] = []

    def _on_success(rig: object) -> None:
        adopted.append(rig)
        window._tx_worker.set_rig(rig)

    rig = FakeRig()
    window._start_rig_connect_thread(rig, _on_success, lambda m: None)
    qtbot.waitUntil(lambda: window._deferred_connect_success is not None, timeout=2000)
    assert adopted == [] and not set_rig.called
    assert window._connect_in_progress()
    window._on_tx_finished()
    assert adopted == [rig]
    set_rig.assert_called_once_with(rig)
    assert window._deferred_connect_success is None


def test_abort_closes_rig_waiting_for_tx_end(window: MainWindow) -> None:
    rig = FakeRig()
    window._deferred_connect_success = (rig, lambda r: None)  # type: ignore[assignment]
    window._abort_connect()
    assert "close" in rig.calls
    assert window._deferred_connect_success is None


def test_double_click_during_rigctld_launch_delay(
    qtbot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = window._config
    cfg.rig_connection_mode = "rigctld"
    cfg.auto_launch_rigctld = True
    cfg.rig_model_id = 1
    launches: list[int] = []
    finishes: list[int] = []
    proc = MagicMock()
    monkeypatch.setattr(
        "open_sstv.ui.main_window.launch_rigctld",
        lambda *a, **k: (launches.append(1), proc)[1],
    )
    window._finish_rigctld_connect = lambda h, p: finishes.append(1)  # type: ignore[method-assign]
    window._on_rig_connect()
    window._on_rig_connect()  # double click
    assert launches == [1]
    assert window._radio_panel._connect_btn.text() == "Cancel"
    qtbot.waitUntil(lambda: finishes == [1], timeout=2000)
    qtbot.wait(600)
    assert finishes == [1]
    window._rigctld_proc = None


def test_cancel_during_rigctld_launch_delay_stops_the_connect(
    qtbot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = window._config
    cfg.rig_connection_mode = "rigctld"
    cfg.auto_launch_rigctld = True
    cfg.rig_model_id = 1
    monkeypatch.setattr("open_sstv.ui.main_window.launch_rigctld", lambda *a, **k: MagicMock())
    finishes: list[int] = []
    window._finish_rigctld_connect = lambda h, p: finishes.append(1)  # type: ignore[method-assign]
    window._on_rig_connect()
    window._on_connect_cancel()
    qtbot.wait(700)
    assert finishes == []
    window._rigctld_proc = None


# --- T-L1 ---


def test_close_sends_emergency_unkey_early_when_worker_wedged(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[float] = []
    t0 = time.monotonic()
    window._tx_worker.emergency_unkey = lambda: calls.append(time.monotonic() - t0)  # type: ignore[method-assign]
    window._tx_worker._idle_event.clear()  # wedged TX
    # Make the rest of shutdown slow-but-bounded so "early" is measurable.
    monkeypatch.setattr(window, "_stop_remote_server", lambda: time.sleep(1.0))
    try:
        threading.Timer(2.0, window._tx_worker._idle_event.set).start()
        window.close()
    finally:
        window._tx_worker._idle_event.set()
    assert calls, "emergency_unkey must run during close"
    assert calls[0] < 1.0, f"emergency unkey ran {calls[0]:.1f}s into close — too late"
