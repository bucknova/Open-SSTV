# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest-qt smoke tests for ``sstv_app.ui.tx_panel.TxPanel``.

These exercise widget state transitions (button enable/disable, status
text, signal emission) without launching the full main window.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from sstv_app.core.modes import Mode
from sstv_app.ui.tx_panel import TxPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def panel(qtbot) -> TxPanel:
    p = TxPanel()
    qtbot.addWidget(p)
    return p


@pytest.fixture
def gradient_path(tmp_path: Path) -> Path:
    img = Image.new("RGB", (100, 100), color=(64, 128, 192))
    path = tmp_path / "in.png"
    img.save(path)
    return path


def test_initial_state_disables_transmit(panel: TxPanel) -> None:
    assert not panel._transmit_btn.isEnabled()
    assert not panel._stop_btn.isEnabled()
    assert panel._load_btn.isEnabled()


def test_load_image_enables_transmit(panel: TxPanel, gradient_path: Path) -> None:
    panel.load_image(gradient_path)
    assert panel._transmit_btn.isEnabled()
    assert "Loaded" in panel._status.text()


def test_load_invalid_image_reports_error(panel: TxPanel, tmp_path: Path) -> None:
    bogus = tmp_path / "not_an_image.png"
    bogus.write_bytes(b"this is not a PNG")
    panel.load_image(bogus)
    assert not panel._transmit_btn.isEnabled()
    assert "Failed to load" in panel._status.text()


def test_transmit_click_emits_signal(
    qtbot, panel: TxPanel, gradient_path: Path
) -> None:
    panel.load_image(gradient_path)
    with qtbot.waitSignal(panel.transmit_requested, timeout=1000) as blocker:
        panel._transmit_btn.click()
    image, mode = blocker.args
    assert image.size == (100, 100)
    assert mode in Mode


def test_stop_click_emits_signal(qtbot, panel: TxPanel) -> None:
    panel.set_transmitting(True)  # enables the stop button
    with qtbot.waitSignal(panel.stop_requested, timeout=1000):
        panel._stop_btn.click()


def test_set_transmitting_toggles_button_state(
    panel: TxPanel, gradient_path: Path
) -> None:
    panel.load_image(gradient_path)

    panel.set_transmitting(True)
    assert not panel._transmit_btn.isEnabled()
    assert panel._stop_btn.isEnabled()
    assert not panel._load_btn.isEnabled()
    assert not panel._mode_combo.isEnabled()

    panel.set_transmitting(False)
    assert panel._transmit_btn.isEnabled()
    assert not panel._stop_btn.isEnabled()
    assert panel._load_btn.isEnabled()
    assert panel._mode_combo.isEnabled()


def test_mode_combo_lists_all_modes(panel: TxPanel) -> None:
    items = [panel._mode_combo.itemData(i) for i in range(panel._mode_combo.count())]
    assert set(items) == set(Mode)


def test_selected_mode_returns_combo_choice(panel: TxPanel) -> None:
    panel._mode_combo.setCurrentIndex(0)
    assert isinstance(panel.selected_mode(), Mode)
