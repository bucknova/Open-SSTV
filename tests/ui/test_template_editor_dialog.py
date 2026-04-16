# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for TemplateEditorDialog.

OP-03: the deep-copy constructor must preserve every ``QSOTemplateOverlay``
field including the optional ``x`` / ``y`` pixel coordinates.  Prior to the
v0.1.27 fix the dialog silently stripped these fields, so opening a template
with hand-placed coordinates in the editor (and clicking OK) permanently
erased them from disk.
"""
from __future__ import annotations

import pytest

from open_sstv.config.templates import QSOTemplate, QSOTemplateOverlay
from open_sstv.ui.template_editor_dialog import TemplateEditorDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def tpl_with_xy() -> QSOTemplate:
    """Template whose overlay carries explicit X/Y coordinates."""
    return QSOTemplate(
        name="Custom Placement",
        overlays=[
            QSOTemplateOverlay(
                text="{mycall}",
                position="Custom",
                size=24,
                color=(255, 255, 255),
                x=50,
                y=100,
            ),
            QSOTemplateOverlay(
                text="PRESET TEXT",
                position="Bottom Center",
                size=18,
                color=(255, 255, 200),
                # No x/y — this one uses the named preset.
            ),
        ],
    )


class TestOp03DeepCopyPreservesXY:
    """The dialog's internal deep copy must not drop optional fields."""

    def test_result_preserves_explicit_xy(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        result = dlg.result_templates()
        assert len(result) == 1
        overlays = result[0].overlays
        assert len(overlays) == 2

        # First overlay kept its (50, 100) coordinates.
        assert overlays[0].x == 50
        assert overlays[0].y == 100
        # Second overlay's None stays None.
        assert overlays[1].x is None
        assert overlays[1].y is None

    def test_deep_copy_isolates_caller(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        """Mutating the dialog's copy must not mutate the caller's list."""
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        # Tamper with the dialog's internal copy.
        dlg._templates[0].overlays[0].x = 999

        # Caller's original template is untouched.
        assert tpl_with_xy.overlays[0].x == 50


# ---------------------------------------------------------------------------
# v0.1.35 — template editor X/Y spin boxes (matching the image editor)
# ---------------------------------------------------------------------------


class TestTemplateEditorXYSpinboxes:
    """The template editor gained X/Y spin boxes in v0.1.35 so users
    can place template overlays at precise pixel coordinates, the same
    way the image editor has allowed since v0.1.23.  Values are in
    the editor's preview-canvas coordinate space (320 × 240); at TX
    time ``draw_text_overlay`` auto-shrinks and clamps for the real
    target mode so the values are portable across all 22 modes.
    """

    def test_loading_custom_xy_overlay_selects_custom_and_populates_spinboxes(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        """Opening a template with an x=50,y=100 Custom overlay
        puts the Position combo on 'Custom' and the spin boxes at
        the saved values."""
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        # Select the first (custom-placement) overlay in the editor.
        dlg._tpl_list.setCurrentRow(0)
        dlg._overlay_list.setCurrentRow(0)

        assert dlg._position_combo.currentText() == "Custom"
        assert dlg._text_x.value() == 50
        assert dlg._text_y.value() == 100

    def test_named_preset_overlay_seeds_spinboxes_from_preset(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        """A non-Custom overlay shows its preset in the combo AND
        seeds the X/Y spin boxes to the preset's computed position
        so the user has a meaningful starting point if they want to
        switch to Custom."""
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        dlg._tpl_list.setCurrentRow(0)
        dlg._overlay_list.setCurrentRow(1)  # the Bottom Center overlay

        assert dlg._position_combo.currentText() == "Bottom Center"
        # Seeded X/Y should be well inside the 320×240 canvas.
        assert 0 <= dlg._text_x.value() <= 320
        assert 0 <= dlg._text_y.value() <= 240
        # Bottom Center should have Y in the bottom half.
        assert dlg._text_y.value() > 120

    def test_editing_xy_flips_position_to_custom(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        """User nudging the X or Y spin box switches the combo to
        Custom so the intent sticks on save."""
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        dlg._tpl_list.setCurrentRow(0)
        dlg._overlay_list.setCurrentRow(1)
        assert dlg._position_combo.currentText() == "Bottom Center"

        # User types a new X value
        dlg._text_x.setValue(77)

        assert dlg._position_combo.currentText() == "Custom"
        # The x/y survives into the result
        result = dlg.result_templates()
        assert result[0].overlays[1].position == "Custom"
        assert result[0].overlays[1].x == 77

    def test_switching_to_named_preset_clears_xy(
        self, qtbot, tpl_with_xy: QSOTemplate
    ) -> None:
        """Selecting a named preset in the combo (away from Custom)
        clears the saved x/y so the preset takes effect.  Mirrors the
        image editor's behaviour."""
        dlg = TemplateEditorDialog([tpl_with_xy])
        qtbot.addWidget(dlg)

        dlg._tpl_list.setCurrentRow(0)
        dlg._overlay_list.setCurrentRow(0)  # the Custom overlay
        assert dlg._position_combo.currentText() == "Custom"

        # User picks Top Right from the combo
        idx = dlg._position_combo.findText("Top Right")
        dlg._position_combo.setCurrentIndex(idx)

        result = dlg.result_templates()
        assert result[0].overlays[0].position == "Top Right"
        assert result[0].overlays[0].x is None
        assert result[0].overlays[0].y is None
