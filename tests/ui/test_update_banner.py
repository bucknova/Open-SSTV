# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the top-of-window update banner."""
from __future__ import annotations

import pytest

from open_sstv.ui.update_banner import UpdateBanner

pytestmark = pytest.mark.gui


def test_banner_hidden_until_update(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = UpdateBanner()
    qtbot.addWidget(banner)
    assert banner.isHidden()


def test_banner_shows_version_and_escapes(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = UpdateBanner()
    qtbot.addWidget(banner)
    banner.show_update("9.9.9<b>", 'https://github.com/x"y')
    assert not banner.isHidden()
    assert "9.9.9&lt;b&gt;" in banner.message()
    assert "&quot;" in banner.message()


def test_banner_dismiss(qtbot) -> None:  # type: ignore[no-untyped-def]
    banner = UpdateBanner()
    qtbot.addWidget(banner)
    banner.show_update("9.9.9", "https://github.com/bucknova/Open-SSTV/releases/latest")
    banner._close.click()
    assert banner.isHidden()
