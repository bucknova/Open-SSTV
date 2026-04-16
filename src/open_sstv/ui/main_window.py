# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Composes the TX panel (left), RX panel (right), three worker threads
(TX playback, RX audio capture, RX decode), a menu bar (File > Settings /
Quit), optional rig polling on a 1 Hz QTimer, and auto-save of decoded
images to the configured directory.

Threading
---------

Five threads total:

* GUI thread — owns the window, the two panels, and Qt's event loop.
* TX worker thread — owns ``TxWorker``, runs encode + ``play_blocking``.
* RX audio thread — owns ``InputStreamWorker``, runs PortAudio + queue
  drain timer.
* RX decode thread — owns ``RxWorker``, runs ``Decoder.feed`` on
  flushed batches.
* Rig poll thread — owns ``_RigPollWorker``, runs blocking get_freq /
  get_mode / get_strength calls so the GUI never stalls on serial I/O.

Splitting audio capture from decoding is deliberate: a slow decode
pass must not stall the PortAudio queue drain. Each worker runs on a
dedicated ``QThread`` with its own event loop, and signals cross the
boundaries via Qt's automatic queued connections.

Signal flow
-----------

TX (unchanged from Phase 1)::

    tx_panel.transmit_requested ──> tx_worker.transmit
    tx_panel.stop_requested     ──> _on_stop_requested (direct, UI thread)
    tx_worker.transmission_*    ──> _on_tx_*        ──> tx_panel
    tx_worker.error             ──> _on_tx_error    ──> status bar

RX::

    rx_panel.capture_requested  ──> _on_capture_requested (UI thread)
        (True)  ──> audio_worker.start
        (False) ──> audio_worker.stop

    audio_worker.started    ──> rx_panel.set_capturing(True)
    audio_worker.stopped    ──> rx_worker.flush + rx_panel.set_capturing(False)
    audio_worker.chunk_ready──> rx_worker.feed_chunk
    audio_worker.error      ──> _on_rx_error  ──> status bar

    rx_panel.clear_requested──> rx_worker.reset + rx_panel.clear
    rx_worker.image_started ──> rx_panel.show_image_started
    rx_worker.image_complete──> rx_panel.show_image_complete
    rx_worker.error         ──> _on_rx_error

The ``stopped → flush`` wire is what makes the tail of an in-flight
image decode even when the user clicks Stop mid-transmission: the
audio worker drains its queue before emitting ``stopped``, and
``RxWorker.flush`` forces any buffered scratch samples through the
decoder one last time.

Lifecycle
---------

