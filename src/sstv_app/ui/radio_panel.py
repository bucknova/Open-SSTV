# SPDX-License-Identifier: GPL-3.0-or-later
"""Radio control panel widget.

A toolbar-style strip that sits above the TX/RX splitter in the main
window. Shows the current rig connection status, frequency, mode, and
signal strength, with a Connect/Disconnect button to manage the
rigctld link at runtime.

The panel owns no sockets or threads — it exposes signals
(``connect_requested``, ``disconnect_requested``) that the
``MainWindow`` translates into ``RigctldClient`` lifecycle calls, and
setters that the 1 Hz poll timer feeds with fresh data.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)


class RadioPanel(QWidget):
    """Toolbar-style widget for rig status and connection control."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    test_tone_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._tx_active = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)

        # Connection button
        self._connect_btn = QPushButton("Connect Rig")
        self._connect_btn.setFixedWidth(130)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        layout.addWidget(self._connect_btn)

        # Test Tone button — disabled until a real rig is connected
        self._test_tone_btn = QPushButton("Test Tone")
        self._test_tone_btn.setToolTip(
            "Transmit a 700 Hz + 1900 Hz two-tone signal for 5 s.\n"
            "Adjust mic/RF gain so ALC just barely lights on peaks."
        )
        self._test_tone_btn.setEnabled(False)
        self._test_tone_btn.clicked.connect(self.test_tone_requested.emit)
        layout.addWidget(self._test_tone_btn)

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

    @staticmethod
    def _add_separator(layout: QHBoxLayout) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

    # === Public API ===

    def set_connected(self, connected: bool) -> None:
        """Update the button label and status indicator."""
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
        self._update_test_tone_btn()

    def set_connection_error(self) -> None:
        """Show a disconnected/error state without changing the button."""
        self._status_label.setText("Connection lost")
        self._status_label.setStyleSheet("color: red;")

    def set_tx_active(self, active: bool) -> None:
        """Disable the connect/disconnect and test-tone buttons during TX.

        Prevents the user from swapping or disconnecting the rig mid-transmit,
        which could leave the radio stuck keyed on the wrong backend.
        """
        self._tx_active = active
        self._connect_btn.setEnabled(not active)
        self._update_test_tone_btn()

    def _update_test_tone_btn(self) -> None:
        """Enable the Test Tone button only when a rig is connected and idle."""
        self._test_tone_btn.setEnabled(self._connected and not self._tx_active)

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

        # S-meter: convert dBm-ish to S-units (S9 ≈ -73 dBm, 6 dB/S-unit)
        if strength_db != 0:
            s_unit = min(9, max(0, (strength_db + 73) // 6))
            self._smeter_bar.setValue(s_unit)
        else:
            self._smeter_bar.setValue(0)

    # === Private slots ===

    @Slot()
    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
        else:
            self.connect_requested.emit()


__all__ = ["RadioPanel"]
