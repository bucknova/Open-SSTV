# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the +RX Image layer-add button in the v0.3 form-based template
editor.

The ``RxImageLayer`` model, TOML I/O, renderer dispatch, and inspector
type-dispatch (including the ``fit`` field handler) all already supported
RxImage; the only missing piece was the button to *create* one and the
``_build_default_layer`` branch to seed sensible defaults.  These tests
pin that wiring down so a future refactor of the button list can't silently
drop the slot type again.
"""
from __future__ import annotations

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import (
    PhotoLayer,
    RxImageLayer,
    Template,
)
from open_sstv.ui.template_editor import TemplateEditor

pytestmark = pytest.mark.gui


@pytest.fixture
def editor(qtbot) -> TemplateEditor:
    """Editor opened on a fresh empty template with default AppConfig."""
    tpl = Template(name="t")
    ed = TemplateEditor(tpl, app_config=AppConfig())
    qtbot.addWidget(ed)
    return ed


def _add_button(ed: TemplateEditor, kind: str):
    """Locate the layer-add button whose ``layer_kind`` property == kind."""
    from PySide6.QtWidgets import QPushButton

    for btn in ed.findChildren(QPushButton):
        if btn.property("layer_kind") == kind:
            return btn
    return None


class TestRxImageButtonExists:
    """The +RX Image button must be present in the layer-add row."""

    def test_button_is_rendered(self, editor: TemplateEditor) -> None:
        btn = _add_button(editor, "rx_image")
        assert btn is not None
        assert btn.text() == "+RX Image"

    def test_button_sits_alongside_existing_kinds(
        self, editor: TemplateEditor
    ) -> None:
        """All five expected layer-add buttons should be present."""
        for kind in ("text", "rect", "photo", "rx_image", "gradient"):
            assert _add_button(editor, kind) is not None, (
                f"missing +{kind} button"
            )


class TestAddRxImageLayer:
    """Clicking +RX Image must create an RxImageLayer with sensible defaults."""

    def test_click_appends_rx_image_layer(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        from PySide6.QtCore import Qt

        assert editor._template.layers == []

        btn = _add_button(editor, "rx_image")
        assert btn is not None
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        assert len(editor._template.layers) == 1
        layer = editor._template.layers[0]
        assert isinstance(layer, RxImageLayer)

    def test_default_layer_id_uses_rx_image_prefix(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        layer = editor._template.layers[0]
        assert layer.id == "rx_image_1"

    def test_id_increments_when_multiple_added(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        ids = [l.id for l in editor._template.layers]
        assert ids == ["rx_image_1", "rx_image_2"]

    def test_default_anchor_and_size(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        """Default RX slot is a ~30%×25% box pinned to bottom-right —
        the conventional inset position for received-image previews."""
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        layer = editor._template.layers[0]
        assert layer.anchor == "BR"
        assert layer.width_pct == pytest.approx(30.0)
        assert layer.height_pct == pytest.approx(25.0)

    def test_default_fit_is_cover(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        assert editor._template.layers[0].fit == "cover"

    def test_added_layer_is_selected(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        """After click the new layer becomes the current selection so the
        inspector immediately shows its properties."""
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        assert editor._layer_list.currentRow() == 0


class TestRxImageInspector:
    """Selecting an RX Image layer must surface a fit dropdown."""

    def test_fit_combo_is_populated_with_rx_image_value(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        """An RxImageLayer with fit='contain' should pre-populate the
        inspector's fit combo on selection."""
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
        # Programmatically set fit so we can verify the inspector reflects it.
        editor._template.layers[0].fit = "contain"
        # Re-select the row to rebuild the inspector form.
        editor._populate_inspector(editor._template.layers[0])

        combo = editor._field_image_fit
        assert combo is not None
        assert combo.currentText() == "contain"
        items = [combo.itemText(i) for i in range(combo.count())]
        assert items == ["contain", "cover", "stretch"]

    def test_changing_fit_updates_layer_model(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        """Changing the fit dropdown writes through to the layer model."""
        from PySide6.QtCore import Qt

        btn = _add_button(editor, "rx_image")
        qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)

        editor._field_image_fit.setCurrentText("stretch")
        assert editor._template.layers[0].fit == "stretch"

    def test_fit_combo_handler_does_not_clobber_other_image_layers(
        self, qtbot, editor: TemplateEditor
    ) -> None:
        """Sanity: the shared fit handler must still work for PhotoLayer
        when a PhotoLayer is the selected layer (regression guard for the
        isinstance check)."""
        # Add a photo layer first, then an rx_image layer.
        from PySide6.QtCore import Qt

        qtbot.mouseClick(_add_button(editor, "photo"), Qt.MouseButton.LeftButton)
        qtbot.mouseClick(
            _add_button(editor, "rx_image"), Qt.MouseButton.LeftButton
        )

        # Select the photo layer.
        editor._layer_list.setCurrentRow(0)
        assert isinstance(editor._template.layers[0], PhotoLayer)

        editor._field_image_fit.setCurrentText("contain")
        assert editor._template.layers[0].fit == "contain"
        # The rx_image layer is untouched.
        assert editor._template.layers[1].fit == "cover"
