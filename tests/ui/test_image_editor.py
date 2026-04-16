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
#
# Previously this class was @pytest.mark.skip'd with the claim that it
# required a display. It doesn't — the QGraphicsScene/QGraphicsRectItem
# machinery runs fine under the offscreen Qt platform used by pytest-qt.
# OP-04 (v0.1.27) unskipped the class.


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

    def test_drag_syncs_spinboxes_to_rect_position(
        self, editor: ImageEditorDialog
    ) -> None:
        """_on_crop_rect_dragged must update the spinboxes to match the
        drag position, with signals blocked so the rect isn't rebuilt.

        The previous version of this test asserted the rect item identity
        stayed the same (no rebuild), which is the actual contract of
        _on_crop_rect_dragged: it only writes the spinboxes with signals
        blocked.
        """
        assert editor._crop_rect_item is not None
        before_item = editor._crop_rect_item

        # Simulate drag: _on_crop_rect_dragged writes spinboxes with signals blocked
        editor._on_crop_rect_dragged(42, 37)

        # Spinboxes reflect the drag position
        assert editor._crop_x.value() == 42
        assert editor._crop_y.value() == 37
        # Rect item is the same object (no unintended rebuild from drag)
        assert editor._crop_rect_item is before_item, (
            "drag must not rebuild the crop rect item"
        )


# ---------------------------------------------------------------------------
# v0.1.30 — Apply Crop resizes to target dimensions in one click
# ---------------------------------------------------------------------------


class TestApplyCropResizesToTarget:
    """Apply Crop must both (a) crop to the selection *and* (b) resize to
    the target SSTV mode's native dimensions in a single click, so the
    preview matches what will actually be transmitted.

    Prior to v0.1.30 the resize happened only in ``_on_accept``, so an
    image whose aspect ratio already matched the target (e.g. an 800×600
    photo into a 4:3 Robot 36 slot) came out of Apply Crop at its input
    size — the button visibly did nothing, and users had to click OK and
    reopen the editor to see the 320×240 result.
    """

    def test_same_aspect_image_resizes_to_target(self, qtbot) -> None:
        """800×600 (4:3) cropped for Robot 36 (320×240, 4:3) must end up
        at 320×240 after Apply Crop, not 800×600."""
        src = Image.new("RGB", (800, 600), (100, 150, 200))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)

        assert dlg._working_image.size == (800, 600), "initial load"

        # Auto-fit Crop (runs in __init__) already produced a full-image
        # crop box since the aspect matches; click Apply Crop.
        dlg._apply_crop()

        assert dlg._working_image.size == (320, 240), (
            f"Apply Crop should resize to target 320×240, got "
            f"{dlg._working_image.size}"
        )

    def test_wider_image_cropped_then_resized(self, qtbot) -> None:
        """An 800×400 image (2:1 aspect) into Robot 36 (4:3) should crop
        to 533×400 at the target aspect, then resize to 320×240."""
        src = Image.new("RGB", (800, 400), (50, 50, 50))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)

        # Auto-fit produced: crop_h=400, crop_w=533 (400 × 4/3)
        dlg._apply_crop()

        assert dlg._working_image.size == (320, 240)

    def test_manual_small_crop_upscales_to_target(self, qtbot) -> None:
        """If the user manually chooses a small crop region (160×120 out
        of a 320×240 image), Apply Crop still resizes to target 320×240
        even though that means upscaling.  This is the documented
        behaviour — the editor's job is to produce a target-sized image.
        """
        src = Image.new("RGB", (320, 240), (0, 255, 0))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)

        dlg._crop_x.setValue(80)
        dlg._crop_y.setValue(60)
        dlg._crop_w.setValue(160)
        dlg._crop_h.setValue(120)
        dlg._apply_crop()

        assert dlg._working_image.size == (320, 240)

    def test_apply_crop_then_ok_is_pixel_equivalent(self, qtbot) -> None:
        """Apply-Crop-then-OK produces the same final image dimensions
        as OK-without-Apply-Crop (the old path that did the resize
        silently).  Guards against a regression where the double-resize
        somehow ended up at wrong dimensions.
        """
        src = Image.new("RGB", (800, 600), (200, 100, 50))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)

        dlg._apply_crop()
        dlg._on_accept()

        result = dlg.result_image()
        assert result is not None
        assert result.size == (320, 240)

    def test_larger_target_mode_resizes_up(self, qtbot) -> None:
        """An image smaller than the target gets upscaled to target dims
        (PD-290 is 800×616). Apply Crop must end at target dims."""
        src = Image.new("RGB", (400, 308), (50, 100, 200))  # half PD-290
        dlg = ImageEditorDialog(src, Mode.PD_290)
        qtbot.addWidget(dlg)

        dlg._apply_crop()

        assert dlg._working_image.size == (800, 616)


