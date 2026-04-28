# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Solid/Rainbow color-mode toggle in the v0.3 form-based
template editor.

The toggle:
* Surfaces a "Color mode" combo on TextLayer selection.
* Hides the Fill colour row in Rainbow mode (it has no effect on RGB
  output; only the alpha channel of layer.fill carries through).
* Writes through to the layer's ``color_mode`` field.
* Triggers a preview rebuild via the existing inspector path so the
  Fill row re-appears when switched back to Solid.
"""
from __future__ import annotations

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import (
    Template,
    TextLayer,
)
from open_sstv.ui.template_editor import TemplateEditor

pytestmark = pytest.mark.gui


def _make_editor(qtbot, *, color_mode: str = "solid") -> TemplateEditor:
    """Editor seeded with a single TextLayer at the chosen colour mode."""
    layer = TextLayer(
        id="t1",
        name="Text",
        text_raw="HELLO",
        font_family="DejaVu Sans Bold",
        font_size_pct=8.0,
        fill=(255, 255, 255, 255),
        color_mode=color_mode,  # type: ignore[arg-type]
    )
    tpl = Template(name="t", layers=[layer])
    ed = TemplateEditor(tpl, app_config=AppConfig())
    qtbot.addWidget(ed)
    ed._layer_list.setCurrentRow(0)
    return ed


class TestColorModeCombo:
    def test_combo_is_present_for_text_layer(self, qtbot) -> None:
        ed = _make_editor(qtbot)
        assert getattr(ed, "_field_color_mode", None) is not None
        items = [
            ed._field_color_mode.itemText(i)
            for i in range(ed._field_color_mode.count())
        ]
        assert items == ["Solid", "Rainbow"]

    def test_combo_reflects_existing_solid(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="solid")
        assert ed._field_color_mode.currentData() == "solid"

    def test_combo_reflects_existing_rainbow(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="rainbow")
        assert ed._field_color_mode.currentData() == "rainbow"


class TestColorModeToggleWritesThrough:
    def test_switching_to_rainbow_updates_layer(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="solid")
        rainbow_idx = ed._field_color_mode.findData("rainbow")
        assert rainbow_idx >= 0
        ed._field_color_mode.setCurrentIndex(rainbow_idx)
        assert ed._template.layers[0].color_mode == "rainbow"

    def test_switching_back_to_solid_updates_layer(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="rainbow")
        solid_idx = ed._field_color_mode.findData("solid")
        assert solid_idx >= 0
        ed._field_color_mode.setCurrentIndex(solid_idx)
        assert ed._template.layers[0].color_mode == "solid"


class TestFillRowVisibility:
    """The Fill colour picker is meaningless in Rainbow mode, so the
    inspector hides it (by skipping the row entirely on rebuild)."""

    def test_fill_button_present_in_solid_mode(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="solid")
        # In solid mode the fill button widget exists *and* is parented
        # into a visible form row.
        btn = ed._field_fill_btn
        assert btn is not None
        assert btn.parentWidget() is not None

    def test_fill_button_not_in_form_in_rainbow_mode(self, qtbot) -> None:
        """In rainbow mode the inspector skips ``form.addRow('Fill:', ...)``
        for the fill button.  The widget object is still constructed
        (so the editor can re-show it after switching back to Solid)
        but it has no parent — it is not laid out anywhere."""
        ed = _make_editor(qtbot, color_mode="rainbow")
        btn = ed._field_fill_btn
        assert btn is not None
        assert btn.parentWidget() is None, (
            "Fill button was added to a layout in Rainbow mode — "
            "it should be hidden from the form."
        )

    def test_toggle_solid_to_rainbow_hides_fill(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="solid")
        rainbow_idx = ed._field_color_mode.findData("rainbow")
        ed._field_color_mode.setCurrentIndex(rainbow_idx)
        # After the rebuild the fresh fill button has no parent.
        assert ed._field_fill_btn.parentWidget() is None

    def test_toggle_rainbow_to_solid_re_shows_fill(self, qtbot) -> None:
        ed = _make_editor(qtbot, color_mode="rainbow")
        solid_idx = ed._field_color_mode.findData("solid")
        ed._field_color_mode.setCurrentIndex(solid_idx)
        # After the rebuild the fresh fill button is laid out into the form.
        assert ed._field_fill_btn.parentWidget() is not None


class TestColorModeSurvivesLayerSwitch:
    """Selecting another layer and coming back must not lose color_mode."""

    def test_rainbow_persists_across_selection(self, qtbot) -> None:
        layer_a = TextLayer(
            id="a", name="A", text_raw="A",
            font_family="DejaVu Sans Bold", font_size_pct=8.0,
            fill=(255, 255, 255, 255), color_mode="rainbow",
        )
        layer_b = TextLayer(
            id="b", name="B", text_raw="B",
            font_family="DejaVu Sans Bold", font_size_pct=8.0,
            fill=(255, 255, 255, 255), color_mode="solid",
        )
        tpl = Template(name="t", layers=[layer_a, layer_b])
        ed = TemplateEditor(tpl, app_config=AppConfig())
        qtbot.addWidget(ed)

        ed._layer_list.setCurrentRow(0)
        assert ed._field_color_mode.currentData() == "rainbow"
        ed._layer_list.setCurrentRow(1)
        assert ed._field_color_mode.currentData() == "solid"
        ed._layer_list.setCurrentRow(0)
        assert ed._field_color_mode.currentData() == "rainbow"
        # Underlying model untouched by the trip.
        assert ed._template.layers[0].color_mode == "rainbow"
        assert ed._template.layers[1].color_mode == "solid"
