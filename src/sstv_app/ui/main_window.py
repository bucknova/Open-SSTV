# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Composes the TX panel (left), RX panel (right), three worker threads
(TX playback, RX audio capture, RX decode), a menu bar (File > Settings /
Quit), optional rig polling on a 1 Hz QTimer, and auto-save of decoded
images to the configured directory.

Threading
---------

Four threads total:

* GUI thread — owns the window, the two panels, and Qt's event loop.
* TX worker thread — owns ``TxWorker``, runs encode + ``play_blocking``.
* RX audio thread — owns ``InputStreamWorker``, runs PortAudio + queue
  drain timer.
* RX decode thread — owns ``RxWorker``, runs ``Decoder.feed`` on
  flushed batches.

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
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
)

from sstv_app.audio.devices import AudioDevice
from sstv_app.audio.input_stream import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_SAMPLE_RATE,
    InputStreamWorker,
)
from sstv_app.config.schema import AppConfig
from sstv_app.config.store import load_config, save_config
from sstv_app.radio.base import ManualRig, Rig
from sstv_app.radio.exceptions import RigError
from sstv_app.ui.rx_panel import RxPanel
from sstv_app.ui.settings_dialog import SettingsDialog
from sstv_app.ui.tx_panel import TxPanel
from sstv_app.ui.workers import RxWorker, TxWorker

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from sstv_app.core.modes import Mode


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

    def __init__(
        self,
        rig: Rig | None = None,
        output_device: AudioDevice | int | None = None,
        input_device: AudioDevice | int | None = None,
        config: AppConfig | None = None,
        parent: QMainWindow | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open SSTV")
        self.resize(1100, 640)

        self._config = config if config is not None else load_config()

        # Open the rig (no-op for ManualRig). We swallow connection
        # failures here so the window still launches with rig control
        # disabled — the user can fix their config and click around.
        self._rig: Rig = rig if rig is not None else ManualRig()
        try:
            self._rig.open()
        except RigError as exc:
            self.statusBar().showMessage(f"Rig: {exc}", 0)

        self._input_device = input_device

        # --- Menu bar ---
        self._build_menu_bar()

        # --- Panels inside a horizontal splitter ---
        self._tx_panel = TxPanel(self)
        self._rx_panel = RxPanel(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._tx_panel)
        splitter.addWidget(self._rx_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        # --- TX worker on its own thread ---
        self._tx_thread = QThread(self)
        self._tx_thread.setObjectName("sstv-app-tx-worker")
        self._tx_worker = TxWorker(rig=self._rig, output_device=output_device)
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
        self._rx_worker = RxWorker()
        self._rx_worker.moveToThread(self._rx_thread)
        self._rx_thread.finished.connect(self._rx_worker.deleteLater)
        self._rx_thread.start()

        # --- Wire TX signals (unchanged from Phase 1) ---
        self._tx_panel.transmit_requested.connect(self._tx_worker.transmit)
        self._tx_panel.stop_requested.connect(self._on_stop_requested)
        self._tx_worker.transmission_started.connect(self._on_tx_started)
        self._tx_worker.transmission_complete.connect(self._on_tx_complete)
        self._tx_worker.transmission_aborted.connect(self._on_tx_aborted)
        self._tx_worker.error.connect(self._on_tx_error)

        # --- Wire RX signals ---
        # Private start/stop dispatch signals → audio worker slots.
        # Cross-thread, so Qt auto-promotes these to QueuedConnection
        # and the PortAudio open runs on the audio worker thread.
        self._request_start_capture.connect(self._audio_worker.start)
        self._request_stop_capture.connect(self._audio_worker.stop)

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

        # RX worker -> UI.
        self._rx_worker.image_started.connect(self._rx_panel.show_image_started)
        self._rx_worker.image_complete.connect(self._rx_panel.show_image_complete)
        self._rx_worker.image_complete.connect(self._on_rx_image_complete)
        self._rx_worker.error.connect(self._on_rx_error)

        # --- Rig status widgets in the status bar ---
        self._rig_freq_label = QLabel("")
        self._rig_mode_label = QLabel("")
        self._rig_strength_label = QLabel("")
        self._rig_status_label = QLabel("")
        for w in (
            self._rig_freq_label,
            self._rig_mode_label,
            self._rig_strength_label,
            self._rig_status_label,
        ):
            self.statusBar().addPermanentWidget(w)

        # --- 1 Hz rig poll timer ---
        self._rig_poll_timer = QTimer(self)
        self._rig_poll_timer.setInterval(1000)
        self._rig_poll_timer.timeout.connect(self._poll_rig)
        if not isinstance(self._rig, ManualRig):
            self._rig_poll_timer.start()

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

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = mb.addMenu("&Help")
        about_action = QAction("&About Open SSTV", self)
        about_action.setMenuRole(QAction.MenuRole.NoRole)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    @Slot()
    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Open SSTV",
            "<h3>Open SSTV v0.1.0</h3>"
            "<p>Open-source SSTV transceiver for amateur radio.</p>"
            "<p>Robot 36 · Martin M1 · Scottie S1</p>"
            '<p><a href="https://github.com/bucknova/Open-SSTV">'
            "github.com/bucknova/Open-SSTV</a></p>"
            "<p>GPL-3.0-or-later</p>",
        )

    @Slot()
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._config, parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._config = dlg.result_config()
            save_config(self._config)
            self.statusBar().showMessage("Settings saved.", 3000)

    # === TX slots ===

    @Slot()
    def _on_stop_requested(self) -> None:
        self._tx_worker.request_stop()

    @Slot()
    def _on_tx_started(self) -> None:
        self._tx_panel.set_transmitting(True)
        self._tx_panel.set_status("Transmitting…")
        self.statusBar().showMessage("Transmitting")

    @Slot()
    def _on_tx_complete(self) -> None:
        self._tx_panel.set_transmitting(False)
        self._tx_panel.set_status("Transmission complete.")
        self.statusBar().showMessage("Ready")

    @Slot()
    def _on_tx_aborted(self) -> None:
        self._tx_panel.set_transmitting(False)
        self._tx_panel.set_status("Transmission aborted.")
        self.statusBar().showMessage("Ready")

    @Slot(str)
    def _on_tx_error(self, message: str) -> None:
        self._tx_panel.set_transmitting(False)
        self._tx_panel.set_status(f"Error: {message}")
        self.statusBar().showMessage(message, 5000)

    # === RX slots ===

    @Slot(bool)
    def _on_capture_requested(self, start: bool) -> None:
        """Translate the panel's Start/Stop toggle into worker calls.

        Emission goes via the private ``_request_start_capture`` /
        ``_request_stop_capture`` signals so the audio-worker slots
        actually run on the audio worker thread (queued connection)
        rather than on the GUI thread.
        """
        if start:
            self._request_start_capture.emit(
                self._input_device, DEFAULT_SAMPLE_RATE, DEFAULT_BLOCKSIZE
            )
        else:
            self._request_stop_capture.emit()

    @Slot()
    def _on_rx_started(self) -> None:
        self._rx_panel.set_capturing(True)
        self.statusBar().showMessage("Capturing")

    @Slot()
    def _on_rx_stopped(self) -> None:
        self._rx_panel.set_capturing(False)
        self._rx_panel.set_status("Capture stopped.")
        self.statusBar().showMessage("Ready")

    @Slot()
    def _on_rx_clear(self) -> None:
        self._rx_worker.reset()
        self._rx_panel.set_status("Cleared — waiting for VIS header.")

    @Slot(object, object, int)
    def _on_rx_image_complete(
        self, image: object, mode: object, vis_code: int
    ) -> None:
        """Auto-save a newly decoded image if the setting is enabled."""
        if not self._config.auto_save:
            return
        pil_image: PILImage = image  # type: ignore[assignment]
        sstv_mode: Mode = mode  # type: ignore[assignment]
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = Path(self._config.images_save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / f"sstv_{sstv_mode.value}_{stamp}.png"
        pil_image.save(str(path))
        self.statusBar().showMessage(f"Auto-saved {path.name}", 3000)

    @Slot(object, object)
    def _on_rx_image_saved(self, image: object, mode: object) -> None:
        """Save a decoded image to disk.

        If auto-save is enabled, writes directly to the configured save
        directory with a timestamped filename. Otherwise, opens a
        ``QFileDialog`` so the user can choose where to save.
        """
        pil_image: PILImage = image  # type: ignore[assignment]
        sstv_mode: Mode = mode  # type: ignore[assignment]
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"sstv_{sstv_mode.value}_{stamp}.png"

        if self._config.auto_save:
            save_dir = Path(self._config.images_save_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            path = save_dir / default_name
            pil_image.save(str(path))
            self.statusBar().showMessage(f"Saved {path.name}", 3000)
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
                pil_image.save(path_str)
                self.statusBar().showMessage(
                    f"Saved {Path(path_str).name}", 3000
                )

    @Slot(str)
    def _on_rx_error(self, message: str) -> None:
        self._rx_panel.set_status(f"RX: {message}")
        self.statusBar().showMessage(message, 5000)

    # === Rig polling ===

    @Slot()
    def _poll_rig(self) -> None:
        """Read frequency, mode, and S-meter from the rig.

        Called every 1 s by ``_rig_poll_timer``. If the rig connection
        fails, we show "Rig: disconnected" in the status bar and keep
        the timer running so reconnection attempts happen automatically
        (the ``RigctldClient`` retries once per call internally). No
        modal dialogs, no exceptions surfaced to the user.
        """
        try:
            freq = self._rig.get_freq()
            mode_name, _ = self._rig.get_mode()
            strength = self._rig.get_strength()
        except RigError:
            self._rig_freq_label.setText("")
            self._rig_mode_label.setText("")
            self._rig_strength_label.setText("")
            self._rig_status_label.setText("Rig: disconnected")
            return

        self._rig_freq_label.setText(self._format_freq(freq))
        self._rig_mode_label.setText(mode_name)
        self._rig_strength_label.setText(
            f"S{min(9, max(0, (strength + 73) // 6))}"
            if strength != 0
            else ""
        )
        self._rig_status_label.setText("")

    @staticmethod
    def _format_freq(hz: int) -> str:
        """Format a frequency in Hz as a friendly display string."""
        if hz <= 0:
            return ""
        if hz >= 1_000_000:
            return f"{hz / 1_000_000:.6f} MHz"
        if hz >= 1_000:
            return f"{hz / 1_000:.3f} kHz"
        return f"{hz} Hz"

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
            (self._tx_panel.transmit_requested, self._tx_worker.transmit),
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

        # Stop RX audio capture via the queued signal so the actual
        # PortAudio/QTimer teardown runs on the audio worker thread
        # (touching a QTimer across thread affinity is illegal and
        # raises warnings). The queued stop lands on the audio
        # thread's event loop before ``quit()`` drains it.
        self._request_stop_capture.emit()

        for thread in (self._tx_thread, self._audio_thread, self._rx_thread):
            thread.quit()
            thread.wait(3000)

        try:
            self._rig.close()
        except RigError:
            # Closing should never throw to the user — they're already
            # quitting.
            pass
        super().closeEvent(event)


__all__ = ["MainWindow"]
