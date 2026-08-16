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


# ---------------------------------------------------------------------------
# v0.3.5 — default geometry tuned to fit 4 template gallery cards in TX panel
# ---------------------------------------------------------------------------


def test_default_window_size_is_1280x720(window: MainWindow) -> None:
    """v0.3.5 bumped the default size from 1100×640 to 1280×720 so the
    initial split allocates enough TX-panel width for 4 cards in the
    template gallery's flow layout (4 × 140 px + gutters + margins ≈
    632 px).  Pin the default geometry so a future tweak that pulls
    the window narrower without re-checking the gallery math gets
    flagged here."""
    sz = window.size()
    assert sz.width() == 1280
    assert sz.height() == 720


def test_splitter_initial_sizes_favor_tx_panel_for_four_cards(
    window: MainWindow, qtbot
) -> None:
    """The splitter's initial sizes bias TX wider than RX so the first-
    open template gallery shows 4 cards in a row.  After ``show()`` and
    ``waitExposed`` the rendered TX-panel width must be at least the
    4-card budget (~632 px).  Equal-stretch factors mean subsequent
    user resizes grow both sides symmetrically."""
    from PySide6.QtWidgets import QSplitter

    window.show()
    qtbot.waitExposed(window)

    splitter = window.centralWidget().findChild(QSplitter)
    assert splitter is not None
    sizes = splitter.sizes()
    assert len(sizes) == 2, f"expected 2 splitter children, got {len(sizes)}"
    tx_width, rx_width = sizes
    # 4-card budget: 4 × 140 px thumbs + 3 × 8 px gutters + flow / panel
    # margins = ~632 px.  Allow a small safety margin in the assertion
    # so platform-dependent chrome adjustments don't make the test
    # flake on tiny rendering differences.
    assert tx_width >= 632, (
        f"TX panel rendered at {tx_width} px — below the 4-card budget "
        f"(~632 px). RX got {rx_width} px."
    )
    # RX still gets meaningful room — not so squeezed it's unusable.
    assert rx_width >= 400, (
        f"RX panel rendered at {rx_width} px — too narrow for the "
        f"image gallery to be usable."
    )


def test_user_resize_still_grows_both_panels(
    window: MainWindow, qtbot
) -> None:
    """Stretch factors are kept at 1:1 so dragging the window edge
    grows both panels.  Verify by resizing the window wider and
    confirming both panels gained width vs the initial split."""
    from PySide6.QtWidgets import QSplitter

    window.show()
    qtbot.waitExposed(window)
    splitter = window.centralWidget().findChild(QSplitter)
    initial = splitter.sizes()

    # Resize the window 200 px wider and let the layout settle.
    window.resize(1480, 720)
    qtbot.waitUntil(lambda: sum(splitter.sizes()) > sum(initial), timeout=500)

    after = splitter.sizes()
    # Both panels should have grown (stretch 1:1 means each gets ~half
    # of the extra 200 px).
    assert after[0] > initial[0], (
        f"TX panel did not grow on window resize: {initial[0]} → {after[0]}"
    )
    assert after[1] > initial[1], (
        f"RX panel did not grow on window resize: {initial[1]} → {after[1]}"
    )


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


def test_handle_worker_error_routes_to_status_bar(
    qtbot, window: MainWindow,
) -> None:
    """M4: TxWorker.error_occurred → MainWindow._handle_worker_error → status bar.

    The worker emits with the slot name and the live exception object; the
    handler stringifies them into a 5-second status-bar message.  Direct
    invocation of the slot keeps the test off the cross-thread queue so the
    assertion doesn't have to ``waitUntil`` a queued delivery — the wiring
    test (next case) covers the queued-connection path.
    """
    boom = RuntimeError("kaboom")
    window._handle_worker_error("test_slot", boom)
    msg = window.statusBar().currentMessage()
    assert "test_slot" in msg
    assert "kaboom" in msg


def test_worker_error_signal_is_wired_to_handler(
    qtbot, window: MainWindow,
) -> None:
    """M4: TxWorker.error_occurred is connected to _handle_worker_error.

    Emit from the worker (still on the GUI thread because the test holds it
    constructed-but-pre-moveToThread for the connect call) and let the queued
    delivery flush through the event loop — the handler should run and the
    status-bar message should reflect the emitted slot name.
    """
    boom = RuntimeError("queued boom")
    window._tx_worker.error_occurred.emit("queued_slot", boom)
    qtbot.waitUntil(
        lambda: "queued_slot" in window.statusBar().currentMessage(),
        timeout=1000,
    )
    assert "queued boom" in window.statusBar().currentMessage()


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


@pytest.mark.skip(reason="flaky headless QThread timing — QTimer.singleShot guard fires on deleted C++ object")
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


# ---------------------------------------------------------------------------
# _RigConnectRelay unit tests (OP2-02 bugfix: lambda→QObject relay)
# ---------------------------------------------------------------------------


def test_relay_on_succeeded_calls_on_success(qapp) -> None:
    """_RigConnectRelay.on_succeeded must invoke on_success with the rig."""
    import threading as _threading
    from unittest.mock import MagicMock

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = _threading.Event()
    timer = MagicMock()
    thread = MagicMock()
    rig = MagicMock()
    results: list[object] = []

    relay = _RigConnectRelay(lambda r: results.append(r), lambda _: None, thread, timer, cancel)
    relay.on_succeeded(rig)

    assert results == [rig]
    timer.stop.assert_called_once()
    thread.quit.assert_called_once()


def test_relay_on_failed_calls_on_error(qapp) -> None:
    """_RigConnectRelay.on_failed must invoke on_error with the message."""
    import threading as _threading
    from unittest.mock import MagicMock

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = _threading.Event()
    timer = MagicMock()
    thread = MagicMock()
    errors: list[str] = []

    relay = _RigConnectRelay(lambda _: None, lambda e: errors.append(e), thread, timer, cancel)
    relay.on_failed("port busy")

    assert errors == ["port busy"]
    timer.stop.assert_called_once()
    thread.quit.assert_called_once()


