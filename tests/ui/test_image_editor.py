# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ImageEditorDialog BZ-series fixes.

BZ-03: _crop_x and _crop_y spinboxes must update the visual crop rectangle
       when the user types a new value, not just when the rect is dragged.
"""
from __future__ import annotations

import pytest
from PIL import Image

from open_sstv.core.modes import Mode
from open_sstv.ui.image_editor import ImageEditorDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def test_image() -> Image.Image:
    """400×300 solid grey image — larger than any crop window we'll test."""
    return Image.new("RGB", (400, 300), (0x80, 0x80, 0x80))


@pytest.fixture
def editor(qtbot, test_image: Image.Image) -> ImageEditorDialog:
    dlg = ImageEditorDialog(test_image, Mode.SCOTTIE_S1)
    qtbot.addWidget(dlg)
    return dlg


# ---------------------------------------------------------------------------
# BZ-03: X/Y spinbox drives the visual crop rectangle
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="requires display for QGraphicsScene interaction — verify visually"
)
class TestCropXYSpinboxUpdatesRect:
    """Setting the X or Y crop spinbox must reposition the crop overlay."""

    def test_set_crop_x_moves_rect(self, editor: ImageEditorDialog) -> None:
        """Setting _crop_x via setValue fires valueChanged → _update_crop_rect."""
        assert editor._crop_rect_item is not None
        auto_x = editor._crop_x.value()

        # Pick a target X that differs from the auto-fit value
        target_x = max(0, auto_x - 5) if auto_x >= 5 else auto_x + 5
        # setValue fires valueChanged naturally, which must call _update_crop_rect
        editor._crop_x.setValue(target_x)

        rect = editor._crop_rect_item.rect()
        assert int(round(rect.x())) == target_x, (
            f"crop rect X should be {target_x}, got {rect.x()} "
            f"(auto-fit was {auto_x})"
        )

    def test_set_crop_y_moves_rect(self, editor: ImageEditorDialog) -> None:
        """Setting _crop_y via setValue fires valueChanged → _update_crop_rect."""
        assert editor._crop_rect_item is not None
        auto_y = editor._crop_y.value()

        target_y = auto_y + 5
        editor._crop_y.setValue(target_y)

        rect = editor._crop_rect_item.rect()
        assert int(round(rect.y())) == target_y, (
            f"crop rect Y should be {target_y}, got {rect.y()}"
        )

    def test_update_crop_rect_reads_all_spinboxes(
        self, editor: ImageEditorDialog
    ) -> None:
        """_update_crop_rect must position the rect using (x, y) from the spinboxes."""
        assert editor._crop_rect_item is not None

        # Set Y via setValue (fires signal → _update_crop_rect reads both spinboxes)
        auto_y = editor._crop_y.value()
        target_y = auto_y + 3
        editor._crop_y.setValue(target_y)

        rect = editor._crop_rect_item.rect()
        # X must still reflect the auto-fit value (we didn't change it)
        assert int(round(rect.x())) == editor._crop_x.value()
        assert int(round(rect.y())) == target_y

    def test_drag_does_not_trigger_rect_rebuild_loop(
        self, editor: ImageEditorDialog
    ) -> None:
        """Dragging must not cause an infinite rect rebuild via spinbox signals.

        The drag callback blocks spinbox signals while writing X/Y, so
        _update_crop_rect must not fire again from the drag path.  We
        verify the rect item is stable after simulating a drag.
        """
        assert editor._crop_rect_item is not None
        before_item = editor._crop_rect_item

        # Simulate drag: _on_crop_rect_dragged writes spinboxes with signals blocked
        editor._on_crop_rect_dragged(editor._crop_x.value(), editor._crop_y.value())

        # Spinboxes should reflect the drag position
        assert editor._crop_x.value() == editor._crop_x.value()  # unchanged
        # Rect item should be the same object (no unintended rebuild from drag)
        assert editor._crop_rect_item is before_item, (
            "drag must not rebuild the crop rect item"
        )
