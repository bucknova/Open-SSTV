# SPDX-License-Identifier: GPL-3.0-or-later
"""Compact quick-fill dialog for QSO template placeholders.

When a template contains ``{theircall}`` or ``{rst}`` the operator needs
to type those values before the text can be burned onto the image.  This
dialog shows *only* the fields the selected template actually uses, with
sensible defaults and Enter-key submission so the flow is as fast as
possible during a live QSO.

The dialog remembers the last ``theircall`` value for the session so
responding to the same station multiple times doesn't require retyping.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from open_sstv.config.templates import QSOTemplate, needs_user_input, resolve_placeholders

# Session-level memory for the last entered callsign.
_last_theircall: str = ""


class QuickFillDialog(QDialog):
    """Minimal dialog that prompts only for the placeholders a template needs.

    Call :meth:`resolved_overlays` after ``exec()`` returns ``Accepted``
    to get the list of overlay dicts ready for rendering.
    """

    def __init__(
        self,
        template: QSOTemplate,
        mycall: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Fill: {template.name}")
        self._template = template
        self._mycall = mycall
        self._theircall_edit: QLineEdit | None = None
        self._rst_edit: QLineEdit | None = None

        needed = needs_user_input(template)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        if "theircall" in needed:
            self._theircall_edit = QLineEdit(_last_theircall)
            self._theircall_edit.setPlaceholderText("e.g. N0CALL")
            self._theircall_edit.selectAll()
            form.addRow("Their Call:", self._theircall_edit)

        if "rst" in needed:
            self._rst_edit = QLineEdit("59")
            self._rst_edit.setPlaceholderText("e.g. 59")
            form.addRow("RST:", self._rst_edit)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Focus the first input field
        if self._theircall_edit is not None:
            self._theircall_edit.setFocus()
        elif self._rst_edit is not None:
            self._rst_edit.setFocus()

        self.setMinimumWidth(280)

    @Slot()
    def _on_accept(self) -> None:
        global _last_theircall  # noqa: PLW0603
        if self._theircall_edit is not None:
            _last_theircall = self._theircall_edit.text().strip().upper()
        self.accept()

    def resolved_overlays(self) -> list[dict]:
        """Return overlay dicts with all placeholders substituted.

        Each dict has keys ``text``, ``position``, ``size``, ``color`` —
        ready to pass to :func:`draw_text_overlay`.
        """
        theircall = ""
        rst = "59"
        if self._theircall_edit is not None:
            theircall = self._theircall_edit.text().strip().upper()
        if self._rst_edit is not None:
            rst = self._rst_edit.text().strip()

        # x/y MUST be forwarded — see the matching fix in
        # ``TxPanel._on_template_activated``.  Dropping them caused
        # Custom-position templates to render at top-left after
        # fill-in rather than at the user's saved coordinates.
        # Fixed in v0.1.36.
        result: list[dict] = []
        for ov in self._template.overlays:
            result.append({
                "text": resolve_placeholders(
                    ov.text,
                    mycall=self._mycall,
                    theircall=theircall,
                    rst=rst,
                ),
                "position": ov.position,
                "size": ov.size,
                "color": ov.color,
                "x": ov.x,
                "y": ov.y,
            })
        return result

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Submit on Enter/Return from any field."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_accept()
        else:
            super().keyPressEvent(event)


__all__ = ["QuickFillDialog"]
