# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest-qt smoke tests for ``open_sstv.ui.main_window.MainWindow``.

These verify the window can be constructed, the worker thread starts,
and the basic signal wiring fires through to the panel — without
actually playing audio. ``encode`` and ``play_blocking`` are patched out
in conftest-style fixtures because the worker would otherwise try to
encode a real image and open a real audio device.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.radio.base import ManualRig
from open_sstv.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, MagicMock]]:
    encode_mock = MagicMock(return_value=np.zeros(100, dtype=np.int16))
    play_mock = MagicMock()
    stop_mock = MagicMock()
    monkeypatch.setattr("open_sstv.ui.workers.encode", encode_mock)
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", play_mock)
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", stop_mock)
    yield {"encode": encode_mock, "play": play_mock, "stop": stop_mock}


@pytest.fixture
def _suppress_first_launch_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``first_launch_seen=True`` on the loaded config.

    v0.2.7 added a welcome-callsign dialog that fires as a modal on
    first launch. Without this fixture, any CI machine without a
    pre-existing config file (or a dev running in a clean XDG dir)
    would block indefinitely on the modal during ``qtbot.waitExposed``.
    We preserve whatever the real ``load_config`` returns (tests may
    depend on the dev's actual audio/rig defaults) and only stamp the
    seen-flag before handing the config to ``MainWindow``.

    Passing an explicit ``config=`` kwarg to ``MainWindow`` is avoided
    because that path triggered a separate teardown segfault on
    Darwin — see the v0.1.33 note further down this file.
    """
    from open_sstv.config.store import load_config as _real_load_config

    def _patched() -> object:
        cfg = _real_load_config()
        cfg.first_launch_seen = True
        return cfg

    monkeypatch.setattr("open_sstv.ui.main_window.load_config", _patched)


@pytest.fixture
def window(
    qtbot,
    patched_audio: dict[str, MagicMock],
    _suppress_first_launch_dialog: None,
) -> MainWindow:
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)  # ensures pytest-qt cleans up the window on test exit
    return w


@pytest.fixture
def gradient_path(tmp_path: Path) -> Path:
    img = Image.new("RGB", (100, 100), color=(20, 40, 60))
    p = tmp_path / "img.png"
    img.save(p)
    return p


def test_window_constructs_and_shows(window: MainWindow, qtbot) -> None:
    window.show()
    qtbot.waitExposed(window)
    assert window.windowTitle() == "Open-SSTV"
    assert window._tx_thread.isRunning()


def test_central_widget_hosts_tx_and_rx_panels(window: MainWindow) -> None:
    """The central widget contains a RadioPanel and a QSplitter hosting
    both a TxPanel and an RxPanel."""
    from PySide6.QtWidgets import QSplitter

    from open_sstv.ui.radio_panel import RadioPanel
    from open_sstv.ui.rx_panel import RxPanel
    from open_sstv.ui.tx_panel import TxPanel

    central = window.centralWidget()
    # The central widget is now a QWidget wrapping the radio panel and splitter.
    children = central.findChildren(QSplitter)
    assert len(children) >= 1
    splitter = children[0]
    panels = [splitter.widget(i) for i in range(splitter.count())]
    assert any(isinstance(p, TxPanel) for p in panels)
    assert any(isinstance(p, RxPanel) for p in panels)
    assert central.findChild(RadioPanel) is not None


def test_transmit_round_trip_through_worker(
    qtbot,
    window: MainWindow,
    gradient_path: Path,
    patched_audio: dict[str, MagicMock],
) -> None:
    """Load an image, click Transmit, and wait for the worker's
    transmission_complete signal to come back to the main thread."""
    window._tx_panel.load_image(gradient_path)

    # Speed the PTT delay down to zero so the test doesn't sit there
    # waiting 200 ms for nothing.
    window._tx_worker._ptt_delay_s = 0

    with qtbot.waitSignal(
        window._tx_worker.transmission_complete, timeout=2000
    ):
        window._tx_panel._transmit_btn.click()

    patched_audio["play"].assert_called_once()
    # ``transmission_complete`` is emitted from the worker thread and
    # delivered to the panel via a queued connection, so the button
    # state update is one event-loop spin behind the signal. Wait for
    # the panel to actually re-enable rather than racing on it.
    qtbot.waitUntil(
        lambda: window._tx_panel._transmit_btn.isEnabled(), timeout=1000
    )
    assert not window._tx_panel._stop_btn.isEnabled()


def test_stop_button_calls_request_stop(
    qtbot,
    window: MainWindow,
    patched_audio: dict[str, MagicMock],
) -> None:
    """Stop is a direct method call, so we don't get a queued signal —
    we just verify the panel's stop_requested wire reaches the worker
    and the underlying sounddevice.stop is called."""
    # Force the panel into "transmitting" state so the Stop button is enabled.
    window._tx_panel.set_transmitting(True)
    window._tx_panel._stop_btn.click()
    patched_audio["stop"].assert_called()


# ---------------------------------------------------------------------------
# v0.1.33 note: startup applies persisted config
# ---------------------------------------------------------------------------
#
# The source fix (``main_window.py`` seeds each worker from ``self._config``
# before moving it to its thread, for output_gain / input_gain / PTT delay /
# TX banner / CW ID / sample rate) is manually verified end-to-end via the
# app itself — the user's persisted settings now take effect on first launch.
#
# No automated regression test is included here because constructing a
# second MainWindow with an explicit ``config=AppConfig(...)`` kwarg inside
# pytest-qt on macOS produces a deterministic teardown segfault in a worker
# thread.  It reproduces even with the simplest possible test
# (``MainWindow(rig=ManualRig(), config=AppConfig())``), even with
# ``sounddevice`` fully monkey-patched, and it does *not* reproduce when
# called from a plain Python script that exercises the exact same code.
# The issue appears to be a PySide6 / pytest-qt interaction specific to
# passing a non-``None`` config to the existing ``MainWindow`` constructor
# in a test harness on Darwin.  Tracked for a follow-up dedicated
# investigation — the user-impact fix ships without it.


# ---------------------------------------------------------------------------
# v0.2.11: connect timeout, Cancel button, close-while-connecting safety
# ---------------------------------------------------------------------------


def test_on_connect_cancel_resets_panel_to_disconnected(
    window: MainWindow, qtbot
) -> None:
    """Calling _on_connect_cancel() must restore 'Connect Rig' text and
    re-enable the button, regardless of whether a thread is running."""
    window._radio_panel.set_connecting()
    assert window._radio_panel._connect_btn.text() == "Cancel"

    window._on_connect_cancel()

    assert window._radio_panel._connect_btn.text() == "Connect Rig"
    assert window._radio_panel._connect_btn.isEnabled()
    # Status bar should mention "cancelled".
    assert "cancel" in window.statusBar().currentMessage().lower()


def test_rig_connect_worker_cancel_suppresses_succeeded(qapp) -> None:
    """_RigConnectWorker must not emit succeeded when cancel is pre-set."""
    import threading as _threading
    from open_sstv.radio.base import ManualRig
    from open_sstv.ui.main_window import _RigConnectWorker

    cancel = _threading.Event()
    cancel.set()  # pre-cancel before run()
    worker = _RigConnectWorker(ManualRig(), cancel)

    succeeded: list[object] = []
    failed: list[str] = []
    worker.succeeded.connect(lambda r: succeeded.append(r))
    worker.failed.connect(lambda e: failed.append(e))

    worker.run()  # synchronous on the test thread

    assert succeeded == [], "cancelled worker must not emit succeeded"
    assert failed == [], "cancelled worker must not emit failed"


def test_rig_connect_worker_cancel_suppresses_failed(qapp, monkeypatch) -> None:
    """_RigConnectWorker must not emit failed when cancel fires before open()
    returns — covers the case where open() raises and cancel is already set."""
    import threading as _threading
    from unittest.mock import MagicMock
    from open_sstv.ui.main_window import _RigConnectWorker

    cancel = _threading.Event()
    cancel.set()

    bad_rig = MagicMock()
    bad_rig.open.side_effect = Exception("port busy")
    worker = _RigConnectWorker(bad_rig, cancel)

    failed: list[str] = []
    worker.failed.connect(lambda e: failed.append(e))
    worker.run()

    assert failed == [], "cancelled worker must not emit failed even on open() error"


def test_abort_connect_is_noop_when_idle(window: MainWindow) -> None:
    """_abort_connect() must not raise when no connect is in flight."""
    assert window._connect_thread is None
    window._abort_connect()  # must not raise
    assert window._connect_thread is None


def test_connect_timeout_calls_on_error(window: MainWindow, qtbot, monkeypatch) -> None:
    """When _CONNECT_TIMEOUT_S elapses, on_error must be called with a
    'timed out' message and the UI must return to a usable state."""
    import threading as _threading

    monkeypatch.setattr(type(window), "_CONNECT_TIMEOUT_S", 0.05)

    gate = _threading.Event()
    error_messages: list[str] = []

    slow_rig = MagicMock()

    def _slow_open() -> None:
        gate.wait(5.0)  # released by on_error so the thread finishes quickly

    slow_rig.open.side_effect = _slow_open

    def _on_success(_r: object) -> None:
        pass  # should not be called

    def _on_error(msg: str) -> None:
        error_messages.append(msg)
        gate.set()  # unblock the slow open so the thread can exit

    window._radio_panel.set_connecting()
    window._start_rig_connect_thread(slow_rig, _on_success, _on_error)

    qtbot.waitUntil(lambda: len(error_messages) > 0, timeout=1000)

    assert "timed out" in error_messages[0].lower()
    # Wait for the thread to finish (gate was set in _on_error)
    qtbot.waitUntil(
        lambda: window._connect_thread is None
        or not window._connect_thread.isRunning(),
        timeout=1000,
    )


def test_close_while_connecting_no_crash(
    qtbot,
    patched_audio: dict[str, MagicMock],
    _suppress_first_launch_dialog: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the window while a connect attempt is in-flight must not crash.

    Regression: QThread(parent=MainWindow) is destroyed by Qt's deleteChildren
    while the thread is still blocking in rig.open() → QThread::~QThread()
    calls fatal().  Fixed by _abort_connect() at the top of closeEvent().
    """
    import threading as _threading

    # Use a very short timeout so _abort_connect doesn't wait 5 s in CI.
    gate = _threading.Event()

    window = MainWindow(rig=ManualRig())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    slow_rig = MagicMock()
    slow_rig.open.side_effect = lambda: gate.wait(3.0)

    window._radio_panel.set_connecting()
    window._start_rig_connect_thread(slow_rig, lambda _: None, lambda _: None)

    # Close immediately — _abort_connect must stop the thread before Qt's
    # deleteChildren destroys the QThread object.
    gate.set()  # unblock rig.open so the thread can finish during abort
    window.close()  # triggers closeEvent → _abort_connect → thread.wait()
