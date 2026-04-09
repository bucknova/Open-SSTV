# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Phase 1 wires up the **TX side only**: a single ``TxPanel`` as the central
widget plus a ``TxWorker`` running on its own ``QThread``. Phase 2 will add
the RX panel, waterfall, decoded gallery, and the radio status bar.

Threading
---------

Two threads:

* GUI thread — owns the window, the panel, and Qt's event loop.
* TX worker thread — owns ``TxWorker``, runs encode + ``play_blocking``.

The signal wiring is one-line per direction:

    panel.transmit_requested  ──Qt.AutoConnection──>  worker.transmit
        # cross-thread, becomes QueuedConnection automatically

    worker.transmission_started  ──Qt.AutoConnection──>  panel.set_transmitting(True)
    worker.transmission_complete ──>  panel.set_transmitting(False) + status
    worker.transmission_aborted  ──>  panel.set_transmitting(False) + status
    worker.error                 ──>  status bar

The Stop path is **not** a signal/slot connection — when the worker is
inside ``play_blocking`` a queued slot would never run. Instead the
panel's ``stop_requested`` signal lands on a UI-thread method that
directly calls ``worker.request_stop()``, which is documented as
thread-safe.

Lifecycle
---------

The window owns the worker thread; ``closeEvent`` aborts any in-flight
transmission, calls ``thread.quit()`` and ``thread.wait()`` so the
worker shuts down cleanly, and closes the rig.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow

from sstv_app.audio.devices import AudioDevice
from sstv_app.radio.base import ManualRig, Rig
from sstv_app.radio.exceptions import RigError
from sstv_app.ui.tx_panel import TxPanel
from sstv_app.ui.workers import TxWorker


class MainWindow(QMainWindow):
    """The Phase 1 main window: one TX panel, one TX worker thread."""

    def __init__(
        self,
        rig: Rig | None = None,
        output_device: AudioDevice | int | None = None,
        parent: QMainWindow | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open SSTV")
        self.resize(640, 540)

        # Open the rig (no-op for ManualRig). We swallow connection
        # failures here so the window still launches with rig control
        # disabled — the user can fix their config and click around.
        self._rig: Rig = rig if rig is not None else ManualRig()
        try:
            self._rig.open()
        except RigError as exc:
            self.statusBar().showMessage(f"Rig: {exc}", 0)

        # --- TX panel ---
        self._tx_panel = TxPanel(self)
        self.setCentralWidget(self._tx_panel)

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

        # --- Wire signals ---
        # UI -> worker (auto-becomes QueuedConnection across threads).
        self._tx_panel.transmit_requested.connect(self._tx_worker.transmit)
        # Stop is a *direct* method call — see module docstring.
        self._tx_panel.stop_requested.connect(self._on_stop_requested)

        # Worker -> UI.
        self._tx_worker.transmission_started.connect(self._on_tx_started)
        self._tx_worker.transmission_complete.connect(self._on_tx_complete)
        self._tx_worker.transmission_aborted.connect(self._on_tx_aborted)
        self._tx_worker.error.connect(self._on_tx_error)

        self.statusBar().showMessage("Ready")

    # === slots ===

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

    # === lifecycle ===

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt API
        # Disconnect the inbound transmit signal first so a click that
        # races with shutdown can't queue a fresh transmit() call onto
        # the worker thread we're about to tear down. Qt raises
        # RuntimeError if the connection is already gone (e.g. closeEvent
        # firing twice via aboutToQuit + the X button), so we swallow it.
        try:
            self._tx_panel.transmit_requested.disconnect(self._tx_worker.transmit)
        except (RuntimeError, TypeError):
            pass

        # Abort any in-flight TX before tearing down the worker thread.
        self._tx_worker.request_stop()
        self._tx_thread.quit()
        self._tx_thread.wait(3000)
        try:
            self._rig.close()
        except RigError:
            # Closing should never throw to the user — they're already
            # quitting.
            pass
        super().closeEvent(event)


__all__ = ["MainWindow"]
