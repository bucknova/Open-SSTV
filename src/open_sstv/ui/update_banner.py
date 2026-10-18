# SPDX-License-Identifier: GPL-3.0-or-later
"""Prominent "new release available" banner shown above the main UI.

The status bar is easy to overlook, so a newer GitHub release is announced
with a coloured strip at the very top of the main window.  It stays until
the operator dismisses it (for this session only — it returns on the next
launch while the release is still newer).
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget


class UpdateBanner(QWidget):
    """Hidden until :meth:`show_update` is called."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#updateBanner { background-color: #f5b301; }"
            "#updateBanner QLabel { color: #1a1a1a; font-weight: bold; }"
            "#updateBanner QLabel a { color: #0b3d91; }"
            "#updateBanner QToolButton { border: none; color: #1a1a1a;"
            " font-weight: bold; padding: 0 6px; }"
            "#updateBanner QToolButton:hover { background: rgba(0,0,0,40); }"
        )
        self._label = QLabel(self)
        self._label.setOpenExternalLinks(True)
        self._label.setWordWrap(True)
        self._close = QToolButton(self)
        self._close.setText("✕")
        self._close.setToolTip("Dismiss until next launch")
        self._close.clicked.connect(self.hide)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 6, 5)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._close)
        self.hide()

    def show_update(self, version: str, url: str) -> None:
        """Announce ``version``; ``url`` is escaped (it came off the network)."""
        self._label.setText(
            f"🔔 New release available: Open-SSTV {html.escape(version)} — "
            f'<a href="{html.escape(url, quote=True)}">Download / release notes</a>'
        )
        self.show()

    def message(self) -> str:
        return self._label.text()
