# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest-qt smoke tests for ``open_sstv.ui.tx_panel.TxPanel``.

These exercise widget state transitions (button enable/disable, status
text, signal emission) without launching the full main window.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from open_sstv.core.modes import Mode
from open_sstv.ui.tx_panel import TxPanel

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


def test_initial_state_enables_transmit_with_default_image(panel: TxPanel) -> None:
    # The TX panel auto-loads the bundled testimage.jpg on __init__, so
    # the transmit button is enabled from the start (v0.2.15+).
    assert panel._transmit_btn.isEnabled()
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
    # The default test image is auto-loaded on __init__, so transmit stays
    # enabled even when a subsequent load_image call fails (the previous
    # valid image is still in memory).
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


def test_export_to_audio_click_emits_signal(
    qtbot, panel: TxPanel, gradient_path: Path
) -> None:
    """Clicking Export to Audio emits the composited image + selected mode.

    v0.3.10 added this in-panel button to replace the v0.3.9 File-menu
    Encode Image to Audio item.  The signal payload mirrors
    transmit_requested so MainWindow can hand the same composite to the
    offline encode worker.
    """
    panel.load_image(gradient_path)
    with qtbot.waitSignal(
        panel.export_to_audio_requested, timeout=1000
    ) as blocker:
        panel._export_btn.click()
    image, mode = blocker.args
    assert image.size == (100, 100)
    assert mode in Mode


def test_set_transmitting_toggles_button_state(
    panel: TxPanel, gradient_path: Path
) -> None:
    panel.load_image(gradient_path)

    panel.set_transmitting(True)
    assert not panel._transmit_btn.isEnabled()
    assert panel._stop_btn.isEnabled()
    assert not panel._load_btn.isEnabled()
    assert not panel._mode_combo.isEnabled()
    # Export mirrors Transmit's enable state — disabled while TX is live.
    assert not panel._export_btn.isEnabled()

    panel.set_transmitting(False)
    assert panel._transmit_btn.isEnabled()
    assert not panel._stop_btn.isEnabled()
    assert panel._load_btn.isEnabled()
    assert panel._mode_combo.isEnabled()
    assert panel._export_btn.isEnabled()


def test_mode_combo_lists_all_modes(panel: TxPanel) -> None:
    items = [panel._mode_combo.itemData(i) for i in range(panel._mode_combo.count())]
    assert set(items) == set(Mode)


def test_selected_mode_returns_combo_choice(panel: TxPanel) -> None:
    panel._mode_combo.setCurrentIndex(0)
    assert isinstance(panel.selected_mode(), Mode)


# ---------------------------------------------------------------------------
# v0.1.37 — TX target outline + status label
# ---------------------------------------------------------------------------


class TestTxTargetStatus:
    """The TX panel shows a small status label + dashed outline so the
    user can see at a glance whether the loaded image matches the
    currently-selected mode's aspect, or will be stretched on TX.
    """

    def test_default_image_shows_status(self, panel: TxPanel) -> None:
        """The bundled testimage.jpg auto-loads on init, so the status
        label is non-empty from the start (v0.2.15+)."""
        assert panel._tx_target_status.text() != ""

    def test_aspect_match_shows_green_status(
        self, panel: TxPanel, tmp_path: Path
    ) -> None:
        """A 320×256 source against Martin M1 (320×256) is a perfect
        match — status label mentions 'matches' and uses the
        green-flavoured colour scheme."""
        big = Image.new("RGB", (320, 256), color=(64, 128, 192))
        path = tmp_path / "m1.png"
        big.save(path)
        panel.load_image(path)

        # Pick Martin M1
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M1:
                panel._mode_combo.setCurrentIndex(i)
                break

        status = panel._tx_target_status.text()
        assert "matches" in status.lower()
        assert "martin_m1" in status

    def test_aspect_mismatch_shows_amber_status(
        self, panel: TxPanel, tmp_path: Path
    ) -> None:
        """A 320×256 source (M1 aspect) against Martin M3 (320×128,
        different aspect) warns that the image will be stretched.

        M3 rather than M2 because M2 is 320×256 like M1 — it halves the
        pixel clock, not the frame (issue #65).  M3 halves the line count,
        so it is the narrowest genuine aspect change we ship."""
        big = Image.new("RGB", (320, 256), color=(64, 128, 192))
        path = tmp_path / "src.png"
        big.save(path)
        panel.load_image(path)

        # Pick Martin M3
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M3:
                panel._mode_combo.setCurrentIndex(i)
                break

        status = panel._tx_target_status.text()
        assert "mismatch" in status.lower()
        assert "stretched" in status.lower()
        assert "martin_m3" in status

    def test_mode_change_updates_status(
        self, panel: TxPanel, tmp_path: Path
    ) -> None:
        """Changing modes after an image is loaded refreshes the
        status label — the original bug (TX preview stays on M1
        when the user switches modes) is fixed by the outline + status
        update on mode change.  Uses M3 as the second mode because it
        is the one that actually changes aspect; M2 shares M1's frame."""
        big = Image.new("RGB", (320, 256), color=(64, 128, 192))
        path = tmp_path / "src.png"
        big.save(path)
        panel.load_image(path)

        # Select M1 → match
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M1:
                panel._mode_combo.setCurrentIndex(i)
                break
        status_m1 = panel._tx_target_status.text()
        assert "matches" in status_m1.lower()

        # Select M3 → mismatch
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M3:
                panel._mode_combo.setCurrentIndex(i)
                break
        status_m3 = panel._tx_target_status.text()
        assert "mismatch" in status_m3.lower()

        assert status_m1 != status_m3, (
            "TX target status label must refresh on mode change — "
            "this is the v0.1.37 user-reported bug regression guard."
        )
