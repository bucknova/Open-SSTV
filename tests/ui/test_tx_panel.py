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


# ---------------------------------------------------------------------------
# v0.1.36 — QSO template with Custom x/y applies correctly
# ---------------------------------------------------------------------------


class TestCustomPositionTemplateRendering:
    """v0.1.36 regression: applying a QSO template whose overlay has
    ``position="Custom"`` with explicit x/y must render the text at
    those coordinates.

    The bug: ``TxPanel._on_template_activated`` (no-user-input branch)
    and ``QuickFillDialog.resolved_overlays`` both converted the
    overlay to a plain ``dict`` but dropped the ``x`` / ``y`` fields.
    When ``_apply_overlays`` later handed that dict to
    ``draw_text_overlay``, the ``x is None`` check sent it down the
    fallback path, ``position_to_xy("Custom", ...)`` returned
    ``(margin, margin)`` (top-left), and the user's carefully-placed
    text rendered at (8, 8).
    """

    def test_template_activation_forwards_xy(
        self, qtbot, panel: TxPanel, tmp_path: Path
    ) -> None:
        """Direct-activation path (template with no placeholders that
        require user input) must forward x/y to the rendered overlay.

        Loads a 320×240 blue canvas, applies a Custom-position
        template with red text at (200, 100), and asserts red pixels
        appear near (200, 100) but NOT at top-left (where the bug
        used to put them).
        """
        from open_sstv.config.templates import QSOTemplate, QSOTemplateOverlay

        # Use a sized canvas that matches Robot 36's native resolution
        # so the target coords (200, 100) land well inside.
        big = Image.new("RGB", (320, 240), color=(64, 128, 192))
        img_path = tmp_path / "big.png"
        big.save(img_path)
        panel.load_image(img_path)
        panel._callsign = "W0AEZ"

        tpl = QSOTemplate(
            name="Custom Place",
            overlays=[
                QSOTemplateOverlay(
                    text="CUSTOM",
                    position="Custom",
                    size=14,
                    color=(255, 0, 0),
                    x=200,
                    y=100,
                ),
            ],
        )

        panel._on_template_activated(tpl)

        # Any red pixel in the image proves the overlay rendered.
        assert panel._current_image is not None
        found_red_near_target = False
        for dx in range(-10, 60):
            for dy in range(-10, 30):
                px = 200 + dx
                py = 100 + dy
                if 0 <= px < panel._current_image.width and 0 <= py < panel._current_image.height:
                    r, g, b = panel._current_image.getpixel((px, py))[:3]
                    if r > 200 and g < 80 and b < 80:
                        found_red_near_target = True
                        break
            if found_red_near_target:
                break

        assert found_red_near_target, (
            "Custom-position overlay did not render near (200, 100); "
            "x/y are likely being dropped in the template-activation path."
        )

        # Negative check: top-left (where the bug rendered) should
        # NOT have any red pixels.
        top_left_has_red = False
        for dx in range(0, 50):
            for dy in range(0, 30):
                if dx < panel._current_image.width and dy < panel._current_image.height:
                    r, g, b = panel._current_image.getpixel((dx, dy))[:3]
                    if r > 200 and g < 80 and b < 80:
                        top_left_has_red = True
                        break
            if top_left_has_red:
                break
        assert not top_left_has_red, (
            "Custom-position overlay leaked to top-left corner — "
            "the x/y-dropping bug is back."
        )

    def test_quick_fill_dialog_forwards_xy(self, qtbot) -> None:
        """QuickFillDialog's ``resolved_overlays`` (used for templates
        with {theircall}/{rst} placeholders) must also forward x/y."""
        from open_sstv.config.templates import QSOTemplate, QSOTemplateOverlay
        from open_sstv.ui.quick_fill_dialog import QuickFillDialog

        tpl = QSOTemplate(
            name="Fill Me",
            overlays=[
                QSOTemplateOverlay(
                    text="{theircall} DE {mycall}",
                    position="Custom",
                    size=20,
                    color=(255, 255, 255),
                    x=123,
                    y=45,
                ),
            ],
        )
        dlg = QuickFillDialog(tpl, mycall="W0AEZ")
        qtbot.addWidget(dlg)

        # Fill in theircall
        dlg._theircall_edit.setText("K0TEST")
        dlg._on_accept()

        overlays = dlg.resolved_overlays()
        assert len(overlays) == 1
        assert overlays[0]["x"] == 123
        assert overlays[0]["y"] == 45
        assert overlays[0]["position"] == "Custom"
        assert "K0TEST" in overlays[0]["text"]
        assert "W0AEZ" in overlays[0]["text"]


# ---------------------------------------------------------------------------
# v0.1.37 — TX target outline + status label
# ---------------------------------------------------------------------------


class TestTxTargetStatus:
    """The TX panel shows a small status label + dashed outline so the
    user can see at a glance whether the loaded image matches the
    currently-selected mode's aspect, or will be stretched on TX.
    """

    def test_no_image_no_status(self, panel: TxPanel) -> None:
        """Before an image is loaded the status label is empty."""
        assert panel._tx_target_status.text() == ""

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
        """A 320×256 source (M1 aspect) against Martin M2 (160×256,
        different aspect) warns that the image will be stretched."""
        big = Image.new("RGB", (320, 256), color=(64, 128, 192))
        path = tmp_path / "src.png"
        big.save(path)
        panel.load_image(path)

        # Pick Martin M2
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M2:
                panel._mode_combo.setCurrentIndex(i)
                break

        status = panel._tx_target_status.text()
        assert "mismatch" in status.lower()
        assert "stretched" in status.lower()
        assert "martin_m2" in status

    def test_mode_change_updates_status(
        self, panel: TxPanel, tmp_path: Path
    ) -> None:
        """Changing modes after an image is loaded refreshes the
        status label — the original bug (TX preview stays on M1
        when user switches to M2) is fixed by the outline + status
        update on mode change."""
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

        # Select M2 → mismatch
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M2:
                panel._mode_combo.setCurrentIndex(i)
                break
        status_m2 = panel._tx_target_status.text()
        assert "mismatch" in status_m2.lower()

        assert status_m1 != status_m2, (
            "TX target status label must refresh on mode change — "
            "this is the v0.1.37 user-reported bug regression guard."
        )
