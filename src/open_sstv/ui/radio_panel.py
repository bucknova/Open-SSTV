# SPDX-License-Identifier: GPL-3.0-or-later
"""Radio control panel widget.

A toolbar-style strip that sits above the TX/RX splitter in the main
window. Shows the current rig connection status, frequency, mode, and
signal strength, with a Connect/Disconnect button to manage the
rigctld link at runtime.

The panel owns no sockets or threads — it exposes signals
(``connect_requested``, ``disconnect_requested``, ``tune_requested``)
that ``MainWindow`` translates into rig lifecycle and CAT calls, and
setters that the 1 Hz poll timer feeds with fresh data.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QToolButton,
    QWidget,
)

from open_sstv.radio.band_plan import SSTV_BAND_PLAN


class RadioPanel(QWidget):
    """Toolbar-style widget for rig status and connection control."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    cancel_requested = Signal()
    test_tone_requested = Signal()
    #: Emitted when the user picks a band-plan entry.
    #: Payload: (freq_hz: int, rig_mode: str, passband_hz: int).
    tune_requested = Signal(int, str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._tx_active = False
        self._connecting = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)

        # Connection button
        self._connect_btn = QPushButton("Connect Rig")
        self._connect_btn.setFixedWidth(130)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self._connect_btn)

        # Test Tone button — disabled only while a TX is already in flight.
        # Deliberately independent of rig-connection state: PTT keying goes
        # through whatever backend is configured, including the no-op
        # ManualRig for VOX/manual-PTT operators who never click "Connect
        # Rig" at all — they still need a way to key up and calibrate ALC.
        self._test_tone_btn = QPushButton("Test Tone")
        self._test_tone_btn.setToolTip(
            "Transmit a 700 Hz + 1900 Hz two-tone signal for 5 s.\n"
            "Adjust mic/RF gain so ALC just barely lights on peaks."
        )
        # Starts enabled — see _update_test_tone_btn: only an in-flight TX
        # disables it, and none is in flight at construction time.
        self._test_tone_btn.clicked.connect(self.test_tone_requested.emit)
        layout.addWidget(self._test_tone_btn)

        # Band Plan button — popup menu of SSTV calling frequencies.
        # Disabled when no rig is connected or TX is active.
        self._band_btn = QToolButton()
        self._band_btn.setText("Band Plan")
        self._band_btn.setToolTip(
            "Tune the rig to a standard SSTV calling frequency.\n"
            "Requires a connected rig (rigctld, serial, or TCI)."
        )
        self._band_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._band_btn.setEnabled(False)
        self._band_btn.setMenu(self._build_band_menu())
        layout.addWidget(self._band_btn)

        # Status indicator
        self._status_label = QLabel("Disconnected")
        self._status_label.setStyleSheet("color: gray;")
        self._status_label.setFixedWidth(110)
        layout.addWidget(self._status_label)

        self._add_separator(layout)

        # Frequency display
        freq_caption = QLabel("Freq:")
        freq_caption.setStyleSheet("font-weight: bold;")
        layout.addWidget(freq_caption)
        self._freq_label = QLabel("—")
        self._freq_label.setMinimumWidth(140)
        self._freq_label.setStyleSheet("font-family: monospace; font-size: 14px;")
        layout.addWidget(self._freq_label)

        self._add_separator(layout)

        # Mode display
        mode_caption = QLabel("Mode:")
        mode_caption.setStyleSheet("font-weight: bold;")
        layout.addWidget(mode_caption)
        self._mode_label = QLabel("—")
        self._mode_label.setFixedWidth(60)
        layout.addWidget(self._mode_label)

        self._add_separator(layout)

        # S-meter
        smeter_caption = QLabel("S:")
        smeter_caption.setStyleSheet("font-weight: bold;")
        layout.addWidget(smeter_caption)
        self._smeter_bar = QProgressBar()
        self._smeter_bar.setRange(0, 9)
        self._smeter_bar.setValue(0)
        self._smeter_bar.setTextVisible(True)
        self._smeter_bar.setFormat("S%v")
        self._smeter_bar.setFixedWidth(100)
        self._smeter_bar.setFixedHeight(18)
        layout.addWidget(self._smeter_bar)

        layout.addStretch()

        # Callsign (right-aligned)
        self._callsign_label = QLabel("")
        self._callsign_label.setStyleSheet(
            "font-weight: bold; font-size: 14px;"
        )
        self._callsign_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._callsign_label)

    def _build_band_menu(self) -> QMenu:
        """Build the SSTV band-plan popup menu from ``SSTV_BAND_PLAN``."""
        menu = QMenu(self)
        last_region: str = ""
        for entry in SSTV_BAND_PLAN:
            if last_region and entry.region != last_region:
                menu.addSeparator()
            last_region = entry.region

            action = QAction(entry.label, self)
            # Capture loop variable by default-arg binding.
            action.triggered.connect(
                lambda _checked=False,
                f=entry.freq_hz,
                m=entry.rig_mode,
                p=entry.passband_hz: self.tune_requested.emit(f, m, p)
            )
            if entry.primary:
                font = action.font()
                font.setBold(True)
                action.setFont(font)
            menu.addAction(action)
        return menu

    @staticmethod
    def _add_separator(layout: QHBoxLayout) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

    # === Public API ===

    @property
    def connected(self) -> bool:
        """True when a rig backend is currently connected."""
        return self._connected

    def set_connecting(self) -> None:
        """Show a connecting-in-progress state with a Cancel button.

        The button is relabelled "Cancel" and kept *enabled* so the user can
        abort the attempt.  ``set_connected`` or ``set_connection_error`` will
        restore the button when the attempt resolves.
        """
        self._connecting = True
        self._connect_btn.setText("Cancel")
        self._connect_btn.setEnabled(True)  # always clickable so Cancel works
        self._status_label.setText("Connecting…")
        self._status_label.setStyleSheet("color: orange;")

    def set_connected(self, connected: bool) -> None:
        """Update the button label and status indicator."""
        self._connecting = False
        self._connected = connected
        if connected:
            self._connect_btn.setText("Disconnect")
            self._status_label.setText("Connected")
            self._status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self._connect_btn.setText("Connect Rig")
            self._status_label.setText("Disconnected")
            self._status_label.setStyleSheet("color: gray;")
            self._freq_label.setText("—")
            self._mode_label.setText("—")
            self._smeter_bar.setValue(0)
        self._update_connect_btn()
        self._update_test_tone_btn()
        self._update_band_btn()

    def set_connection_error(self) -> None:
        """Show a disconnected/error state and re-enable the connect button."""
        self._connecting = False
        self._connect_btn.setText("Connect Rig")  # reset from "Cancel" if needed
        self._update_connect_btn()
        self._status_label.setText("Connection lost")
        self._status_label.setStyleSheet("color: red;")

    def set_tx_active(self, active: bool) -> None:
        """Disable the connect/disconnect and test-tone buttons during TX.

        Prevents the user from swapping or disconnecting the rig mid-transmit,
        which could leave the radio stuck keyed on the wrong backend.
        """
        self._tx_active = active
        self._update_connect_btn()
        self._update_test_tone_btn()
        self._update_band_btn()

    def _update_connect_btn(self) -> None:
        """Enable the connect/cancel button whenever TX is not active.

        During connecting, the button shows "Cancel" and stays enabled — so
        the user can always abort.  TX disables it (to prevent rig mid-swap).
        """
        self._connect_btn.setEnabled(not self._tx_active)

    def _update_test_tone_btn(self) -> None:
        """Enable the Test Tone button whenever no TX is already in flight.

        Not gated on rig connection — VOX/manual-PTT operators never
        connect a rig at all, and Test Tone works fine through the no-op
        ManualRig backend (see ``TxWorker._run_tx``).
        """
        self._test_tone_btn.setEnabled(not self._tx_active)

    def _update_band_btn(self) -> None:
        """Enable the Band Plan button only when a rig is connected and idle."""
        self._band_btn.setEnabled(self._connected and not self._tx_active)

    def set_callsign(self, callsign: str) -> None:
        self._callsign_label.setText(callsign)

    def update_rig_status(
        self, freq_hz: int, mode: str, strength_db: int
    ) -> None:
        """Feed fresh poll data into the display widgets."""
        # Frequency
        if freq_hz > 0:
            if freq_hz >= 1_000_000:
                self._freq_label.setText(f"{freq_hz / 1_000_000:.6f} MHz")
            elif freq_hz >= 1_000:
                self._freq_label.setText(f"{freq_hz / 1_000:.3f} kHz")
            else:
                self._freq_label.setText(f"{freq_hz} Hz")
        else:
            self._freq_label.setText("—")

        # Mode
        self._mode_label.setText(mode if mode else "—")

        # S-meter: convert dBm to S-units.
        # Standard scale: S0 = −127 dBm, each S-unit = 6 dB, S9 = −73 dBm.
        # Formula: S = (dBm + 127) // 6  (clamped 0–9).
        # Bug history: the old formula (dBm+73)//6 mapped S9→0, making the
        # bar appear empty for every real signal until S9+60 was exceeded.
        # OP-33: ``strength_db == 0`` is treated as the "no reading"
        # sentinel (ManualRig and the various rig backends return 0 when
        # the rig hasn't been polled yet).  A genuine 0 dBm reading is
        # ~S9+73 — well off the top of the meter — so this collision is
        # cosmetic in practice.  When pollers gain a richer "no reading"
        # signal (e.g. None) wire it through here.
        if strength_db != 0:
            s_unit = min(9, max(0, (strength_db + 127) // 6))
            self._smeter_bar.setValue(s_unit)
        else:
            self._smeter_bar.setValue(0)

    # === Private slots ===

    @Slot()
    def _on_connect_clicked(self) -> None:
        if self._connecting:
            self.cancel_requested.emit()
        elif self._connected:
            self.disconnect_requested.emit()
        else:
            self.connect_requested.emit()


__all__ = ["RadioPanel"]