class TestRefreshPreviewSceneRect:
    """The scene rect must match the working image's current size so the
    view renders at the right scale.  Tracks the v0.1.31 fix where the
    preview was being fit-to-view unconditionally, hiding the 800×600 →
    320×240 size change from the user (both 4:3, both filled the view
    identically).
    """

    def test_scene_rect_matches_working_image_after_apply_crop(
        self, qtbot
    ) -> None:
        """Apply Crop shrinks the working image to target size; the scene
        rect the view renders must match that new size, not the
        pre-crop size."""
        src = Image.new("RGB", (800, 600), (100, 100, 100))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)

        dlg._apply_crop()

        # Working image is 320×240; scene rect must be too.
        scene_rect = dlg._scene.sceneRect()
        assert scene_rect.width() == 320
        assert scene_rect.height() == 240

    def test_view_transform_is_identity_for_small_image(
        self, qtbot
    ) -> None:
        """When the pixmap is smaller than the viewport (typical case
        after cropping-and-resizing to 320×240), the view transform
        must be reset to identity (1:1 pixel scale) so the user sees
        the image at its actual size — not stretched to fill the view.
        The previous behaviour (fitInView unconditionally) made every
        4:3 preview look identical regardless of resolution.
        """
        from PySide6.QtWidgets import QApplication

        src = Image.new("RGB", (800, 600), (200, 200, 200))
        dlg = ImageEditorDialog(src, Mode.ROBOT_36)
        qtbot.addWidget(dlg)
        dlg.show()  # view needs a viewport size to make the comparison
        qtbot.waitExposed(dlg)
        QApplication.processEvents()

        dlg._apply_crop()
        QApplication.processEvents()

        # View transform should be identity (m11 == 1.0, m22 == 1.0)
        # because 320×240 fits inside the dialog's allocated view area.
        t = dlg._view.transform()
        assert abs(t.m11() - 1.0) < 0.01, (
            f"View transform m11 (x-scale) should be 1.0 for a "
            f"320×240 image inside a larger viewport, got {t.m11()}"
        )
        assert abs(t.m22() - 1.0) < 0.01, (
            f"View transform m22 (y-scale) should be 1.0 for a "
            f"320×240 image inside a larger viewport, got {t.m22()}"
        )

    def test_view_scales_down_when_image_exceeds_viewport(
        self, qtbot
    ) -> None:
        """A very large target (PD-290 is 800×616) may still exceed a
        small viewport — in that case ``fitInView`` must run to scale
        the scene down.  Guards against a "never scale" regression."""
        from PySide6.QtWidgets import QApplication

        src = Image.new("RGB", (800, 616), (50, 50, 50))
        dlg = ImageEditorDialog(src, Mode.PD_290)
        qtbot.addWidget(dlg)
        # Force the dialog to a small size so 800×616 exceeds the view.
        dlg.resize(500, 400)
        dlg.show()
        qtbot.waitExposed(dlg)
        QApplication.processEvents()

        dlg._refresh_preview()
        QApplication.processEvents()

        # Should have scaled down — m11 and m22 should be < 1.0.
        t = dlg._view.transform()
        assert t.m11() < 1.0, (
            f"800×616 image in a 500×400 dialog should have been "
            f"scaled down, got m11={t.m11()}"
        )
