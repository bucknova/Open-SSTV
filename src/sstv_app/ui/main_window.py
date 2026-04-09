# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Phase 2 step 17 wires up both halves of the app: the existing ``TxPanel``
on the left, a new ``RxPanel`` on the right, and three worker threads —
TX playback, RX audio capture, and RX decode. The radio status bar and
settings dialog are still deferred to Phase 3.

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

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QSplitter

from sstv_app.audio.devices import AudioDevice
from sstv_app.audio.input_stream import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_SAMPLE_RATE,
    InputStreamWorker,
)
from sstv_app.radio.base import ManualRig, Rig
from sstv_app.radio.exceptions import RigError
from sstv_app.ui.rx_panel import RxPanel
from sstv_app.ui.tx_panel import TxPanel
from sstv_app.ui.workers import RxWorker, TxWorker


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
        parent: QMainWindow | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open SSTV")
        self.resize(1100, 640)

        # Open the rig (no-op for ManualRig). We swallow connection
        # failures here so the window still launches with rig control
        # disabled — the user can fix their config and click around.
        self._rig: Rig = rig if rig is not None else ManualRig()
        try:
            self._rig.open()
        except RigError as exc:
            self.statusBar().showMessage(f"Rig: {exc}", 0)

        self._input_device = input_device

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
        self._rx_worker.error.connect(self._on_rx_error)

        self.statusBar().showMessage("Ready")

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

    @Slot(object, object)
    def _on_rx_image_saved(self, image: object, mode: object) -> None:
        """Placeholder for Phase 3 save-to-disk dialog.

        For now we just acknowledge the double-click in the status bar
        so the user gets feedback. The real ``QFileDialog`` hookup is
        part of the settings/auto-save polish pass.
        """
        del image, mode  # unused until Phase 3
        self.statusBar().showMessage("Save dialog coming in Phase 3", 3000)

    @Slot(str)
    def _on_rx_error(self, message: str) -> None:
        self._rx_panel.set_status(f"RX: {message}")
        self.statusBar().showMessage(message, 5000)

    # === lifecycle ===

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt API
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
