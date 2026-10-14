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

from open_sstv.ui.log_qso_dialog import GRID_MAX_LEN, install_uppercase


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

        # v0.6.14 audit nit: this label is RichText (for the <tt> tokens),
        # where "\n\n" collapses to a space and *…* is literal asterisks,
        # so the welcome text rendered as one run-on paragraph.  Real
        # paragraphs and <i> instead.
        intro = QLabel(
            "<p>Open-SSTV is a cross-platform amateur-radio SSTV "
            "transceiver.</p>"
            "<p>Enter your callsign below — it's stamped on the transmitted "
            "image banner and keyed as the CW station ID at the end of "
            "every transmission, which covers FCC §97.119 identification "
            "rules (and equivalents in other administrations).</p>"
            "<p>Name, grid square, and QTH are optional — fill them in if "
            "you'd like the template tokens "
            "(<tt>{name}</tt>, <tt>{grid}</tt>, <tt>{qth}</tt>) to "
            "resolve to your station info on every TX.</p>"
            "<p>If you're only planning to listen, click <i>Skip for now</i> "
            "— you can set everything later under File → Settings.</p>"
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
        install_uppercase(self._callsign_input)
        form.addRow("Callsign:", self._callsign_input)

        # v0.3.4: optional operator-info fields.  Empty submissions are
        # fine — they leave the corresponding AppConfig field at its
        # current value (empty on a fresh install).
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. Kevin")
        form.addRow("Name:", self._name_input)

        self._grid_input = QLineEdit()
        self._grid_input.setPlaceholderText("e.g. EM29")
        # Maidenhead locators run to 8 characters (extended square, e.g.
        # EM29LK12); 6 cut off operators who use the extended form.
        self._grid_input.setMaxLength(GRID_MAX_LEN)
        # Force uppercase as the user types — Maidenhead is conventionally
        # rendered with the field letters uppercase.
        install_uppercase(self._grid_input)
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
