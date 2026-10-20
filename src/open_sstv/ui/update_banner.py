# SPDX-License-Identifier: GPL-3.0-or-later
"""Prominent "new release available" banner shown above the main UI.

The status bar is easy to overlook, so a newer GitHub release is announced
with a strip at the very top of the main window.  The colours are an
informational blue picked from the active theme (amber is the app's warning
colour, and a bright bar above a transmitter UI would read as an alert).

Closing the banner emits :attr:`dismissed` with the version; the owner
persists it so that version stays hidden while the next release shows the
banner again.
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

#: (background, text, link, bottom border) per theme.
_LIGHT = ("#e8f1fb", "#0b3d6e", "#0b3d6e", "#9cc3ea")
_DARK = ("#12324f", "#d6e8fa", "#8cc4ff", "#2a5a86")


def _is_dark(widget: QWidget) -> bool:
    """True when the widget's palette has a dark window background."""
    return widget.palette().window().color().lightness() < 128


def banner_stylesheet(dark: bool) -> str:
    bg, fg, link, border = _DARK if dark else _LIGHT
    return (
        f"#updateBanner {{ background-color: {bg};"
        f" border-bottom: 1px solid {border}; }}"
        f"#updateBanner QLabel {{ color: {fg}; background: transparent; }}"
        f"#updateBanner QToolButton {{ border: none; color: {fg};"
        " padding: 0 6px; background: transparent; }"
        "#updateBanner QToolButton:hover { background: rgba(127,127,127,50); }"
    ), link


class UpdateBanner(QWidget):
    """Hidden until :meth:`show_update` is called."""

    #: Emitted with the version when the operator closes the banner.
    dismissed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._version = ""
        self._label = QLabel(self)
        self._label.setOpenExternalLinks(True)
        self._label.setWordWrap(True)
        self._close = QToolButton(self)
        self._close.setText("✕")
        self._close.setToolTip("Dismiss (this version only)")
        self._close.clicked.connect(self._on_close)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 6, 5)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._close)
        self.hide()

    def show_update(self, version: str, url: str) -> None:
        """Announce ``version``; both values came off the network, so escape."""
        self._version = version
        sheet, link = banner_stylesheet(_is_dark(self))
        self.setStyleSheet(sheet)
        self._label.setText(
            f"New release available: Open-SSTV {html.escape(version)} — "
            f'<a href="{html.escape(url, quote=True)}" '
            f'style="color:{link}">Download / release notes</a>'
        )
        self.show()

    def message(self) -> str:
        return self._label.text()

    def _on_close(self) -> None:
        self.hide()
        self.dismissed.emit(self._version)
