# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-QSO entry form — capture at TX/RX completion, edit from the logbook.

Opened in two roles:

- **Capture** (``MainWindow``): a draft built by ``LogbookCoordinator``
  arrives with mode / time / frequency / image auto-filled; the
  operator adds callsign + RSV + notes while the contact is fresh.
  Esc rejects → **no row is written**, so a noise-triggered RX
  completion costs one keypress to dismiss.
- **Edit / New** (``LogbookDialog``): same form over an existing row
  (or a blank manual entry).  *Save & New* lets the operator key in a
  stack of paper-log entries without re-opening the dialog chain.

The dialog mutates the ``QSO`` it was given only inside
``result_qso()`` — callers persist the result; the dialog never
touches the store itself.
"""
from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from open_sstv.ui.utils import pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from open_sstv.logbook.model import QSO

#: Same preset ladder as the TX panel's QSO-state bar.
_RSV_PRESETS: tuple[str, ...] = (
    "595", "575", "555", "535", "495", "455", "395", "335",
)

#: Thumbnail box for the auto-filled image preview.
_THUMB_W, _THUMB_H = 240, 180


def format_frequency(frequency_hz: int | None) -> str:
    """Human display for a frequency snapshot: ``14.230 MHz`` or an em-dash.

    kHz resolution — hams think in kHz steps; the full Hz value stays
    in the database and in ADIF export untouched.
    """
    if frequency_hz is None or frequency_hz <= 0:
        return "—"
    return f"{frequency_hz / 1_000_000:.3f} MHz"


class LogQsoDialog(QDialog):
    """Entry form for one QSO.  See module docstring for the two roles."""

    def __init__(
        self,
        qso: QSO,
        *,
        preview_image: PILImage | None = None,
        allow_save_and_new: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._qso = qso
        self._save_and_new = False

        is_edit = qso.id is not None
        verb = "Edit QSO" if is_edit else "Log QSO"
        who = f" — {qso.callsign}" if qso.callsign else f" — {qso.direction} {qso.mode}".rstrip()
        self.setWindowTitle(f"{verb}{who}")
        self.setModal(True)

        root = QHBoxLayout(self)

        # --- Left: image thumbnail + auto-filled facts -----------------
        left = QVBoxLayout()
        self._thumb = QLabel()
        self._thumb.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet("border: 1px solid palette(mid);")
        self._set_thumbnail(preview_image)
        left.addWidget(self._thumb)

        facts = QFormLayout()
        facts.setHorizontalSpacing(8)
        facts.addRow("Direction:", QLabel(qso.direction))
        facts.addRow("Mode:", QLabel(qso.mode or "—"))
        time_s = qso.time_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        facts.addRow("Time:", QLabel(time_s))
        facts.addRow("Frequency:", QLabel(format_frequency(qso.frequency_hz)))
        left.addLayout(facts)
        left.addStretch(1)
        root.addLayout(left)

        # --- Right: editable contact fields ----------------------------
        right = QVBoxLayout()
        form = QFormLayout()
        form.setHorizontalSpacing(8)

        self._callsign = QLineEdit(qso.callsign)
        self._callsign.setPlaceholderText("W0XYZ")
        self._callsign.textChanged.connect(self._uppercase_callsign)
        form.addRow("Callsign:", self._callsign)

        self._rsv_sent = self._rsv_combo(qso.rsv_sent)
        form.addRow("RSV sent:", self._rsv_sent)
        self._rsv_received = self._rsv_combo(qso.rsv_received)
        form.addRow("RSV rcvd:", self._rsv_received)

        self._name = QLineEdit(qso.name)
        self._name.setPlaceholderText("Operator name")
        form.addRow("Name:", self._name)

        self._qth = QLineEdit(qso.qth)
        self._qth.setPlaceholderText("City / location")
        form.addRow("QTH:", self._qth)

        self._grid = QLineEdit(qso.grid)
        self._grid.setPlaceholderText("EN34")
        form.addRow("Grid:", self._grid)

        self._comment = QLineEdit(qso.comment)
        self._comment.setPlaceholderText("Notes")
        form.addRow("Notes:", self._comment)

        right.addLayout(form)
        right.addStretch(1)

        # --- Buttons ----------------------------------------------------
        buttons = QDialogButtonBox()
        self._save_btn = buttons.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._save_btn.setDefault(True)
        self._save_new_btn: QPushButton | None = None
        if allow_save_and_new:
            self._save_new_btn = buttons.addButton(
                "Save && New", QDialogButtonBox.ButtonRole.ActionRole
            )
            self._save_new_btn.clicked.connect(self._on_save_and_new)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)

        root.addLayout(right, stretch=1)

        self._callsign.setFocus()

    # === Public API ===

    def result_qso(self) -> QSO:
        """The QSO with the form's edits applied.  Call after accept."""
        q = self._qso
        q.callsign = self._callsign.text().strip().upper()
        q.rsv_sent = self._rsv_sent.currentText().strip()
        q.rsv_received = self._rsv_received.currentText().strip()
        q.name = self._name.text().strip()
        q.qth = self._qth.text().strip()
        q.grid = self._grid.text().strip().upper()
        q.comment = self._comment.text().strip()
        return q

    @property
    def save_and_new(self) -> bool:
        """True when the dialog was accepted via *Save & New*."""
        return self._save_and_new

    # === Internals ===

    def _rsv_combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        for preset in _RSV_PRESETS:
            combo.addItem(preset)
        combo.setCurrentText(current)
        return combo

    def _set_thumbnail(self, preview_image: PILImage | None) -> None:
        """Thumbnail from the in-memory image, the stored path, or a note.

        The in-memory image wins (capture flow — the file may not exist
        yet).  A stored path that no longer resolves shows the
        "missing image" indicator rather than erroring: we don't own
        the user's files and they're allowed to move them.
        """
        pixmap = None
        if preview_image is not None:
            pixmap = pil_to_pixmap(preview_image)
        elif self._qso.image_path is not None:
            if self._qso.image_path.is_file():
                from PySide6.QtGui import QPixmap  # noqa: PLC0415

                loaded = QPixmap(str(self._qso.image_path))
                pixmap = loaded if not loaded.isNull() else None
            if pixmap is None:
                self._thumb.setText("Missing image")
                return
        if pixmap is None:
            self._thumb.setText("No image")
            return
        self._thumb.setPixmap(
            pixmap.scaled(
                _THUMB_W,
                _THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @Slot(str)
    def _uppercase_callsign(self, text: str) -> None:
        upper = text.upper()
        if upper != text:
            cursor = self._callsign.cursorPosition()
            self._callsign.blockSignals(True)
            self._callsign.setText(upper)
            self._callsign.setCursorPosition(cursor)
            self._callsign.blockSignals(False)

    @Slot()
    def _on_save_and_new(self) -> None:
        self._save_and_new = True
        self.accept()


__all__ = ["LogQsoDialog", "format_frequency"]