def test_relay_cancel_suppresses_on_succeeded(qapp) -> None:
    """Relay must not call on_success if cancel is already set (e.g. timeout won)."""
    import threading as _threading
    from unittest.mock import MagicMock

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = _threading.Event()
    cancel.set()
    timer = MagicMock()
    thread = MagicMock()
    results: list[object] = []

    relay = _RigConnectRelay(lambda r: results.append(r), lambda _: None, thread, timer, cancel)
    relay.on_succeeded(MagicMock())

    assert results == []
    timer.stop.assert_not_called()


def test_relay_cancel_suppresses_on_failed(qapp) -> None:
    """Relay must not call on_error if cancel is already set."""
    import threading as _threading
    from unittest.mock import MagicMock

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = _threading.Event()
    cancel.set()
    timer = MagicMock()
    thread = MagicMock()
    errors: list[str] = []

    relay = _RigConnectRelay(lambda _: None, lambda e: errors.append(e), thread, timer, cancel)
    relay.on_failed("CI-V timeout")

    assert errors == []
    timer.stop.assert_not_called()


def test_relay_on_succeeded_sets_cancel_to_block_timeout(qapp) -> None:
    """on_succeeded must mark cancel so a racing timeout callback is a no-op."""
    import threading as _threading
    from unittest.mock import MagicMock

    from open_sstv.ui.main_window import _RigConnectRelay

    cancel = _threading.Event()
    relay = _RigConnectRelay(
        lambda _: None, lambda _: None, MagicMock(), MagicMock(), cancel
    )
    relay.on_succeeded(MagicMock())
    assert cancel.is_set()


def test_start_rig_connect_thread_success_updates_radio_panel(
    window: MainWindow, qtbot
) -> None:
    """Full integration: a fast-responding rig must flip the panel to Connected.

    This is the regression test for the OP2-02 lambda→relay fix.  Before the
    fix, on_success ran on the worker thread where widget mutations are silently
    dropped on macOS, so the panel stayed stuck at 'Connecting' forever.
    """
    fast_rig = MagicMock()
    fast_rig.open.return_value = None
    fast_rig.ping.return_value = None

    window._radio_panel.set_connecting()
    assert window._radio_panel._connect_btn.text() == "Cancel"

    def _on_success(connected_rig: object) -> None:
        window._radio_panel.set_connected(True)

    def _on_error(msg: str) -> None:
        pass

    window._start_rig_connect_thread(fast_rig, _on_success, _on_error)

    qtbot.waitUntil(
        lambda: window._radio_panel._connect_btn.text() == "Disconnect",
        timeout=2000,
    )
    assert window._radio_panel.connected


def test_start_rig_connect_thread_failure_updates_radio_panel(
    window: MainWindow, qtbot
) -> None:
    """A failing rig must call on_error and leave a usable state."""
    from open_sstv.radio.exceptions import RigConnectionError

    bad_rig = MagicMock()
    bad_rig.open.side_effect = RigConnectionError("port not found")

    errors: list[str] = []

    def _on_error(msg: str) -> None:
        errors.append(msg)
        window._radio_panel.set_connection_error()

    window._radio_panel.set_connecting()
    window._start_rig_connect_thread(bad_rig, lambda _: None, _on_error)

    qtbot.waitUntil(lambda: len(errors) > 0, timeout=2000)
    assert "port not found" in errors[0]
    qtbot.waitUntil(
        lambda: window._radio_panel._connect_btn.text() == "Connect Rig",
        timeout=1000,
    )


# ---------------------------------------------------------------------------
# _RigPollWorker: consecutive-error counter + auto-disconnect signal
# ---------------------------------------------------------------------------


