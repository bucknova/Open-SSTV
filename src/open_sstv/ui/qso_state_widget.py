# SPDX-License-Identifier: GPL-3.0-or-later
"""QSO State widget for the TX panel.

Collects per-QSO context used to resolve tokens in v0.3 templates:
ToCall, RST, other-op's name, and a free-form note.

State persists across multiple transmissions within the same QSO; the
user clears it explicitly with [Clear QSO] or by changing ToCall to a
different callsign (heuristic: new callsign → new QSO).

The ``state_changed`` signal is debounced 300 ms so live-preview
re-renders don't fire on every keypress.
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
    """

    state_changed = Signal(object)  # QSOState

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._prev_tocall: str = ""

        # --- Debounce timer ---
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)

        # --- Row 1: ToCall + RST ---
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        row1.addWidget(QLabel("ToCall:"))
        self._tocall = QLineEdit()
        self._tocall.setPlaceholderText("W0XYZ")
        self._tocall.setMaximumWidth(100)
        self._tocall.textChanged.connect(self._on_tocall_changed)
        row1.addWidget(self._tocall)

        row1.addWidget(QLabel("RST:"))
        self._rst = QComboBox()
        self._rst.setEditable(True)
        self._rst.setMaximumWidth(70)
        for r in _RST_PRESETS:
            self._rst.addItem(r)
        self._rst.setCurrentText("595")
        self._rst.currentTextChanged.connect(self._on_field_changed)
        row1.addWidget(self._rst)

        row1.addWidget(QLabel("Name:"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("Optional")
        self._name.setMaximumWidth(100)
        self._name.textChanged.connect(self._on_field_changed)
        row1.addWidget(self._name)

        row1.addWidget(QLabel("Note:"))
        self._note = QLineEdit()
        self._note.setPlaceholderText("Optional")
        self._note.textChanged.connect(self._on_field_changed)
        row1.addWidget(self._note, stretch=1)

        self._clear_btn = QPushButton("Clear QSO")
        self._clear_btn.setMaximumWidth(90)
        self._clear_btn.clicked.connect(self.clear)
        row1.addWidget(self._clear_btn)

        layout.addLayout(row1)

    # === Public API ===

    def get_state(self) -> QSOState:
        """Return the current QSO state."""
        return QSOState(
            tocall=self._tocall.text().strip(),
            rst=self._rst.currentText().strip() or "595",
            tocall_name=self._name.text().strip(),
            note=self._note.text().strip(),
        )

    @Slot()
    def clear(self) -> None:
        """Wipe all fields and emit state_changed immediately."""
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
        finally:
            self._tocall.blockSignals(False)
            self._rst.blockSignals(False)
            self._name.blockSignals(False)
            self._note.blockSignals(False)
        self._debounce.stop()
        self._emit_state()

    # === Slots ===

    @Slot(str)
    def _on_tocall_changed(self, text: str) -> None:
        # Uppercase-on-type.
        upper = text.upper()
        if upper != text:
            cursor = self._tocall.cursorPosition()
            self._tocall.blockSignals(True)
            self._tocall.setText(upper)
            self._tocall.setCursorPosition(cursor)
            self._tocall.blockSignals(False)
        self._on_field_changed(upper)

    @Slot()
    @Slot(str)
    def _on_field_changed(self, _text: str = "") -> None:
        self._debounce.start()

    @Slot()
    def _emit_state(self) -> None:
        self.state_changed.emit(self.get_state())


__all__ = ["QSOStateWidget"]
