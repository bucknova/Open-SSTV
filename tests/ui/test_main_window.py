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

    def _make_worker(self) -> "_RigPollWorker":
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
        """When the user clicks Start, MainWindow must look up the configured
        input device by name so a replug (new PortAudio index) is handled
        transparently — the stale cached index is not passed to start().
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
        original_start = window._audio_worker.start

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