class TestRigPollWorkerErrorCounter:
    """Unit tests for _RigPollWorker's consecutive-error counter and
    radio_disconnected signal.  Tests bypass the QThread and call poll()
    directly on the test thread so they run synchronously and need no
    qtbot.waitUntil.
    """

    def _make_worker(self):  # type: ignore[no-untyped-def]
        # _RigPollWorker is intentionally private; import lazily so the
        # test file doesn't claim public access.  Return type elided
        # because the forward reference can't be resolved without
        # exposing the private name at module level.
        from open_sstv.ui.main_window import _RigPollWorker  # type: ignore[attr-defined]
        return _RigPollWorker()

    def test_successful_poll_resets_counter(self, qapp) -> None:
        """A successful poll resets consecutive_errors to 0."""
        from open_sstv.radio.base import ManualRig

        worker = self._make_worker()
        worker._consecutive_errors = 2  # simulate prior failures
        worker.set_rig(ManualRig())  # set_rig also resets, but...

        # Force the counter back to 2 to test that poll() itself resets it
        worker._consecutive_errors = 2
        worker.poll()

        assert worker._consecutive_errors == 0

    def test_unsupported_s_meter_does_not_disconnect(self, qapp) -> None:
        """An S-meter that isn't supported must NOT drop the rig.

        A rigctld backend with no STRENGTH level answers RPRT -11.  That
        used to share a try-block with freq/mode, so it tripped the
        3-strike auto-disconnect and killed a rig whose PTT and frequency
        control were working fine (reported by a FlexRadio user).
        """
        from open_sstv.radio.exceptions import RigCommandError

        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.return_value = 14_074_000
        rig.get_mode.return_value = ("USB", 2400)
        rig.get_strength.side_effect = RigCommandError(
            "'l STRENGTH' returned RPRT -11", command="l STRENGTH"
        )
        worker.set_rig(rig)

        results: list[tuple] = []
        disconnects: list[int] = []
        worker.poll_result.connect(lambda *a: results.append(a))
        worker.radio_disconnected.connect(lambda: disconnects.append(1))

        for _ in range(5):  # well past the 3-strike threshold
            worker.poll()

        assert worker._consecutive_errors == 0, "S-meter must not count as a failure"
        assert disconnects == [], "an unsupported S-meter must not disconnect the rig"
        assert len(results) == 5, "freq/mode must still be reported"
        assert results[0][0] == 14_074_000
        assert results[0][2] == 0, "unavailable strength reads as 0"

    def test_failed_poll_increments_counter(self, qapp) -> None:
        """Each failing poll increments the counter by 1."""
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.side_effect = RuntimeError("device gone")
        worker.set_rig(rig)

        assert worker._consecutive_errors == 0
        worker.poll()
        assert worker._consecutive_errors == 1
        worker.poll()
        assert worker._consecutive_errors == 2

    def test_radio_disconnected_fires_at_threshold(self, qapp) -> None:
        """radio_disconnected emits exactly once at _POLL_FAIL_THRESHOLD."""
        from open_sstv.ui.main_window import _RigPollWorker  # type: ignore[attr-defined]

        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.side_effect = RuntimeError("unplug")
        worker.set_rig(rig)

        disconnected: list[bool] = []
        worker.radio_disconnected.connect(lambda: disconnected.append(True))

        threshold = _RigPollWorker._POLL_FAIL_THRESHOLD
        for _ in range(threshold - 1):
            worker.poll()
        assert disconnected == [], "signal must not fire before threshold"

        worker.poll()
        assert disconnected == [True], "signal must fire exactly at threshold"

        # Additional failures must NOT re-fire the signal
        worker.poll()
        worker.poll()
        assert disconnected == [True], "signal must not fire again above threshold"

    def test_poll_error_emitted_on_every_failure(self, qapp) -> None:
        """poll_error fires on every failing poll, regardless of threshold."""
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.side_effect = RuntimeError("gone")
        worker.set_rig(rig)

        errors: list[bool] = []
        worker.poll_error.connect(lambda: errors.append(True))

        for _ in range(5):
            worker.poll()

        assert len(errors) == 5

    def test_set_rig_resets_counter(self, qapp) -> None:
        """set_rig() resets consecutive_errors so a new rig starts fresh."""
        from open_sstv.radio.base import ManualRig

        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.side_effect = RuntimeError("gone")
        worker.set_rig(rig)

        worker.poll()
        worker.poll()
        assert worker._consecutive_errors == 2

        worker.set_rig(ManualRig())
        assert worker._consecutive_errors == 0

    def test_termios_error_triggers_disconnect(self, qapp) -> None:
        """termios.error from get_freq increments counter and fires the signal."""
        import termios

        from open_sstv.ui.main_window import _RigPollWorker  # type: ignore[attr-defined]

        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.side_effect = termios.error(6, "Device not configured")
        worker.set_rig(rig)

        disconnected: list[bool] = []
        worker.radio_disconnected.connect(lambda: disconnected.append(True))

        threshold = _RigPollWorker._POLL_FAIL_THRESHOLD
        for _ in range(threshold):
            worker.poll()

        assert disconnected == [True]


class TestRigPollWorkerTune:
    """Unit tests for _RigPollWorker.tune() — the Band Plan CAT write path.

    ``set_freq()`` on Kenwood/Yaesu direct-serial rigs is fire-and-forget
    (the radio sends no response, so a rejected frequency change is
    otherwise indistinguishable from success) — ``tune()`` now verifies
    with a ``get_freq()`` readback and emits ``tune_failed`` on mismatch.
    """

    def _make_worker(self):  # type: ignore[no-untyped-def]
        from open_sstv.ui.main_window import _RigPollWorker  # type: ignore[attr-defined]
        return _RigPollWorker()

    def test_tune_sets_freq_and_mode_when_family_differs(self, qapp) -> None:
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.return_value = 14_230_000  # matches the requested freq
        rig.get_mode.return_value = ("CW", 0)  # different family than USB
        worker.set_rig(rig)

        failures: list[str] = []
        worker.tune_failed.connect(lambda reason: failures.append(reason))

        worker.tune(14_230_000, "USB", 2700)

        rig.set_freq.assert_called_once_with(14_230_000)
        rig.set_mode.assert_called_once_with("USB", 2700)
        assert failures == []

    def test_tune_skips_mode_when_family_matches(self, qapp) -> None:
        """User already on a data variant (e.g. Yaesu DATA-U) — same
        family as the target, so set_mode must not be re-sent."""
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.return_value = 14_230_000
        rig.get_mode.return_value = ("DATA-U", 0)
        worker.set_rig(rig)

        worker.tune(14_230_000, "USB", 2700)

        rig.set_freq.assert_called_once_with(14_230_000)
        rig.set_mode.assert_not_called()

    def test_tune_emits_tune_failed_on_freq_readback_mismatch(self, qapp) -> None:
        """set_freq() reported success but the radio is still on the old
        frequency (dial lock, band-edge reject, …) — must be surfaced."""
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.return_value = 7_100_000  # unchanged, not the requested freq
        worker.set_rig(rig)

        failures: list[str] = []
        worker.tune_failed.connect(lambda reason: failures.append(reason))

        worker.tune(14_230_000, "USB", 2700)

        assert len(failures) == 1
        assert "7100000" in failures[0] or "7_100_000" in failures[0]
        # A failed freq change must not proceed to set_mode.
        rig.set_mode.assert_not_called()

    def test_tune_zero_freq_readback_is_not_treated_as_mismatch(self, qapp) -> None:
        """PTT-only rigs never report frequency (get_freq() always 0) —
        that must not be flagged as a failed tune."""
        worker = self._make_worker()
        rig = MagicMock()
        rig.get_freq.return_value = 0
        rig.get_mode.return_value = ("", 0)
        worker.set_rig(rig)

        failures: list[str] = []
        worker.tune_failed.connect(lambda reason: failures.append(reason))

        worker.tune(14_230_000, "USB", 2700)

        assert failures == []

    def test_tune_emits_tune_failed_on_set_freq_exception(self, qapp) -> None:
        from open_sstv.radio.exceptions import RigCommandError

        worker = self._make_worker()
        rig = MagicMock()
        rig.set_freq.side_effect = RigCommandError("Radio rejected command (?)")
        worker.set_rig(rig)

        failures: list[str] = []
        worker.tune_failed.connect(lambda reason: failures.append(reason))

        worker.tune(14_230_000, "USB", 2700)

        assert len(failures) == 1


