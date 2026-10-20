# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the top-of-window update banner."""
from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QPalette

from open_sstv.ui.update_banner import UpdateBanner

pytestmark = pytest.mark.gui


def _banner(qtbot, window_color: str) -> UpdateBanner:  # type: ignore[no-untyped-def]
    banner = UpdateBanner()
    pal = banner.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(window_color))
    banner.setPalette(pal)
    qtbot.addWidget(banner)
    return banner


def test_hidden_until_update(qtbot) -> None:  # type: ignore[no-untyped-def]
    assert _banner(qtbot, "#ffffff").isHidden()


def test_escapes_version_and_url(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = _banner(qtbot, "#ffffff")
    banner.show_update("9.9.9<b>", 'https://github.com/x"y')
    assert "9.9.9&lt;b&gt;" in banner.message()
    assert "&quot;" in banner.message()


def test_close_hides_and_emits_version(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = _banner(qtbot, "#ffffff")
    banner.show_update("9.9.9", "https://github.com/bucknova/Open-SSTV/releases/latest")
    with qtbot.waitSignal(banner.dismissed) as blocker:
        banner._close.click()
    assert blocker.args == ["9.9.9"]
    assert banner.isHidden()


def test_light_theme_colours(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = _banner(qtbot, "#f0f0f0")
    banner.show_update("9.9.9", "https://github.com/bucknova/Open-SSTV/releases/latest")
    sheet = banner.styleSheet()
    assert "#e8f1fb" in sheet and "#0b3d6e" in sheet and "#9cc3ea" in sheet


def test_dark_theme_colours(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = _banner(qtbot, "#202020")
    banner.show_update("9.9.9", "https://github.com/bucknova/Open-SSTV/releases/latest")
    sheet = banner.styleSheet()
    assert "#12324f" in sheet and "#d6e8fa" in sheet and "#2a5a86" in sheet
    assert "#8cc4ff" in banner.message()