``closeEvent`` tears everything down in a safe order: stop TX, stop RX
audio capture, flush the decoder, quit and join all three worker
threads, and finally close the rig.
"""
from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from open_sstv.audio.devices import (
    AudioDevice,
    find_input_device_by_name,
    find_output_device_by_name,
)
from open_sstv import __version__
from open_sstv.audio.input_stream import (
    DEFAULT_BLOCKSIZE,
    InputStreamWorker,
)
from open_sstv.config.schema import AppConfig
from open_sstv.config.store import load_config, save_config
from open_sstv.config.templates import load_templates
from open_sstv.radio.base import ManualRig, Rig, RigConnectionMode
from open_sstv.radio.exceptions import RigError
from open_sstv.radio.rigctld import RigctldClient, is_safe_rigctld_arg
from open_sstv.radio.serial_rig import create_serial_rig
from open_sstv.ui.radio_panel import RadioPanel
from open_sstv.ui.rx_panel import RxPanel
from open_sstv.ui.settings_dialog import SettingsDialog
from open_sstv.ui.tx_panel import TxPanel
from open_sstv.core.modes import Mode
from open_sstv.ui.workers import RxWorker, TxWorker

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class _RigPollWorker(QObject):
    """Polls the rig for frequency, mode, and S-meter on a dedicated thread.

    The ``poll`` slot blocks on serial/socket I/O; by running on its own
    ``QThread`` it cannot freeze the GUI regardless of connection quality.
    The GUI thread fires the 1 Hz ``_rig_poll_timer``; that timer's
    ``timeout`` signal is connected here via a queued connection so the
    actual blocking call happens on this object's thread.
    """

    poll_result = Signal(int, str, int)  # (freq_hz, mode_name, strength_db)
    poll_error = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rig: Rig = ManualRig()

    def set_rig(self, rig: Rig) -> None:
        """Swap the rig reference. GIL-safe for a plain attribute store."""
        self._rig = rig

    @Slot()
    def poll(self) -> None:
        """Read freq/mode/strength from the rig. Blocks; runs on worker thread."""
        try:
            freq = self._rig.get_freq()
            mode_name, _ = self._rig.get_mode()
            strength = self._rig.get_strength()
        except RigError:
            self.poll_error.emit()
            return
        self.poll_result.emit(freq, mode_name, strength)


class MainWindow(QMainWindow):
    """The Phase 2 main window: TX + RX side-by-side, three worker threads."""

    #: Private signals used to dispatch ``start``/``stop`` calls onto
    #: the audio worker thread. We can't just call
    #: ``self._audio_worker.start(...)`` directly — that would run the
    #: PortAudio open on the UI thread. Emitting via signals means Qt's
    #: auto-connect promotes the call to a ``QueuedConnection`` and the
    #: slot executes on the worker thread's event loop.
    _request_start_capture = Signal(object, int, int)
    _request_stop_capture = Signal()
    #: Routes the "Clear" action to RxWorker.reset() via a queued connection
    #: so the reset runs on the RX decode thread, not the GUI thread.
    _request_rx_reset = Signal()
    #: Dispatch TX calls to the TX worker thread via queued connection.
    #: Direct method calls from the GUI thread would run on the wrong thread.
    _request_transmit = Signal(object, object)  # (PIL.Image, Mode)
    _request_test_tone = Signal()
    #: Gates the RX decoder on/off during TX (queued → RxWorker thread).
    _request_rx_gate = Signal(bool)
    #: Settings-change dispatchers — queued to RxWorker so decoder rebuilds
    #: happen on the worker thread, never racing with feed_chunk.
    _rx_weak_signal_changed = Signal(bool)
    _rx_incremental_decode_changed = Signal(bool)
    _rx_sample_rate_changed = Signal(int)
    #: OP-09: cover the previously-direct-call settings too so every
    #: per-worker setting flows through a queued connection on its
    #: receiver's event loop.  Symmetry > convenience.
    _rx_final_slant_correction_changed = Signal(bool)
    _tx_sample_rate_changed = Signal(int)

    def __init__(
        self,
        rig: Rig | None = None,
        output_device: AudioDevice | int | None = None,
        input_device: AudioDevice | int | None = None,
        config: AppConfig | None = None,
        parent: QMainWindow | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open-SSTV")
        self.resize(1100, 640)

        self._config = config if config is not None else load_config()

        # Rig starts as ManualRig (no-op). The user clicks "Connect Rig"
        # in the radio panel to establish a live rigctld link; the settings
        # dialog configures host/port.
        self._rig: Rig = rig if rig is not None else ManualRig()

        # Resolve saved device names from config to real AudioDevice objects.
        # If the caller passed explicit devices, use those; otherwise look up
        # what the user last selected in Settings.
        # OP-18: track whether a saved-but-missing device fell back to the
        # system default so we can surface a status-bar notice once the
        # status bar exists later in __init__.
        self._missing_devices: list[str] = []
        if output_device is None:
            output_device = find_output_device_by_name(
                self._config.audio_output_device
            )
            if output_device is None and self._config.audio_output_device:
                self._missing_devices.append(
                    f"output '{self._config.audio_output_device}'"
                )
        if input_device is None:
            input_device = find_input_device_by_name(
                self._config.audio_input_device
            )
            if input_device is None and self._config.audio_input_device:
                self._missing_devices.append(
                    f"input '{self._config.audio_input_device}'"
                )
        self._input_device = input_device
        self._rigctld_proc: "subprocess.Popen | None" = None
        self._capture_running: bool = False
        self._last_abort_was_watchdog: bool = False
        #: Watchdog budget (seconds) of the most recently fired TX watchdog,
        #: forwarded by ``TxWorker.watchdog_fired``.  Used by
        #: ``_on_tx_aborted`` to format a precise "exceeded N s" message
        #: instead of hardcoding the value (the budget is now per-
        #: transmission, see ``_compute_playback_watchdog_s``).
        self._last_watchdog_duration_s: float = 0.0
        self._last_tx_was_test_tone: bool = False

        # --- Menu bar ---
        self._build_menu_bar()

        # --- Radio panel (toolbar strip above TX/RX) ---
        self._radio_panel = RadioPanel(self)
        self._radio_panel.set_callsign(self._config.callsign)
        self._radio_panel.connect_requested.connect(self._on_rig_connect)
        self._radio_panel.disconnect_requested.connect(self._on_rig_disconnect)

        # Push callsign to TX panel for the image editor's text overlay
        self._tx_panel = TxPanel(
            templates=load_templates(),
            default_mode=self._config.default_tx_mode,
            parent=self,
        )
        self._tx_panel.set_callsign(self._config.callsign)

        # --- Panels inside a horizontal splitter ---
        self._rx_panel = RxPanel(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._tx_panel)
        splitter.addWidget(self._rx_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        # Stack radio panel + splitter into the central widget.
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._radio_panel)
        central_layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        # --- TX worker on its own thread ---
        self._tx_thread = QThread(self)
        self._tx_thread.setObjectName("sstv-app-tx-worker")
        self._tx_worker = TxWorker(
            rig=self._rig,
            output_device=output_device,
            sample_rate=self._config.sample_rate,
            ptt_delay_s=self._config.ptt_delay_s,
        )
        # v0.1.33: seed worker state from the persisted config BEFORE
        # moveToThread so the workers are born in the state the user
        # left them in.  Prior to this, ``_apply_config`` only ran on
        # Settings dialog save, so the first launch after a fresh edit
        # ignored every field that doesn't have a constructor kwarg
        # (output gain, CW ID, TX banner, etc).  User-reported as "the
        # app does not respect previously set mic gain levels."
        # Direct setter calls while the worker is still on the GUI
        # thread avoid the queued-signal-during-teardown segfault that
        # emitting via ``_apply_config`` from ``__init__`` produced.
        self._tx_worker.set_output_gain(self._config.audio_output_gain)
        self._tx_worker.set_cw_id(
            self._config.cw_id_enabled,
            self._config.callsign,
            self._config.cw_id_wpm,
            self._config.cw_id_tone_hz,
        )
        self._tx_worker.set_tx_banner(
            self._config.tx_banner_enabled,
            self._config.callsign,
            self._config.tx_banner_bg_color,
            self._config.tx_banner_text_color,
            self._config.tx_banner_size,
        )
        self._tx_worker.moveToThread(self._tx_thread)
        # Standard Qt cleanup pattern: when the thread finishes, schedule
        # the worker for deletion. Without this the worker is left as an
        # orphaned QObject on a dead thread, and Qt's queued-connection
        # machinery hits "current thread's event dispatcher has already
        # been destroyed" warnings during app shutdown.
        self._tx_thread.finished.connect(self._tx_worker.deleteLater)
        self._tx_thread.start()

        # --- RX audio worker on its own thread ---
        self._audio_thread = QThread(self)
        self._audio_thread.setObjectName("sstv-app-rx-audio")
        self._audio_worker = InputStreamWorker()
        self._audio_worker.moveToThread(self._audio_thread)
        self._audio_thread.finished.connect(self._audio_worker.deleteLater)
        self._audio_thread.start()

        # --- RX decode worker on its own thread ---
        self._rx_thread = QThread(self)
        self._rx_thread.setObjectName("sstv-app-rx-decode")
        self._rx_worker = RxWorker(
            sample_rate=self._config.sample_rate,
            weak_signal=self._config.rx_weak_signal_mode,
            final_slant_correction=self._config.apply_final_slant_correction,
            incremental_decode=self._config.incremental_decode,
        )
        # v0.1.33: seed RX input gain from config BEFORE moveToThread
        # (see matching TX block above for the full rationale).  This is
        # the direct user-reported symptom — "the app does not respect
        # previously set mic gain levels" — because audio_input_gain
        # wasn't a RxWorker constructor kwarg and the only code path
        # that pushed it to the worker was ``_apply_config``, which
        # only fired when the user re-opened Settings.
        self._rx_worker.set_input_gain(self._config.audio_input_gain)
        self._rx_worker.moveToThread(self._rx_thread)
        self._rx_thread.finished.connect(self._rx_worker.deleteLater)
        self._rx_thread.start()

        # --- Wire TX signals ---
        # panel → flag-setter on GUI thread, then dispatch via queued signal
        self._tx_panel.transmit_requested.connect(self._on_transmit_requested)
        self._tx_panel.stop_requested.connect(self._on_stop_requested)
        self._radio_panel.test_tone_requested.connect(self._on_test_tone_requested)
        # Private dispatch signals → worker slots (QueuedConnection across thread)
        self._request_transmit.connect(self._tx_worker.transmit)
        self._request_test_tone.connect(self._tx_worker.transmit_test_tone)
        self._request_rx_gate.connect(self._rx_worker.set_tx_active)
        self._tx_worker.transmission_started.connect(self._on_tx_started)
        self._tx_worker.transmission_progress.connect(self._tx_panel.show_tx_progress)
        self._tx_worker.transmission_progress.connect(self._on_tx_progress)
        self._tx_worker.transmission_complete.connect(self._on_tx_complete)
        self._tx_worker.transmission_aborted.connect(self._on_tx_aborted)
        self._tx_worker.watchdog_fired.connect(self._on_watchdog_fired)
        self._tx_worker.error.connect(self._on_tx_error)

        # --- Wire RX signals ---
        # Private start/stop dispatch signals → audio worker slots.
        # Cross-thread, so Qt auto-promotes these to QueuedConnection
        # and the PortAudio open runs on the audio worker thread.
        self._request_start_capture.connect(self._audio_worker.start)
        self._request_stop_capture.connect(self._audio_worker.stop)
        self._request_rx_reset.connect(self._rx_worker.reset)
        # Settings dispatchers — connect BEFORE _apply_config is ever called.
        # Because rx_worker lives on rx_thread, Qt auto-promotes these to
        # QueuedConnection, so the decoder rebuilds happen on the worker thread.
        self._rx_weak_signal_changed.connect(self._rx_worker.set_weak_signal)
        self._rx_incremental_decode_changed.connect(self._rx_worker.set_incremental_decode)
        self._rx_sample_rate_changed.connect(self._rx_worker.set_sample_rate)
        # OP-09: previously-direct calls now flow through queued signals too.
        self._rx_final_slant_correction_changed.connect(
            self._rx_worker.set_final_slant_correction
        )
        self._tx_sample_rate_changed.connect(self._tx_worker.set_sample_rate)

        # Panel -> window (we translate capture_requested into the
        # dispatch signals above, because ``start`` needs the device
        # argument the panel doesn't know about).
        self._rx_panel.capture_requested.connect(self._on_capture_requested)
        self._rx_panel.clear_requested.connect(self._on_rx_clear)
        self._rx_panel.image_saved.connect(self._on_rx_image_saved)

        # Audio worker -> RX worker (chunks flow across the thread
        # boundary via queued connection; Qt handles the marshalling).
        self._audio_worker.chunk_ready.connect(self._rx_worker.feed_chunk)
        # Tail flush: when audio stops, force whatever's left in the
        # scratch buffer through the decoder so the last sub-second of
        # an in-flight image isn't discarded.
        self._audio_worker.stopped.connect(self._rx_worker.flush)
        # Audio -> UI.
        self._audio_worker.started.connect(self._on_rx_started)
        self._audio_worker.stopped.connect(self._on_rx_stopped)
        self._audio_worker.error.connect(self._on_rx_error)
        self._audio_worker.stream_error.connect(self._on_rx_error)

        # RX worker -> UI.
        self._rx_worker.image_started.connect(self._rx_panel.show_image_started)
        self._rx_worker.image_progress.connect(self._rx_panel.show_image_progress)
        self._rx_worker.image_complete.connect(self._rx_panel.show_image_complete)
        self._rx_worker.image_complete.connect(self._on_rx_image_complete)
        self._rx_worker.status_update.connect(self._rx_panel.set_status)
        self._rx_worker.error.connect(self._on_rx_error)

        # --- Rig poll: lightweight 1 Hz timer on GUI thread dispatches to
        #     _RigPollWorker on its own thread so blocking serial/socket calls
        #     never stall the event loop. ---
        self._rig_poll_timer = QTimer(self)
        self._rig_poll_timer.setInterval(1000)

        self._rig_poll_thread = QThread(self)
        self._rig_poll_thread.setObjectName("sstv-app-rig-poll")
        self._rig_poll_worker = _RigPollWorker()
        self._rig_poll_worker.moveToThread(self._rig_poll_thread)
        self._rig_poll_thread.finished.connect(self._rig_poll_worker.deleteLater)
        self._rig_poll_thread.start()

        # Queued connection: timeout fires on GUI thread → slot runs on poll thread.
        self._rig_poll_timer.timeout.connect(self._rig_poll_worker.poll)
        self._rig_poll_worker.poll_result.connect(self._on_poll_result)
        self._rig_poll_worker.poll_error.connect(self._radio_panel.set_connection_error)

        # --- Keyboard shortcuts ---
        save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        save_shortcut.activated.connect(self._on_save_shortcut)

        # v0.1.33: the TX/RX panel-level defaults (sample-rate label,
        # default TX mode) were previously only applied through
        # ``_apply_config`` on Settings save.  Seeding them directly
        # here so the first launch already reflects the persisted
        # config without emitting queued cross-thread signals from
        # __init__ (which caused a teardown-race segfault in tests).
        self._tx_panel.set_sample_rate(self._config.sample_rate)
        self._tx_panel.set_default_mode(self._config.default_tx_mode)

        # OP-18: surface saved-but-missing audio devices so the user
        # knows their previously-selected device fell back to system
        # default rather than silently using the wrong one.
        if self._missing_devices:
            self.statusBar().showMessage(
                f"Saved audio device(s) not found: {', '.join(self._missing_devices)}"
                " — using system default. Open Settings → Audio to reselect.",
                10000,
            )
        else:
            self.statusBar().showMessage("Ready")

    # === Menu bar ===

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        settings_action = QAction("&Settings…", self)
        # NoRole prevents macOS from moving this into the app menu and
        # leaving File empty (which hides the entire menu).
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)
        # Keep a reference so TX start/stop can enable/disable it.
        self._settings_action = settings_action

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = mb.addMenu("&Help")
        about_action = QAction("&About Open-SSTV", self)
        about_action.setMenuRole(QAction.MenuRole.NoRole)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    @Slot()
    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Open-SSTV",
            f"<h3>Open-SSTV v{__version__}</h3>"
            "<p>Open-source SSTV transceiver for amateur radio.</p>"
            "<p>22 modes: Robot 36, Martin M1/M2/M3/M4, Scottie S1/S2/S3/S4/DX, "
            "PD-50/90/120/160/180/240/290, Wraase SC2-120/SC2-180, Pasokon P3/P5/P7.</p>"
            "<p>Created by Kevin &mdash; W0AEZ</p>"
            '<p><a href="https://github.com/bucknova/Open-SSTV">'
            "github.com/bucknova/Open-SSTV</a></p>"
            "<p>GPL-3.0-or-later</p>",
        )

    @Slot()
    def _open_settings(self) -> None:
        # Capture current gain so Cancel can revert any live-pushed value.
        _original_output_gain = self._config.audio_output_gain
        dlg = SettingsDialog(
            self._config,
            rig_connected=self._radio_panel.connected,
            parent=self,
        )
        # Route Test Tone from the dialog through the same path as the Radio
        # panel button (queued signal → TxWorker on its own thread).
        dlg.test_tone_requested.connect(self._on_test_tone_requested)
        # Keep the dialog's button in sync with live TX state while it's open.
        self._tx_worker.transmission_started.connect(dlg.on_tx_started)
        self._tx_worker.transmission_complete.connect(dlg.on_tx_ended)
        self._tx_worker.transmission_aborted.connect(dlg.on_tx_ended)
        self._tx_worker.error.connect(dlg.on_tx_error)
        # Store lambda references so the finally block can disconnect them
        # explicitly — you can't disconnect an anonymous lambda by identity.
        _gain_lambda = lambda gain: self._tx_worker.set_output_gain(gain)  # noqa: E731
        _revert_lambda = lambda: self._tx_worker.set_output_gain(_original_output_gain)  # noqa: E731
        # Live-push TX gain on each slider tick (no disk write).
        dlg.output_gain_changed.connect(_gain_lambda)
        # Revert the live gain if the user cancels.
        dlg.rejected.connect(_revert_lambda)
        try:
            result = dlg.exec()
        finally:
            # Disconnect ALL wired signals immediately.  The dialog is about
            # to go out of scope; if tx_worker → dlg connections linger,
            # PySide6's C++ side holds a stale reference and the
            # QDialogWrapper destructor segfaults during Python finalization
            # (atexit → destroyQCoreApplication → PySide::destructionVisitor
            # on an already-freed Python wrapper).  The try/finally guarantees
            # the disconnects fire even if exec() raises.
            # tx_worker → dlg (emitter outlives dialog)
            self._tx_worker.transmission_started.disconnect(dlg.on_tx_started)
            self._tx_worker.transmission_complete.disconnect(dlg.on_tx_ended)
            self._tx_worker.transmission_aborted.disconnect(dlg.on_tx_ended)
            self._tx_worker.error.disconnect(dlg.on_tx_error)
            # dlg → self (emitter dies with dialog, but disconnect for symmetry)
            dlg.test_tone_requested.disconnect(self._on_test_tone_requested)
            dlg.output_gain_changed.disconnect(_gain_lambda)
            dlg.rejected.disconnect(_revert_lambda)

        if result == SettingsDialog.DialogCode.Accepted:
            old_input_device = self._config.audio_input_device
            old_sample_rate = self._config.sample_rate

            self._config = dlg.result_config()
            # Adopt any rigctld process the dialog launched before trying to
            # persist, so a save failure doesn't orphan the process.
            if dlg.rigctld_process is not None:
                self._rigctld_proc = dlg.rigctld_process
            # Always apply to in-memory state so the session works even if
            # the disk write fails.
            self._apply_config()
            try:
                save_config(self._config)
            except OSError as exc:
                self.statusBar().showMessage(
                    f"Settings applied but could not be saved to disk: {exc}", 8000
                )
                return

            # If audio input device or sample rate changed while capture is
            # active, the running stream is still using the old settings.
            # Notify the user — we don't auto-restart because that would
            # discard any partially decoded in-flight image.
            audio_restart_needed = (
                self._config.audio_input_device != old_input_device
                or self._config.sample_rate != old_sample_rate
            )
            if audio_restart_needed and self._capture_running:
                self.statusBar().showMessage(
                    "Audio settings changed — restart capture to apply.", 5000
                )
            else:
                self.statusBar().showMessage("Settings saved.", 3000)

    def _apply_config(self) -> None:
        """Push the current ``_config`` into all live workers and UI elements.

        Called both on a successful settings save and on a disk-write failure
        so the session always reflects the user's latest choices.
        """
        self._radio_panel.set_callsign(self._config.callsign)
        self._tx_panel.set_callsign(self._config.callsign)
        self._tx_panel.set_default_mode(self._config.default_tx_mode)
        new_output = find_output_device_by_name(self._config.audio_output_device)
        self._tx_worker.set_output_device(new_output)
        self._tx_worker.set_output_gain(self._config.audio_output_gain)
        self._tx_worker.set_ptt_delay(self._config.ptt_delay_s)
        new_input = find_input_device_by_name(self._config.audio_input_device)
        self._input_device = new_input
        self._rx_worker.set_input_gain(self._config.audio_input_gain)
        # Emit via queued signals so decoder rebuilds happen on the worker
        # thread, not the GUI thread (H-02 fix; OP-09 extended to cover
        # set_final_slant_correction too).
        self._rx_weak_signal_changed.emit(self._config.rx_weak_signal_mode)
        self._rx_final_slant_correction_changed.emit(
            self._config.apply_final_slant_correction
        )
        self._rx_incremental_decode_changed.emit(self._config.incremental_decode)
        self._tx_worker.set_tx_banner(
            self._config.tx_banner_enabled,
            self._config.callsign,
            self._config.tx_banner_bg_color,
            self._config.tx_banner_text_color,
            self._config.tx_banner_size,
        )
        self._tx_worker.set_cw_id(
            self._config.cw_id_enabled,
            self._config.callsign,
            self._config.cw_id_wpm,
            self._config.cw_id_tone_hz,
        )
        # Propagate sample rate to both workers (takes effect on the next
        # encode/capture start).  Both go via queued signals so the
        # change lands on the receiving worker's own event loop (OP-09).
        self._tx_sample_rate_changed.emit(self._config.sample_rate)
        self._rx_sample_rate_changed.emit(self._config.sample_rate)
        # TX panel needs the rate too so the progress-bar elapsed/total
        # seconds label is correct at 44.1 kHz (OP-06).
        self._tx_panel.set_sample_rate(self._config.sample_rate)

    # === TX slots ===

    @Slot(object, object)
    def _on_transmit_requested(self, image: "PILImage", mode: "Mode") -> None:
        """Set the test-tone flag and dispatch via queued signal to TX thread."""
        self._last_tx_was_test_tone = False
        self._request_transmit.emit(image, mode)

    @Slot()
    def _on_test_tone_requested(self) -> None:
        """Set the test-tone flag and dispatch via queued signal to TX thread."""
        self._last_tx_was_test_tone = True
        self._request_test_tone.emit()

    @Slot()
    def _on_stop_requested(self) -> None:
        self._tx_worker.request_stop()

    @Slot(int, int)
    def _on_tx_progress(self, samples_played: int, samples_total: int) -> None:
        """Update the status bar countdown during a test-tone transmission."""
        if not self._last_tx_was_test_tone or samples_total <= 0:
            return
        remaining_s = max(
            0, int((samples_total - samples_played) / self._config.sample_rate)
        )
        self.statusBar().showMessage(
            f"Transmitting test tone… {remaining_s}s remaining"
        )

    @Slot()
    def _on_tx_started(self) -> None:
        self._tx_panel.set_transmitting(True)
        if self._last_tx_was_test_tone:
            self._tx_panel.set_status("Transmitting test tone…")
            self.statusBar().showMessage("Transmitting test tone…")
        else:
            self._tx_panel.set_status("Transmitting…")
            self.statusBar().showMessage("Transmitting")
        # Lock rig controls for the duration of the transmission so the user
        # can't swap or disconnect the rig while PTT is keyed.
        self._radio_panel.set_tx_active(True)
        self._settings_action.setEnabled(False)
        # Gate the RX decoder to prevent self-decode through loopback (R-2).
        self._request_rx_gate.emit(True)
        self._rx_panel.set_status("RX paused during TX.")

    def _unlock_rig_controls(self) -> None:
        """Re-enable rig UI after TX completes, aborts, or errors."""
        self._radio_panel.set_tx_active(False)
        self._settings_action.setEnabled(True)

    def _schedule_rx_resume(self) -> None:
        """Lift the RX gate 50 ms after TX ends.

        The brief delay lets any trailing RF (and the PortAudio callback
        that was already queued) drain before the decoder resumes, so no
        TX-period audio bleeds into the next RX attempt.  The gate-off
        also calls RxWorker.reset() so the counter and decoder start clean.
        """
        QTimer.singleShot(50, lambda: self._request_rx_gate.emit(False))

    @Slot()
    def _on_tx_complete(self) -> None:
        self._tx_panel.set_transmitting(False)
        if self._last_tx_was_test_tone:
            self._last_tx_was_test_tone = False
            alc_msg = (
                "Test tone complete. "
                "If ALC didn't move, check: "
                "(1) Radio's USB MOD Level menu, "
                "(2) this app's TX gain slider, "
                "(3) computer output volume."
            )
            self._tx_panel.set_status(alc_msg)
            self.statusBar().showMessage(alc_msg, 10000)
        else:
            self._tx_panel.set_status("Transmission complete.")
            self.statusBar().showMessage("Ready")
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    @Slot(float)
    def _on_watchdog_fired(self, duration_s: float) -> None:
        """Watchdog tripped: record the fact so _on_tx_aborted can display
        a persistent message instead of the generic "Ready".

        ``duration_s`` is the budget (seconds) the firing timer was
        created with — stage 1 (encode) or stage 2 (per-transmission
        playback).  Forwarded from ``TxWorker.watchdog_fired`` so the
        UI message can quote the actual number.
        """
        self._last_abort_was_watchdog = True
        self._last_watchdog_duration_s = duration_s

    @Slot()
    def _on_tx_aborted(self) -> None:
        self._tx_panel.set_transmitting(False)
        if self._last_abort_was_watchdog:
            self._last_abort_was_watchdog = False
            msg = (
                f"TX watchdog: exceeded {self._last_watchdog_duration_s:.0f} s "
                "— rig unkeyed automatically"
            )
            self._tx_panel.set_status(msg)
            self.statusBar().showMessage(msg)
        else:
            if self._last_tx_was_test_tone:
                self._last_tx_was_test_tone = False
                self._tx_panel.set_status("Test tone stopped.")
            else:
                self._tx_panel.set_status("Transmission aborted.")
            self.statusBar().showMessage("Ready")
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    @Slot(str)
    def _on_tx_error(self, message: str) -> None:
        self._last_tx_was_test_tone = False
        self._tx_panel.set_transmitting(False)
        self._tx_panel.set_status(f"Error: {message}")
        self.statusBar().showMessage(message, 5000)
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    # === RX slots ===

    @Slot(bool)
    def _on_capture_requested(self, start: bool) -> None:
        """Translate the panel's Start/Stop toggle into worker calls.

        Emission goes via the private ``_request_start_capture`` /
        ``_request_stop_capture`` signals so the audio-worker slots
        actually run on the audio worker thread (queued connection)
        rather than on the GUI thread.

        On start, the audio capture is deferred until the RxWorker's
        ``reset_done`` fires (OP-05): emitting ``_request_rx_reset`` and
        ``_request_start_capture`` simultaneously from the GUI thread
        races on two different worker threads, and a pre-queued chunk
        from an already-warm device can reach ``feed_chunk`` before the
        reset slot runs.  The one-shot ``reset_done → start_capture``
        connection sequences the two steps deterministically.
        """
        if start:
            # Reset the decoder + sample counter so each new capture session
            # starts from zero rather than accumulating across stop/restart
            # cycles (bug R-1: counter climbed past 127s with no image).
            # Defer the start-capture request until reset_done arrives so
            # the two worker threads are ordered correctly (OP-05).
            device = self._input_device
            sample_rate = self._config.sample_rate

            def _start_once() -> None:
                # Disconnect ourselves before emitting so a later reset()
                # (e.g. user clicks Clear) doesn't retrigger start_capture.
                try:
                    self._rx_worker.reset_done.disconnect(_start_once)
                except (RuntimeError, TypeError):
                    pass
                self._request_start_capture.emit(
                    device, sample_rate, DEFAULT_BLOCKSIZE
                )

            self._rx_worker.reset_done.connect(_start_once)
            self._request_rx_reset.emit()
        else:
            # Cancel any in-flight decode before stopping audio so the tail
            # flush triggered by audio_worker.stopped doesn't block the worker
            # thread for several seconds on a large buffer.
            self._rx_worker.request_cancel()
            self._request_stop_capture.emit()

    @Slot()
    def _on_rx_started(self) -> None:
        self._capture_running = True
        self._rx_panel.set_capturing(True)
        self.statusBar().showMessage("Capturing")

    @Slot()
    def _on_rx_stopped(self) -> None:
        self._capture_running = False
        self._rx_panel.set_capturing(False)
        self._rx_panel.set_status("Capture stopped.")
        self.statusBar().showMessage("Ready")

    @Slot()
    def _on_rx_clear(self) -> None:
        # Cancel any in-flight decode immediately (thread-safe flag set)
        # before the queued reset() slot clears the buffer on the worker thread.
        self._rx_worker.request_cancel()
        self._request_rx_reset.emit()
        self._rx_panel.set_status("Cleared — waiting for VIS header.")

    @Slot(object, object, int)
    def _on_rx_image_complete(
        self, image: object, mode: object, vis_code: int
    ) -> None:
        """Auto-save a newly decoded image if the setting is enabled."""
        if not self._config.auto_save:
            return
        pil_image: PILImage = image  # type: ignore[assignment]
        # Mode may arrive as a plain str (Qt unwraps StrEnum through signals)
        mode_name = mode.value if isinstance(mode, Mode) else str(mode)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = Path(self._config.images_save_dir)
        path = save_dir / f"sstv_{mode_name}_{stamp}.png"
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            pil_image.save(str(path))
            self.statusBar().showMessage(f"Auto-saved {path.name}", 3000)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    @Slot(object, object)
    def _on_rx_image_saved(self, image: object, mode: object) -> None:
        """Save a decoded image to disk.

        If auto-save is enabled, writes directly to the configured save
        directory with a timestamped filename. Otherwise, opens a
        ``QFileDialog`` so the user can choose where to save.
        """
        pil_image: PILImage = image  # type: ignore[assignment]
        # Mode may arrive as a plain str (Qt unwraps StrEnum through signals)
        mode_name = mode.value if isinstance(mode, Mode) else str(mode)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"sstv_{mode_name}_{stamp}.png"

        if self._config.auto_save:
            save_dir = Path(self._config.images_save_dir)
            path = save_dir / default_name
            try:
                save_dir.mkdir(parents=True, exist_ok=True)
                pil_image.save(str(path))
                self.statusBar().showMessage(f"Saved {path.name}", 3000)
            except OSError as exc:
                QMessageBox.warning(self, "Save failed", str(exc))
        else:
            start_dir = str(
                Path(self._config.images_save_dir) / default_name
            )
            path_str, _ = QFileDialog.getSaveFileName(
                self,
                "Save decoded image",
                start_dir,
                "PNG (*.png);;JPEG (*.jpg *.jpeg);;All files (*)",
            )
            if path_str:
                try:
                    pil_image.save(path_str)
                    self.statusBar().showMessage(
                        f"Saved {Path(path_str).name}", 3000
                    )
                except OSError as exc:
                    QMessageBox.warning(self, "Save failed", str(exc))

    @Slot()
    def _on_save_shortcut(self) -> None:
        """Ctrl+S: save the most recent decoded image."""
        self._rx_panel.save_current_image()

    @Slot(str)
    def _on_rx_error(self, message: str) -> None:
        self._rx_panel.set_status(f"RX: {message}")
        self.statusBar().showMessage(message, 5000)

    # === Rig connect / disconnect / poll ===

    @Slot()
    def _on_rig_connect(self) -> None:
        """Create a rig backend from the current config and start polling.

        Dispatches to either a Direct Serial backend or a rigctld TCP
        client depending on ``rig_connection_mode``. For rigctld, if
        ``auto_launch_rigctld`` is enabled and a radio model is configured,
        spawns rigctld automatically before connecting.
        """
        mode = self._config.rig_connection_mode

        # OP-28: dispatch via RigConnectionMode rather than string literals
        # so a future enum rename can't silently break one of the three
        # call sites that used to carry bare strings.
        if mode == RigConnectionMode.MANUAL:
            # Shouldn't normally reach here, but handle gracefully
            self.statusBar().showMessage(
                "Rig mode set to Manual — configure a connection in Settings first.", 5000,
            )
            return

        if mode == RigConnectionMode.SERIAL:
            self._connect_serial()
        else:
            self._connect_rigctld()

    def _connect_serial(self) -> None:
        """Create a direct serial rig backend and start polling."""
        port = self._config.rig_serial_port
        if not port:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                "No serial port configured — open Settings > Radio.", 5000,
            )
            return

        try:
            rig = create_serial_rig(
                protocol=self._config.rig_serial_protocol,
                port=port,
                baud_rate=self._config.rig_baud_rate,
                ci_v_address=self._config.rig_civ_address,
                ptt_line=self._config.rig_ptt_line,
            )
            rig.open()
            rig.ping()
        except RigError as exc:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Serial connection failed on {port} — {exc}", 5000,
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Serial connection failed: {exc}", 5000,
            )
            return

        self._rig = rig
        self._tx_worker.set_rig(rig)
        self._rig_poll_worker.set_rig(rig)
        self._radio_panel.set_connected(True)
        self._rig_poll_timer.start()
        self.statusBar().showMessage(
            f"Connected via {self._config.rig_serial_protocol} on {port}", 3000,
        )

    def _connect_rigctld(self) -> None:
        """Create a RigctldClient and start polling, optionally launching rigctld."""
        host = self._config.rigctld_host
        port = self._config.rigctld_port

        # Auto-launch rigctld if configured
        if (
            self._config.auto_launch_rigctld
            and self._config.rig_model_id > 0
            and self._rigctld_proc is None
        ):
            # OP-13: reject leading-dash values so a hand-edited config
            # can't slip an arbitrary rigctld flag into the argv.
            if not is_safe_rigctld_arg(self._config.rig_serial_port):
                self.statusBar().showMessage(
                    f"Refusing to launch rigctld with unsafe serial port "
                    f"{self._config.rig_serial_port!r} — "
                    "edit Settings → Radio → Serial port.",
                    8000,
                )
                return
            cmd = [
                "rigctld",
                "-m", str(self._config.rig_model_id),
                "-t", str(port),
            ]
            if self._config.rig_serial_port:
                cmd += ["-r", self._config.rig_serial_port]
            if self._config.rig_baud_rate:
                cmd += ["-s", str(self._config.rig_baud_rate)]
            try:
                self._rigctld_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                self.statusBar().showMessage(
                    "rigctld not found — install Hamlib, or switch to Direct Serial in Settings", 5000,
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.statusBar().showMessage(f"Failed to launch rigctld: {exc}", 5000)
                return
            # Give rigctld 500 ms to bind the port without freezing the GUI.
            QTimer.singleShot(500, lambda: self._finish_rigctld_connect(host, port))
            return

        self._finish_rigctld_connect(host, port)

    def _finish_rigctld_connect(self, host: str, port: int) -> None:
        """Attempt the actual socket connection to rigctld.

        Called either immediately (no auto-launch) or after a 500 ms
        ``QTimer.singleShot`` delay when rigctld was just spawned.
        Runs on the GUI thread but is fast (single TCP connect + ping).
        """
        try:
            client = RigctldClient(host=host, port=port)
            client.open()
            client.ping()
        except RigError as exc:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Could not connect to rigctld at {host}:{port} — {exc}",
                5000,
            )
            return

        self._rig = client
        self._tx_worker.set_rig(client)
        self._rig_poll_worker.set_rig(client)
        self._radio_panel.set_connected(True)
        self._rig_poll_timer.start()
        self.statusBar().showMessage(
            f"Connected to rigctld at {host}:{port}", 3000
        )

    @Slot()
    def _on_rig_disconnect(self) -> None:
        """Stop polling and tear down the rig link."""
        self._rig_poll_timer.stop()
        try:
            self._rig.close()
        except RigError:
            pass
        self._rig = ManualRig()
        self._tx_worker.set_rig(self._rig)
        self._rig_poll_worker.set_rig(self._rig)
        self._radio_panel.set_connected(False)
        # Stop rigctld if we launched it
        self._kill_rigctld()
        self.statusBar().showMessage("Rig disconnected.", 3000)

    def _kill_rigctld(self) -> None:
        """Terminate any rigctld process we spawned.

        Defensive against a process that already exited on its own
        (e.g. rigctld rejected its CLI args and quit) — ``terminate()``
        / ``wait()`` / ``kill()`` can raise ``ProcessLookupError`` (POSIX)
        or generic ``OSError`` in that case.  We always clear
        ``_rigctld_proc`` so the next launch attempt starts fresh
        regardless of how the cleanup went (OP-19).
        """
        if self._rigctld_proc is None:
            return
        try:
            self._rigctld_proc.terminate()
            try:
                self._rigctld_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._rigctld_proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        except (ProcessLookupError, OSError):
            # Already gone — nothing to do.
            pass
        finally:
            self._rigctld_proc = None

    @Slot(int, str, int)
    def _on_poll_result(self, freq: int, mode_name: str, strength: int) -> None:
        """Receive a successful rig poll result from ``_RigPollWorker``.

        Called via queued connection from the poll worker thread; runs
        on the GUI thread. ``poll_error`` from the worker connects directly
        to ``_radio_panel.set_connection_error``.
        """
        self._radio_panel.update_rig_status(freq, mode_name, strength)

    # === lifecycle ===

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt API
        # Stop rig polling first to avoid timer fires during teardown.
        self._rig_poll_timer.stop()

        # Disconnect inbound user signals first so a click that races
        # with shutdown can't queue fresh work onto a thread we're about
        # to tear down. Qt raises RuntimeError / TypeError if the
        # connection is already gone (e.g. closeEvent firing twice via
        # aboutToQuit + the X button), so we swallow both.
        for signal, slot in (
            (self._request_transmit, self._tx_worker.transmit),
            (self._request_test_tone, self._tx_worker.transmit_test_tone),
            (self._audio_worker.chunk_ready, self._rx_worker.feed_chunk),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        # Abort any in-flight TX before tearing down the worker thread.
        # request_stop is explicitly thread-safe (threading.Event +
        # sounddevice.stop), so calling it from the UI thread is fine.
        self._tx_worker.request_stop()
        self.statusBar().showMessage("Closing…")
        # Give the TX worker up to 1 s to unwind out of play_blocking
        # (chunked-write path checks stop_event only between 0.1 s chunks).
        # thread.wait(3000) below would handle it anyway, but waiting here
        # first makes the shutdown ordering explicit and avoids the edge case
        # where the thread.quit() drains queued events before the worker exits.
        self._tx_worker.wait_for_stop(timeout=1.0)

        # Stop RX audio capture via the queued signal so the actual
        # PortAudio/QTimer teardown runs on the audio worker thread
        # (touching a QTimer across thread affinity is illegal and
        # raises warnings). The queued stop lands on the audio
        # thread's event loop before ``quit()`` drains it.
        self._request_stop_capture.emit()

        self._tx_thread.quit()
        if not self._tx_thread.wait(3000):
            import logging as _logging
            import threading as _threading
            _logging.getLogger(__name__).warning(
                "TX worker thread did not finish within timeout — "
                "attempting emergency PTT unkey"
            )
            # Run emergency_unkey in a daemon thread with a short join so
            # a dead-rig serial timeout (~1.5 s) can't freeze the GUI for
            # the full timeout while we're trying to quit (OP-08).  The
            # thread is daemon=True so even if the unkey itself hangs,
            # the interpreter exits cleanly.
            t = _threading.Thread(
                target=self._tx_worker.emergency_unkey,
                name="sstv-app-emergency-unkey",
                daemon=True,
            )
            t.start()
            t.join(timeout=1.5)

        for thread in (
            self._audio_thread,
            self._rx_thread,
            self._rig_poll_thread,
        ):
            thread.quit()
            thread.wait(3000)

        try:
            self._rig.close()
        except RigError:
            # Closing should never throw to the user — they're already
            # quitting.
            pass
        self._kill_rigctld()
        super().closeEvent(event)


__all__ = ["MainWindow"]