class TestOnRadioDisconnected:
    """Integration: _on_radio_disconnected reverts MainWindow to idle state."""

    def test_on_radio_disconnected_stops_timer_and_sets_disconnected(
        self, window: MainWindow, qapp
    ) -> None:
        """After _on_radio_disconnected fires, the poll timer stops and
        the radio panel shows disconnected state."""
        from open_sstv.radio.base import ManualRig

        # Simulate a connected state
        fake_rig = MagicMock()
        fake_rig.get_freq.return_value = 14_074_000
        fake_rig.get_mode.return_value = ("USB", 2400)
        fake_rig.get_strength.return_value = -73
        window._rig = fake_rig
        window._radio_panel.set_connected(True)
        window._rig_poll_timer.start()

        assert window._rig_poll_timer.isActive()
        assert window._radio_panel.connected

        window._on_radio_disconnected()

        assert not window._rig_poll_timer.isActive()
        assert not window._radio_panel.connected
        assert isinstance(window._rig, ManualRig)

    def test_on_radio_disconnected_is_idempotent(
        self, window: MainWindow, qapp
    ) -> None:
        """Calling _on_radio_disconnected when already disconnected is a no-op."""
        from open_sstv.radio.base import ManualRig

        assert isinstance(window._rig, ManualRig)  # starts disconnected
        window._on_radio_disconnected()  # must not raise or change state
        assert isinstance(window._rig, ManualRig)

    def test_on_radio_disconnected_closes_old_rig(
        self, window: MainWindow, qapp
    ) -> None:
        """The old rig's close() is called, even if it raises."""
        dying_rig = MagicMock()
        dying_rig.close.side_effect = Exception("termios.error: device gone")
        window._rig = dying_rig

        window._on_radio_disconnected()  # must not raise

        dying_rig.close.assert_called_once()


# ---------------------------------------------------------------------------
# USB replug / device re-enumeration tests
# ---------------------------------------------------------------------------


