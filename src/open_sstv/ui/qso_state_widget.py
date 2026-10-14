# SPDX-License-Identifier: GPL-3.0-or-later
"""QSO State widget for the TX panel.

Collects per-QSO context used to resolve tokens in v0.3 templates:
ToCall, RST, other-op's name, and a free-form note.

State persists across multiple transmissions within the same QSO; the
user clears it explicitly with [Clear QSO] or by changing ToCall to a
different callsign (heuristic: new callsign → new QSO).

The ``state_changed`` signal is debounced 300 ms so live-preview
re-renders don't fire on every keypress.

v0.6.7 adds RST-received / QTH / Grid plus an [External Log] button.
These describe the contact rather than resolving a template token, so
they're kept off the ``QSOState`` dataclass and exposed through
``get_contact_fields()`` instead.  Both consumers use them: the UDP
QSO-log broadcast (``logbook.udp_log``) and the logbook draft built at
TX completion.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from open_sstv.templates.model import QSOState
from open_sstv.ui.log_qso_dialog import GRID_MAX_LEN, install_uppercase

# Common SSTV RST reports in the combo history (descending quality).
_RST_PRESETS: tuple[str, ...] = (
    "595",
    "575",
    "555",
    "535",
    "495",
    "455",
    "395",
    "335",
)

_DEBOUNCE_MS: int = 300


class QSOStateWidget(QWidget):
    """Compact QSO-state input bar for the TX panel.

    Signals
    -------
    state_changed(QSOState):
        Emitted after a 300 ms debounce whenever any field changes.
        Consumers (live preview, thumbnail gallery) should connect here.
    logbook_requested():
        Emitted when the [Logbook…] button is clicked.  Relayed up
        through TxPanel to MainWindow, which owns the logbook window —
        this widget stays standalone-testable with no logbook import.
    udp_log_requested():
        Emitted when the [External Log] button is clicked.  Relayed up
        through TxPanel to MainWindow, which builds a QSO from
        ``get_state()`` + ``get_contact_fields()`` and sends it via
        ``logbook.udp_log.UdpQsoLogger`` — this widget stays free of
        any logbook/network import.
    """

    state_changed = Signal(object)  # QSOState
    logbook_requested = Signal()
    udp_log_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._prev_tocall: str = ""
        #: ``%q`` / ``{qso_serial}``: this session's QSO number, starting
        #: at 1 and advanced by *Clear QSO* after a contact with a ToCall.
        #: v0.6.14 audit: QSOState.serial was never set, so the token
        #: always rendered 0.
        self._serial: int = 1

        # --- Debounce timer ---
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        # --- Row 1: ToCall + RST sent/received ---
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        row1.addWidget(QLabel("ToCall:"))
        self._tocall = QLineEdit()
        self._tocall.setPlaceholderText("W0XYZ")
        self._tocall.setMaximumWidth(100)
        install_uppercase(self._tocall)
        self._tocall.textChanged.connect(self._on_field_changed)
        row1.addWidget(self._tocall)

        self._serial_label = QLabel()
        self._serial_label.setToolTip(
            "QSO number this session — the %q / {qso_serial} template token. "
            "Clear QSO advances it."
        )
        row1.addWidget(self._serial_label)
        self._update_serial_label()

        row1.addWidget(QLabel("RSTs:"))
        self._rst = QComboBox()
        self._rst.setEditable(True)
        self._rst.setMaximumWidth(70)
        for r in _RST_PRESETS:
            self._rst.addItem(r)
        self._rst.setCurrentText("595")
        self._rst.currentTextChanged.connect(self._on_field_changed)
        row1.addWidget(self._rst)

        # v0.6.7: RST received sits right next to RST sent — feeds the
        # UDP QSO log only (see module docstring), not a template token.
        row1.addWidget(QLabel("RSTr:"))
        self._rst_received = QComboBox()
        self._rst_received.setEditable(True)
        self._rst_received.setMaximumWidth(70)
        for r in _RST_PRESETS:
            self._rst_received.addItem(r)
        self._rst_received.setCurrentText("")
        row1.addWidget(self._rst_received)

        row1.addStretch(1)
        self._clear_btn = QPushButton("Clear QSO")
        self._clear_btn.setMaximumWidth(90)
        self._clear_btn.clicked.connect(self.clear)
        row1.addWidget(self._clear_btn)

        # v0.4: one-click path to the logbook while working a contact —
        # same destination as Tools → Logbook… (Cmd/Ctrl+L).
        self._logbook_btn = QPushButton("Logbook…")
        self._logbook_btn.setMaximumWidth(90)
        self._logbook_btn.setToolTip("Open the logbook window (Ctrl+L / Cmd+L)")
        self._logbook_btn.clicked.connect(self.logbook_requested)
        row1.addWidget(self._logbook_btn)

        # v0.6.7: one-click UDP QSO-log broadcast — sent once per QSO from
        # whatever is currently in this bar, independent of the local
        # logbook database.  See ``logbook.udp_log``.
        self._udp_log_btn = QPushButton("External Log")
        self._udp_log_btn.setMaximumWidth(100)
        self._udp_log_btn.setToolTip(
            "Send this QSO as a UDP log message to a companion logging "
            "app (Settings → Logging)"
        )
        self._udp_log_btn.clicked.connect(self.udp_log_requested)
        row1.addWidget(self._udp_log_btn)

        layout.addLayout(row1)

        # --- Row 2: Name / QTH / Grid (v0.6.7 adds QTH + Grid) ---
        # Name still resolves the {tocall_name} template token; QTH/Grid
        # are UDP-QSO-log-only (see module docstring).
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("Name:"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("Optional")
        self._name.setMaximumWidth(100)
        self._name.textChanged.connect(self._on_field_changed)
        row2.addWidget(self._name)

        row2.addWidget(QLabel("QTH:"))
        self._qth = QLineEdit()
        self._qth.setPlaceholderText("Optional")
        self._qth.setMaximumWidth(100)
        row2.addWidget(self._qth)

        row2.addWidget(QLabel("Grid:"))
        self._grid = QLineEdit()
        self._grid.setPlaceholderText("EN34")
        self._grid.setMaximumWidth(70)
        self._grid.setMaxLength(GRID_MAX_LEN)
        install_uppercase(self._grid)
        row2.addWidget(self._grid)
        row2.addStretch(1)
        layout.addLayout(row2)

        # --- Row 3: free-form note (unhidden in v0.4) ---
        # Resolves the {note} template token and pre-fills the Notes
        # field of the v0.4 log-QSO capture dialog.
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        row3.addWidget(QLabel("Note:"))
        self._note = QLineEdit()
        self._note.setPlaceholderText("Optional — logged with the QSO")
        self._note.textChanged.connect(self._on_field_changed)
        row3.addWidget(self._note, stretch=1)
        layout.addLayout(row3)

    # === Public API ===

    def get_state(self) -> QSOState:
        """Return the current QSO state."""
        return QSOState(
            tocall=self._tocall.text().strip(),
            rst=self._rst.currentText().strip() or "595",
            tocall_name=self._name.text().strip(),
            note=self._note.text().strip(),
            serial=self._serial,
        )

    @property
    def serial(self) -> int:
        return self._serial

    def set_serial(self, serial: int) -> None:
        """Set the QSO number (``%q``); values below 1 become 1."""
        self._serial = max(1, int(serial))
        self._update_serial_label()
        self._on_field_changed()

    def _update_serial_label(self) -> None:
        self._serial_label.setText(f"#{self._serial}")

    def get_contact_fields(self) -> tuple[str, str, str]:
        """Return ``(rst_received, qth, grid)`` describing the contact.

        Kept separate from ``get_state()``/``QSOState`` because these
        aren't template tokens.  They feed both the UDP QSO-log
        broadcast and the logbook draft at TX completion — the name is
        deliberately not UDP-specific, since scoping them to that one
        consumer is what caused them to be silently dropped from the
        logbook in the first place.
        """
        return (
            self._rst_received.currentText().strip(),
            self._qth.text().strip(),
            self._grid.text().strip().upper(),
        )

    @Slot()
    def clear(self) -> None:
        """Wipe all fields and emit state_changed immediately.

        A contact that had a ToCall counts as finished, so the QSO
        number advances for the next one.
        """
        if self._tocall.text().strip():
            self._serial += 1
            self._update_serial_label()
        self._prev_tocall = ""
        self._tocall.blockSignals(True)
        self._rst.blockSignals(True)
        self._name.blockSignals(True)
        self._note.blockSignals(True)
        try:
            self._tocall.clear()
            self._rst.setCurrentText("595")
            self._name.clear()
            self._note.clear()
            self._rst_received.setCurrentText("")
            self._qth.clear()
            self._grid.clear()
        finally:
            self._tocall.blockSignals(False)
            self._rst.blockSignals(False)
            self._name.blockSignals(False)
            self._note.blockSignals(False)
        self._debounce.stop()
        self._emit_state()

    # === Slots ===

    @Slot()
    @Slot(str)
    def _on_field_changed(self, _text: str = "") -> None:
        self._debounce.start()

    @Slot()
    def _emit_state(self) -> None:
        self.state_changed.emit(self.get_state())


__all__ = ["QSOStateWidget"]
