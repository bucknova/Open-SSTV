# SPDX-License-Identifier: GPL-3.0-or-later
"""First-launch welcome dialog (v0.2.7).

Shown exactly once on a truly fresh install — detected by the
``first_launch_seen`` flag in ``AppConfig`` — to collect the operator's
callsign.  The callsign is required for the TX banner strip and the CW
station ID that's keyed after every SSTV transmission (FCC §97.119 and
equivalent ID rules in other administrations), so prompting for it up
front saves the user hunting through Settings before their first TX.

The dialog is deliberately tiny: a welcome paragraph, a single callsign
input, and two buttons (*Save* / *Skip for now*).  Whichever button the
user clicks, the caller flips ``first_launch_seen`` to ``True`` so the
dialog never reappears — listening-only operators who skip can set
their callsign later in Settings without being nagged every launch.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


class FirstLaunchDialog(QDialog):
    """Welcome-and-callsign prompt shown once on first launch."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to Open-SSTV")
        self.setModal(True)
        # Keep the dialog compact — this is not a settings panel.
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        header = QLabel("<b>Welcome to Open-SSTV</b>")
        layout.addWidget(header)

        intro = QLabel(
            "Open-SSTV is a cross-platform amateur-radio SSTV transceiver.\n\n"
            "Enter your callsign below — it's stamped on the transmitted "
            "image banner and keyed as the CW station ID at the end of "
            "every transmission, which covers FCC §97.119 identification "
            "rules (and equivalents in other administrations).\n\n"
            "Name, grid square, and QTH are optional — fill them in if "
            "you'd like the v0.3 template tokens "
            "(<tt>{name}</tt>, <tt>{grid}</tt>, <tt>{qth}</tt>) to "
            "resolve to your station info on every TX.\n\n"
            "If you're only planning to listen, click *Skip for now* — "
            "you can set everything later under File → Settings."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        form = QFormLayout()
        self._callsign_input = QLineEdit()
        self._callsign_input.setPlaceholderText("e.g. W0AEZ")
        # 12 chars covers the longest legitimate suffix (e.g. "VE3ABC/MM",
        # "W1ABC/QRP") without making the field look oversized.
        self._callsign_input.setMaxLength(12)
        self._callsign_input.textChanged.connect(self._on_text_changed)
        form.addRow("Callsign:", self._callsign_input)

        # v0.3.4: optional operator-info fields.  Empty submissions are
        # fine — they leave the corresponding AppConfig field at its
        # current value (empty on a fresh install).
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. Kevin")
        form.addRow("Name:", self._name_input)

        self._grid_input = QLineEdit()
        self._grid_input.setPlaceholderText("e.g. EM29")
        # Maidenhead grid is at most 6 characters (subsquare precision).
        self._grid_input.setMaxLength(6)
        # Force uppercase as the user types — Maidenhead is conventionally
        # rendered with the field letters uppercase.
        self._grid_input.textChanged.connect(self._on_grid_changed)
        form.addRow("Grid Square:", self._grid_input)

        self._qth_input = QLineEdit()
        self._qth_input.setPlaceholderText("e.g. Kansas City, MO")
        form.addRow("QTH:", self._qth_input)

        layout.addLayout(form)

        self._check_updates = QCheckBox("Check for updates on startup")
        self._check_updates.setChecked(True)
        layout.addWidget(self._check_updates)

        privacy_note = QLabel(
            "Checks github.com/bucknova/Open-SSTV for new releases. No data is sent."
        )
        privacy_note.setWordWrap(True)
        privacy_note.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(privacy_note)

        # Standard Qt dialog buttons with custom labels.  Using the
        # standard box gives us correct platform-native ordering (Save
        # on the right on Windows, left on macOS) without hand-rolling
        # a QHBoxLayout + addStretch dance.
        buttons = QDialogButtonBox(self)
        self._save_btn = buttons.addButton(
            "Save", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._skip_btn = buttons.addButton(
            "Skip for now", QDialogButtonBox.ButtonRole.RejectRole
        )
        self._save_btn.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_text_changed(self, text: str) -> None:
        """Force uppercase as the user types.

        Callsigns are canonically uppercase (FCC ULS, LOTW, QRZ all
        render them uppercase) and anyone who types ``w0aez`` means
        ``W0AEZ``.  Do this without recursing into ``textChanged`` by
        only rewriting when the cased form actually differs.
        """
        upper = text.upper()
        if upper == text:
            return
        cursor = self._callsign_input.cursorPosition()
        self._callsign_input.blockSignals(True)
        try:
            self._callsign_input.setText(upper)
            self._callsign_input.setCursorPosition(cursor)
        finally:
            self._callsign_input.blockSignals(False)

    def _on_grid_changed(self, text: str) -> None:
        """Force uppercase on the grid input — same pattern as callsign."""
        upper = text.upper()
        if upper == text:
            return
        cursor = self._grid_input.cursorPosition()
        self._grid_input.blockSignals(True)
        try:
            self._grid_input.setText(upper)
            self._grid_input.setCursorPosition(cursor)
        finally:
            self._grid_input.blockSignals(False)

    def check_updates_enabled(self) -> bool:
        """Return whether the user opted in to startup update checks."""
        return self._check_updates.isChecked()

    def callsign(self) -> str:
        """Return the (trimmed, uppercased) callsign the user typed.

        May be empty — callers should only persist a non-empty result
        if ``exec()`` returned ``QDialog.DialogCode.Accepted``.  An
        empty *Save* is semantically a *Skip*, and the caller should
        treat it that way (don't overwrite any pre-existing callsign
        with empty).
        """
        return self._callsign_input.text().strip().upper()

    def operator_name(self) -> str:
        """Return the (trimmed) operator name. May be empty."""
        return self._name_input.text().strip()

    def grid_square(self) -> str:
        """Return the (trimmed, uppercased) Maidenhead grid. May be empty."""
        return self._grid_input.text().strip().upper()

    def qth(self) -> str:
        """Return the (trimmed) QTH free-text. May be empty."""
        return self._qth_input.text().strip()


__all__ = ["FirstLaunchDialog"]
