# SPDX-License-Identifier: GPL-3.0-or-later
"""Horizontal button bar for QSO templates.

Shows one button per saved template plus a gear icon that opens the
template editor.  Clicking a template button emits
``template_activated`` with the corresponding ``QSOTemplate``.
"""
from __future__ import annotations

from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from sstv_app.config.templates import QSOTemplate


class QSOTemplateBar(QWidget):
    """Row of one-click QSO template buttons."""

    template_activated = Signal(object)  # QSOTemplate
    clear_text_requested = Signal()
    edit_templates_requested = Signal()

    def __init__(
        self, templates: list[QSOTemplate] | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._buttons: list[QPushButton] = []
        self._clear_btn = QPushButton("Clear Text")
        self._clear_btn.setToolTip("Remove template text from the image")
        self._clear_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._clear_btn.clicked.connect(self.clear_text_requested.emit)
        self._gear_btn = QPushButton("\u2699")  # gear icon
        self._gear_btn.setToolTip("Edit templates\u2026")
        self._gear_btn.setFixedWidth(32)
        self._gear_btn.clicked.connect(self.edit_templates_requested.emit)
        if templates:
            self.set_templates(templates)
        else:
            self._layout.addStretch()
            self._layout.addWidget(self._clear_btn)
            self._layout.addWidget(self._gear_btn)

    def set_templates(self, templates: list[QSOTemplate]) -> None:
        """Rebuild the button row from *templates*."""
        # Remove old template buttons
        for btn in self._buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._buttons.clear()

        # Remove persistent widgets temporarily
        self._layout.removeWidget(self._clear_btn)
        self._layout.removeWidget(self._gear_btn)

        # Remove any spacers
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() and item.widget() not in (self._gear_btn, self._clear_btn):
                item.widget().deleteLater()

        # Add template buttons
        for tpl in templates:
            btn = QPushButton(tpl.name)
            btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            btn.setToolTip(f"Apply \u201c{tpl.name}\u201d template")
            btn.clicked.connect(partial(self._on_template_clicked, tpl))
            self._layout.addWidget(btn)
            self._buttons.append(btn)

        self._layout.addStretch()
        self._layout.addWidget(self._clear_btn)
        self._layout.addWidget(self._gear_btn)

    def _on_template_clicked(self, tpl: QSOTemplate) -> None:
        self.template_activated.emit(tpl)


__all__ = ["QSOTemplateBar"]