class TestAudioDeviceReplug:
    """Verify that the Start Capture button is never permanently disabled
    and that a re-plugged USB audio device is found by name even if its
    PortAudio index changed.
    """

    def test_start_button_re_enabled_after_stream_open_fails(
        self, window: MainWindow, qtbot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If InputStreamWorker.start() fails (e.g. stale device index), the
        Start Capture button must be re-enabled so the user can try again.

        Regression: before the fix, start() emitted error but not stopped,
        so RxPanel.set_capturing() was never called and the button stayed
        greyed out permanently.
        """
        import sounddevice as _sd

        # Make every sd.InputStream() raise so start() always fails.
        monkeypatch.setattr(
            "open_sstv.audio.input_stream.sd.InputStream",
            MagicMock(side_effect=_sd.PortAudioError("no device")),
        )

        btn = window._rx_panel._start_btn
        assert btn.isEnabled(), "button should start enabled"

        # Simulate user clicking Start.
        btn.click()
        # Button is disabled immediately on click.
        assert not btn.isEnabled()

        # Process the queued signal chain: capture_requested →
        # _on_capture_requested → reset_done → _request_start_capture →
        # audio_worker.start() → stopped (from failure) → _on_rx_stopped →
        # set_capturing(False) → button re-enabled.
        qtbot.waitUntil(lambda: btn.isEnabled(), timeout=2000)
        assert btn.text() == "Start Capture"

    def test_capture_start_re_enumerates_input_device_by_name(
        self, window: MainWindow, qtbot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a device-lost event, MainWindow must look up the configured
        input device by name on the next Start so a replug (new PortAudio
        index) is handled transparently.

        M16: the re-enumeration is now gated on
        ``_input_device_needs_relookup`` (set by
        ``_on_audio_device_lost``).  Without a prior device-lost event,
        the cached index is reused — this avoids 50-500ms GUI freezes
        from ``sd.query_devices()`` on every capture start in the
        common case where nothing has changed.  The test simulates the
        replug scenario by flipping the flag before calling Start.
        """
        from open_sstv.audio.devices import AudioDevice

        # Simulate the configured device name.
        window._config.audio_input_device = "IC-7300 USB Audio CODEC"

        # The old (stale) device object — wrong index 3.
        stale_device = AudioDevice(
            index=3,
            name="IC-7300 USB Audio CODEC",
            host_api="CoreAudio",
            max_input_channels=2,
            max_output_channels=0,
            default_sample_rate=48000.0,
        )
        window._input_device = stale_device
        # M16: simulate a prior device-lost event so _start_once knows
        # to re-resolve the PortAudio index by name on this Start.
        window._input_device_needs_relookup = True

        # The fresh device after replug — correct new index 7.
        fresh_device = AudioDevice(
            index=7,
            name="IC-7300 USB Audio CODEC",
            host_api="CoreAudio",
            max_input_channels=2,
            max_output_channels=0,
            default_sample_rate=48000.0,
        )

        # Patch find_input_device_by_name to return the fresh device.
        monkeypatch.setattr(
            "open_sstv.ui.main_window.find_input_device_by_name",
            lambda _name: fresh_device,
        )

        # Capture what device index reaches audio_worker.start().
        received_device: list[object] = []

        def _capture_start(device, *args, **kwargs):
            received_device.append(device)
            # Don't actually open a stream — just emit started so the test
            # doesn't hang on set_capturing() waiting for audio.
            window._audio_worker.started.emit()

        monkeypatch.setattr(window._audio_worker, "start", _capture_start)

        # Also patch reset so reset_done fires synchronously.
        original_reset = window._rx_worker.reset

        def _fast_reset():
            original_reset()

        monkeypatch.setattr(window._rx_worker, "reset", _fast_reset)

        window._on_capture_requested(True)

        # reset_done fires on rx_worker thread; process events to let
        # _start_once fire and call our _capture_start mock.
        qtbot.waitUntil(lambda: len(received_device) > 0, timeout=2000)

        assert received_device[0] is fresh_device, (
            "_on_capture_requested must re-enumerate the device by name so a "
            "replug with a new PortAudio index is handled correctly"
        )


# ---------------------------------------------------------------------------
# UI feedback for audio device disconnect (stream_error signal chain)
# ---------------------------------------------------------------------------


class TestAudioDeviceLostUI:
    """Verify the device-loss message persists in the status bar and RX panel
    and is not overwritten by the generic 'Capture stopped.' / 'Ready' text.
    """

    def test_device_lost_message_shown_in_status_bar(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_audio_device_lost must post a sticky status-bar message with
        no timeout, so it survives until the user acts."""
        msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(msg)
        assert window.statusBar().currentMessage() == msg

    def test_device_lost_message_shown_in_rx_panel(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_audio_device_lost must also update the RX panel status label."""
        msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(msg)
        assert window._rx_panel._status.text() == msg

    def test_device_lost_message_survives_rx_stopped(
        self, window: MainWindow, qapp
    ) -> None:
        """When stream_error fires before stopped, _on_rx_stopped must
        re-show the disconnect message, not 'Capture stopped.' / 'Ready'."""
        msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(msg)
        # Now the stopped signal fires (as it would after device-loss stop()).
        window._on_rx_stopped()

        assert window.statusBar().currentMessage() == msg
        assert window._rx_panel._status.text() == msg

    def test_device_lost_flag_cleared_after_rx_stopped(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_rx_stopped must clear _last_rx_disconnect_msg after consuming it
        so subsequent normal stops don't re-show the stale disconnect message."""
        msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(msg)
        window._on_rx_stopped()

        assert window._last_rx_disconnect_msg == ""

    def test_normal_stop_shows_not_listening(
        self, window: MainWindow, qapp
    ) -> None:
        """A deliberate stop (no device loss) must show the 'Not listening'
        message in the RX panel and 'Ready' in the status bar."""
        # No prior _on_audio_device_lost call.
        window._on_rx_stopped()

        assert "Not listening" in window._rx_panel._status.text()
        assert window.statusBar().currentMessage() == "Ready"

    def test_stream_error_wired_to_device_lost_slot(
        self, window: MainWindow, qapp
    ) -> None:
        """stream_error must be connected to _on_audio_device_lost, NOT
        _on_rx_error, so the message is stored for _on_rx_stopped to use."""
        msg = "Audio device disconnected — replug and click Start to recover"
        # Emit stream_error directly on the worker (direct call, synchronous).
        window._audio_worker.stream_error.emit(msg)
        # The stored flag confirms _on_audio_device_lost ran (not _on_rx_error,
        # which never sets this attribute).
        assert window._last_rx_disconnect_msg == msg

    # --- signal ordering race: stream_error arrives before started ---

    def test_late_rx_started_does_not_overwrite_disconnect_msg(
        self, window: MainWindow, qapp
    ) -> None:
        """If stream_error fires before started (race on disconnect during start),
        _on_rx_started must not overwrite the disconnect message in the UI."""
        msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(msg)
        # Simulate the late started arriving after stream_error.
        window._on_rx_started()

        assert window._rx_panel._status.text() == msg
        assert window.statusBar().currentMessage() == msg

    def test_late_rx_started_does_not_set_capture_running(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_rx_started after device loss must not set _capture_running=True."""
        window._on_audio_device_lost("disconnected")
        window._on_rx_started()
        assert not window._capture_running

    def test_late_rx_started_leaves_button_as_start_capture(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_rx_started after device loss must not flip the button to 'Stop Capture'."""
        window._on_audio_device_lost("disconnected")
        window._on_rx_started()
        assert window._rx_panel._start_btn.text() == "Start Capture"
        assert window._rx_panel._start_btn.isEnabled()

    # --- status_update gate ---

    def test_status_update_suppressed_after_stop(
        self, window: MainWindow, qapp
    ) -> None:
        """RxWorker status_update must be suppressed after _on_rx_stopped so
        the 'Listening' heartbeat cannot overwrite 'Not listening…'."""
        from open_sstv.ui.workers import RX_LISTENING

        window._on_rx_stopped()  # sets suppress flag + "Not listening…"
        window._on_rx_status_update(RX_LISTENING)  # periodic heartbeat
        assert "Not listening" in window._rx_panel._status.text()

    def test_status_update_suppressed_after_device_lost(
        self, window: MainWindow, qapp
    ) -> None:
        """RxWorker status_update must be suppressed after device loss."""
        from open_sstv.ui.workers import RX_LISTENING

        disconnect_msg = "Audio device disconnected — replug and click Start to recover"
        window._on_audio_device_lost(disconnect_msg)
        window._on_rx_status_update(RX_LISTENING)
        assert window._rx_panel._status.text() == disconnect_msg

    def test_status_update_allowed_during_capture(
        self, window: MainWindow, qapp
    ) -> None:
        """The listening heartbeat drives the animated indicator while
        capture is active (not suppressed)."""
        from open_sstv.ui.workers import RX_LISTENING

        window._on_rx_started()  # clears suppress flag
        window._on_rx_status_update(RX_LISTENING)
        assert "Listening" in window._rx_panel._status.text()

    def test_suppress_flag_cleared_on_rx_started(
        self, window: MainWindow, qapp
    ) -> None:
        """_on_rx_started must clear _suppress_rx_status_updates."""
        window._suppress_rx_status_updates = True
        window._on_rx_started()
        assert not window._suppress_rx_status_updates

    def test_capture_requested_clears_disconnect_msg(
        self, window: MainWindow, qtbot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clicking Start must clear _last_rx_disconnect_msg so the new session
        starts clean even if a prior session's signals are still in-flight."""
        window._last_rx_disconnect_msg = "stale disconnect message"
        # Patch reset so we don't need a real rx_thread.
        monkeypatch.setattr(window._rx_worker, "reset", lambda: None)
        window._on_capture_requested(True)
        assert window._last_rx_disconnect_msg == ""


# ---------------------------------------------------------------------------
# Bug fix: stream-open error message overwritten by "Not listening…" (OP-RX-02)
# ---------------------------------------------------------------------------


class TestStreamOpenErrorPersistence:
    """Verify that when InputStreamWorker.start() fails, the error message is
    still visible after _on_rx_stopped runs — not overwritten by "Not listening…".
    """

    def test_rx_audio_error_stored_when_not_capturing(
        self, window: MainWindow
    ) -> None:
        """_on_rx_audio_error must save the message when not capturing so
        _on_rx_stopped can re-show it."""
        window._capture_running = False
        window._on_rx_audio_error("Could not open input stream: [Errno -9996] Invalid device")
        assert "Could not open input stream" in window._last_rx_audio_error_msg

    def test_rx_audio_error_not_stored_when_capturing(
        self, window: MainWindow
    ) -> None:
        """Runtime audio errors (device pulled mid-capture) must NOT overwrite
        _last_rx_audio_error_msg — that field is only for start-time failures."""
        window._capture_running = True
        window._last_rx_audio_error_msg = ""
        window._on_rx_audio_error("runtime device error")
        assert window._last_rx_audio_error_msg == ""

    def test_rx_audio_error_shown_in_panel(self, window: MainWindow) -> None:
        """_on_rx_audio_error must immediately update the RX panel status."""
        window._capture_running = False
        window._on_rx_audio_error("no device found")
        assert "no device found" in window._rx_panel._status.text()

    def test_rx_stopped_shows_audio_error_not_not_listening(
        self, window: MainWindow
    ) -> None:
        """When _last_rx_audio_error_msg is set, _on_rx_stopped must re-show the
        error rather than overwriting with 'Not listening…'."""
        window._last_rx_audio_error_msg = "RX: Could not open input stream: stale index"
        window._on_rx_stopped()
        assert "Not listening" not in window._rx_panel._status.text()
        assert "Could not open input stream" in window._rx_panel._status.text()

    def test_rx_stopped_clears_audio_error_msg(self, window: MainWindow) -> None:
        """_on_rx_stopped must consume _last_rx_audio_error_msg so a subsequent
        normal stop shows 'Not listening…' as expected."""
        window._last_rx_audio_error_msg = "RX: some error"
        window._on_rx_stopped()
        assert window._last_rx_audio_error_msg == ""

    def test_capture_requested_clears_audio_error_msg(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clicking Start must clear _last_rx_audio_error_msg so a stale
        error from the previous attempt does not ghost into the next session."""
        window._last_rx_audio_error_msg = "RX: stale error from prior attempt"
        monkeypatch.setattr(window._rx_worker, "reset", lambda: None)
        window._on_capture_requested(True)
        assert window._last_rx_audio_error_msg == ""

    def test_capture_requested_clears_suppress_flag(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a stream-open failure left _suppress_rx_status_updates=True
        (because _on_rx_started never fired), clicking Start must clear it so
        the next session's status updates are visible."""
        window._suppress_rx_status_updates = True
        monkeypatch.setattr(window._rx_worker, "reset", lambda: None)
        window._on_capture_requested(True)
        assert not window._suppress_rx_status_updates

    @pytest.mark.skip(reason="flaky headless QThread timing — async signal chain races in headless PortAudio")
    def test_stream_open_fail_error_survives_stopped(
        self, window: MainWindow, qtbot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full-path: stream open fails → error + stopped queued → after both
        process, the error message is still visible (not 'Not listening…')."""
        import sounddevice as _sd

        monkeypatch.setattr(
            "open_sstv.audio.input_stream.sd.InputStream",
            MagicMock(side_effect=_sd.PortAudioError("Invalid device")),
        )

        window._on_capture_requested(True)

        # Wait for the full chain to settle (reset → start → fail → stopped).
        qtbot.waitUntil(
            lambda: window._rx_panel._start_btn.isEnabled(), timeout=2000
        )
        # Error message must be visible, not the generic "Not listening…".
        status_text = window._rx_panel._status.text()
        assert "Not listening" not in status_text, (
            f"Expected error message to persist, got: {status_text!r}"
        )
        assert "Invalid device" in status_text or "Could not open" in status_text, (
            f"Expected stream-open error in status, got: {status_text!r}"
        )


# ---------------------------------------------------------------------------
# Bug fix: deleteLater race → QObjectWrapper use-after-free (OP-CX-01)
# ---------------------------------------------------------------------------


class TestRigConnectLifecycle:
    """Verify that the rig-connect-thread cleanup does not race between
    Python GC and Qt's deleteLater, which causes a QObjectWrapper crash.
    """

    def test_worker_and_relay_refs_cleared_after_connect(
        self, window: MainWindow, qtbot
    ) -> None:
        """After a successful connect, all connect-thread refs must be None so
        Python GC is the sole owner and deleteLater is never in the picture."""
        fast_rig = MagicMock()
        fast_rig.open.return_value = None
        fast_rig.ping.return_value = None

        window._start_rig_connect_thread(fast_rig, lambda _: None, lambda _: None)
        qtbot.waitUntil(lambda: window._connect_thread is None, timeout=2000)

        assert window._connect_worker is None
        assert window._connect_relay is None
        assert window._connect_timeout_timer is None

    def test_two_sequential_connects_no_crash(
        self, window: MainWindow, qtbot
    ) -> None:
        """Two back-to-back connect attempts must both complete cleanly,
        proving the per-connect thread/worker/relay lifecycle is correct."""
        fast_rig = MagicMock()
        fast_rig.open.return_value = None
        fast_rig.ping.return_value = None

        results: list[str] = []

        def _on_success(_rig: object) -> None:
            results.append("ok")

        # First connect.
        window._start_rig_connect_thread(fast_rig, _on_success, lambda _: None)
        qtbot.waitUntil(lambda: len(results) == 1, timeout=2000)
        qtbot.waitUntil(lambda: window._connect_thread is None, timeout=1000)

        # Second connect — must not crash even though the first worker/relay
        # were already cleaned up by Python GC (not deleteLater).
        window._start_rig_connect_thread(fast_rig, _on_success, lambda _: None)
        qtbot.waitUntil(lambda: len(results) == 2, timeout=2000)
        qtbot.waitUntil(lambda: window._connect_thread is None, timeout=1000)

        assert results == ["ok", "ok"]

    def test_connect_thread_finished_cleanup_is_idempotent(
        self, window: MainWindow
    ) -> None:
        """Calling _on_connect_thread_finished when all refs are already None
        (e.g. after _abort_connect) must not raise."""
        window._connect_thread = None
        window._connect_worker = None
        window._connect_relay = None
        window._connect_timeout_timer = None
        # Must not raise.
        window._on_connect_thread_finished()


# ---------------------------------------------------------------------------
# Poll-worker auto-disconnect aborts any in-flight TX (OP-TX-02)
# ---------------------------------------------------------------------------


class TestRadioDisconnectedAbortsTx:
    """When _RigPollWorker fires radio_disconnected, any in-flight TX must
    be aborted immediately so audio doesn't leak to Mac speakers."""

    def test_radio_disconnected_calls_tx_request_stop(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_on_radio_disconnected must call tx_worker.request_stop() so the
        TX stop_event is set and sd.stop() is called immediately."""
        stop_calls: list[int] = []
        monkeypatch.setattr(window._tx_worker, "request_stop", lambda: stop_calls.append(1))

        # Simulate a non-ManualRig being connected so the guard doesn't return early.
        from unittest.mock import MagicMock
        fake_rig = MagicMock()
        fake_rig.close.return_value = None
        window._rig = fake_rig  # type: ignore[assignment]

        window._on_radio_disconnected()

        assert len(stop_calls) == 1, (
            "_on_radio_disconnected must call request_stop() to abort TX"
        )

    def test_radio_disconnected_is_noop_when_manual_rig(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_on_radio_disconnected must return early when already on ManualRig
        — the guard prevents double-fire from queued signals."""
        stop_calls: list[int] = []
        monkeypatch.setattr(window._tx_worker, "request_stop", lambda: stop_calls.append(1))

        from open_sstv.radio.base import ManualRig
        window._rig = ManualRig()

        window._on_radio_disconnected()

        assert len(stop_calls) == 0, (
            "request_stop() must not be called when already on ManualRig"
        )


# ---------------------------------------------------------------------------
# v0.3.12 — Export to Audio applies the TX banner stamp
# ---------------------------------------------------------------------------
#
# v0.3.10 added Export to Audio but bypassed TxWorker, so the banner stamp at
# workers.py:606 never ran and the resulting WAV decoded without a banner.
# v0.3.12 mirrors the same gating in MainWindow._on_export_to_audio_requested:
# banner only when tx_banner_enabled AND no v0.3 template composited (templates
# carry their own text overlays).


class TestExportToAudioBanner:
    """Pin the banner-application logic for Export to Audio."""

    @staticmethod
    def _stub_export(
        window: MainWindow,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> list[Image.Image]:
        """Wire Export to Audio so it runs synchronously and captures the
        image handed to ``OfflineEncodeWorker`` without doing a real encode."""
        out_path = tmp_path / "out.wav"
        monkeypatch.setattr(
            "PySide6.QtWidgets.QFileDialog.getSaveFileName",
            staticmethod(
                lambda *a, **kw: (str(out_path), "WAV audio (*.wav)")
            ),
        )

        captured: list[Image.Image] = []

        class _FakeEncodeWorker:
            """Captures the image arg; emits nothing so the test
            doesn't have to wait on a real Robot 36 encode."""

            encode_complete: object = None
            error: object = None
            finished: object = None

            def __init__(self, image, mode, fs, out_path):  # noqa: ARG002
                captured.append(image)
                # Provide the same Qt-Signal-shaped attributes the production
                # MainWindow expects to connect to — a no-op MagicMock for
                # ``connect`` is enough since we never start the thread.
                self.encode_complete = MagicMock()
                self.error = MagicMock()
                self.finished = MagicMock()

            def moveToThread(self, _thread) -> None:  # noqa: N802 — Qt API
                pass

            def deleteLater(self) -> None:  # noqa: N802 — Qt API
                pass

            def run(self) -> None:
                pass

        monkeypatch.setattr(
            "open_sstv.ui.main_window.OfflineEncodeWorker", _FakeEncodeWorker
        )

        # Suppress the real QThread so we don't actually start one.
        class _FakeThread:
            def __init__(self, _parent=None) -> None:
                self.finished = MagicMock()
                self.started = MagicMock()

            def setObjectName(self, _name) -> None:  # noqa: N802
                pass

            def start(self) -> None:
                pass

            def quit(self) -> None:
                pass

            def wait(self, _timeout=0) -> bool:  # noqa: ARG002
                return True

            def deleteLater(self) -> None:  # noqa: N802
                pass

        monkeypatch.setattr("open_sstv.ui.main_window.QThread", _FakeThread)

        return captured

    def test_banner_applied_when_enabled_and_no_template(
        self,
        window: MainWindow,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        captured = self._stub_export(window, monkeypatch, tmp_path)
        window._config.tx_banner_enabled = True
        window._config.callsign = "W0AEZ"

        # Spy on the banner-stamp call.
        banner_calls: list[int] = []
        from open_sstv.core import banner as _banner_mod
        real_apply = _banner_mod.apply_tx_banner

        def _spy(image, *args, **kwargs):  # noqa: ANN001
            banner_calls.append(1)
            return real_apply(image, *args, **kwargs)

        # Patch the import target inside main_window's slot (which does
        # ``from open_sstv.core.banner import apply_tx_banner`` at call time).
        monkeypatch.setattr(_banner_mod, "apply_tx_banner", _spy)

        from open_sstv.core.modes import Mode
        img = Image.new("RGB", (320, 240), color=(100, 200, 50))
        window._on_export_to_audio_requested(img, Mode.ROBOT_36)

        assert len(banner_calls) == 1, "banner should have been applied once"
        assert len(captured) == 1, "encode worker should have been constructed"
        # The captured image should be the banner-stamped one, not the raw input.
        # apply_tx_banner returns same-dimensions but different pixels in the
        # top strip — so the top-left pixel should not be the source color.
        assert captured[0].getpixel((0, 0)) != (100, 200, 50), (
            "top-left pixel should be in the banner strip, not the source color"
        )

    def test_banner_skipped_when_disabled(
        self,
        window: MainWindow,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        captured = self._stub_export(window, monkeypatch, tmp_path)
        window._config.tx_banner_enabled = False

        banner_calls: list[int] = []
        from open_sstv.core import banner as _banner_mod
        monkeypatch.setattr(
            _banner_mod,
            "apply_tx_banner",
            lambda *a, **kw: banner_calls.append(1),
        )

        from open_sstv.core.modes import Mode
        img = Image.new("RGB", (320, 240), color=(100, 200, 50))
        window._on_export_to_audio_requested(img, Mode.ROBOT_36)

        assert banner_calls == [], "banner must not be applied when disabled"
        assert len(captured) == 1
        # Image should pass through unchanged.
        assert captured[0].getpixel((0, 0)) == (100, 200, 50)

    def test_banner_applied_regardless_of_template_state(
        self,
        window: MainWindow,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """v0.3.13 policy pin: when banner is enabled in Settings, it
        stamps even if a v0.3 template is selected.  Previously (v0.3.12)
        the template-active state suppressed the banner; that gate was
        removed per user feedback — banner-on means banner-always-on.
        """
        captured = self._stub_export(window, monkeypatch, tmp_path)
        window._config.tx_banner_enabled = True
        window._config.callsign = "W0AEZ"

        # Place a fake template into the panel to simulate "template selected".
        # The new policy ignores this — banner should still stamp.
        from open_sstv.templates.model import Template
        window._tx_panel._selected_template = Template(
            name="Fake", role="cq", layers=[]
        )

        banner_calls: list[int] = []
        from open_sstv.core import banner as _banner_mod
        real_apply = _banner_mod.apply_tx_banner

        def _spy(image, *args, **kwargs):  # noqa: ANN001
            banner_calls.append(1)
            return real_apply(image, *args, **kwargs)

        monkeypatch.setattr(_banner_mod, "apply_tx_banner", _spy)

        from open_sstv.core.modes import Mode
        img = Image.new("RGB", (320, 240), color=(100, 200, 50))
        window._on_export_to_audio_requested(img, Mode.ROBOT_36)

        assert len(banner_calls) == 1, (
            "banner must stamp even with a template selected (v0.3.13)"
        )
        assert len(captured) == 1
        # Banner overwrites the top strip — top-left pixel is now banner bg.
        assert captured[0].getpixel((0, 0)) != (100, 200, 50)



class TestRemoteStatusIndicator:
    """Phase 2: a persistent status-bar indicator reflects whether the
    embedded remote server is running."""

    def test_hidden_when_server_not_running(self, window: MainWindow) -> None:
        assert window._remote_status_label.isVisible() is False

    def test_shown_after_indicator_lit(self, window: MainWindow) -> None:
        window.show()
        window._show_remote_indicator("http://0.0.0.0:8730/?token=demo")
        assert window._remote_status_label.isVisible() is True
        # Click-through link opens on this machine (loopback), not 0.0.0.0.
        assert "127.0.0.1:8730" in window._remote_status_label.text()
        assert "0.0.0.0" not in window._remote_status_label.text()
        # Tooltip preserves the real bind for LAN devices.
        assert "0.0.0.0:8730" in window._remote_status_label.toolTip()

    def test_hidden_again_after_stop(self, window: MainWindow) -> None:
        window.show()
        window._show_remote_indicator("http://127.0.0.1:8730/?token=demo")
        window._stop_remote_server()
        assert window._remote_status_label.isVisible() is False


class TestRemoteTxWiring:
    """Phase 3c: the control plane's key/unkey callbacks are wired to the
    real TX worker, with the GUI-thread state guard that stops a stale key
    from surviving an abort."""

    def test_unkey_stops_the_worker(self, window: MainWindow) -> None:
        window._tx_worker.request_stop = MagicMock()  # type: ignore[method-assign]
        window._remote_tx_unkey("heartbeat_lost")
        window._tx_worker.request_stop.assert_called_once()

    def test_unkey_drops_ptt_directly_and_first(self, window: MainWindow) -> None:
        # The dead-man's-switch must not rely on the audio worker unwinding
        # to drop PTT: a worker wedged in a blocking write() would never
        # reach its finally-block set_ptt(False).  So emergency_unkey (a
        # direct PTT-off) fires, and BEFORE request_stop, so the transmitter
        # unkeys even if stopping the audio hangs.
        calls: list[str] = []
        window._tx_worker.emergency_unkey = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda: calls.append("emergency_unkey")
        )
        window._tx_worker.request_stop = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda: calls.append("request_stop")
        )
        window._remote_tx_unkey("heartbeat_lost")
        window._tx_worker.emergency_unkey.assert_called_once()
        assert calls == ["emergency_unkey", "request_stop"], (
            "PTT must be dropped directly before (and independent of) "
            "stopping the audio worker"
        )

    def test_request_ignored_when_not_transmitting(self, window: MainWindow) -> None:
        # The GUI-thread guard: if the control plane isn't TRANSMITTING
        # (e.g. an abort landed first), the rig must not be keyed.
        spy = MagicMock()
        window._request_transmit.connect(spy)
        window._on_remote_tx_request("some-id", "martin_m1")
        spy.assert_not_called()

    def test_request_keys_rig_when_transmitting(
        self, window: MainWindow, tmp_path: Path
    ) -> None:
        img_path = tmp_path / "a.png"
        Image.new("RGB", (32, 24), (1, 2, 3)).save(img_path)
        window._remote_service.image_path = MagicMock(return_value=img_path)  # type: ignore[method-assign]
        window._config.remote_tx_enabled = True
        window._rig = MagicMock()  # a connected (non-Manual) rig — remote TX gate
        cp = window._remote_control
        assert cp.take_lease("A").ok
        tok = cp.request("A", "some-id", "martin_m1").token
        assert cp.confirm("A", tok or "").ok  # -> TRANSMITTING
        assert cp.status()["state"] == "transmitting"

        spy = MagicMock()
        window._request_transmit.connect(spy)
        window._on_remote_tx_request("some-id", "martin_m1")
        assert spy.call_count == 1
        from open_sstv.core.modes import Mode
        assert spy.call_args[0][1] == Mode.MARTIN_M1
