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
