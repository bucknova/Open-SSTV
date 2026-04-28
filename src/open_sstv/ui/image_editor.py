# SPDX-License-Identifier: GPL-3.0-or-later
"""Image editor dialog for cropping, resizing, and adding text overlays.

Used from the TX panel to prepare images before SSTV transmission.
The editor operates on ``PIL.Image.Image`` objects so the result is
exactly what the encoder will process.

Workflow
--------

1. User loads an image in the TX panel.
2. User clicks "Edit Image..." which opens this dialog.
3. The editor shows the image with crop handles locked to the target
   mode's aspect ratio.
4. User can add text overlays (callsign, labels, etc.).
5. On accept, the edited PIL image is returned to the TX panel.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image, ImageDraw
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPen,
)
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.ui.draw_text import draw_text_overlay, position_to_xy
from open_sstv.ui.utils import pil_to_pixmap as _pil_to_pixmap

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image as PILImage


#: Editor working-image cap.  A phone photograph (4032 × 3024, say)
#: overwhelmed the viewport under the v0.1.34 strict-1:1 rendering —
#: the crop rectangle lived far outside the visible area and the user
#: had to pan-and-scroll constantly to select a region.
#:
#: We pre-shrink oversized sources on entry to the editor so the
#: working image never exceeds either 3× the target mode's dimensions
#: or a hard absolute ceiling.  LANCZOS preserves enough detail for
#: the final target-size resize to be visually indistinguishable from
#: resizing the raw source: 2–3× the target gives the filter plenty of
#: headroom, and the user's cropping coordinates only need to pick a
#: semantically-meaningful region of the image — sub-pixel precision
#: at the source resolution is not useful for a 320 × 240 or 640 × 496
#: transmission.
_EDITOR_MAX_SOURCE_W: int = 1280
_EDITOR_MAX_SOURCE_H: int = 960
_EDITOR_TARGET_MULTIPLIER: int = 3


def _shrink_source_for_editor(
    image: PILImage,
    target_w: int,
    target_h: int,
) -> PILImage:
    """Return a copy of *image* scaled down to a size that's comfortable
    to edit at, preserving aspect ratio and never upscaling.

    The cap is the smaller of ``_EDITOR_TARGET_MULTIPLIER × target``
    and the absolute ``_EDITOR_MAX_SOURCE_W × _EDITOR_MAX_SOURCE_H``.
    If the source already fits inside both caps, a plain copy is
    returned.

    Output quality is not a concern here: the working image is what
    the user crops and manipulates; the final transmission is always
    produced by a separate LANCZOS resize to the target mode's
    dimensions, which would discard most of the source's pixels
    anyway.

    Examples
    --------
    4032 × 3024 phone photo → Robot 36 (320 × 240): capped at
    ``min(3 × 320, 1280) = 960`` wide and ``min(3 × 240, 960) = 720``
    tall.  Source scales by ``min(960 / 4032, 720 / 3024) = 0.238``
    → 960 × 720 working image.
    """
    iw, ih = image.size
    max_w = min(_EDITOR_TARGET_MULTIPLIER * target_w, _EDITOR_MAX_SOURCE_W)
    max_h = min(_EDITOR_TARGET_MULTIPLIER * target_h, _EDITOR_MAX_SOURCE_H)
    # Never make the editor working image smaller than the target
    # itself — the user still needs at least target-resolution pixels
    # to pick a meaningful crop region from.
    max_w = max(target_w, max_w)
    max_h = max(target_h, max_h)
    if iw <= max_w and ih <= max_h:
        return image.copy()
    scale = min(max_w / iw, max_h / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


class _CropRect(QGraphicsRectItem):
    """Draggable crop rectangle constrained to an aspect ratio.

    The user can drag the entire rectangle or resize from the edges.
    For simplicity, drag moves the whole rect; resize isn't interactive
    — the user adjusts crop via spinboxes.

    When the rect is dragged, ``itemChange`` fires with the new
    position.  If ``on_moved`` is set (a callable), it is called with
    the effective scene-space origin ``(x, y)`` so the parent dialog
    can sync its spinboxes.
    """

    on_moved: Callable[[int, int], None] | None = None

    def __init__(self, rect: QRectF, parent=None) -> None:
        super().__init__(rect, parent)
        self.setPen(QPen(QColor(255, 255, 0), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(255, 255, 0, 30)))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(
            QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges, True,
        )

    def itemChange(self, change, value):
        if (
            change == QGraphicsRectItem.GraphicsItemChange.ItemPositionHasChanged
            and self.on_moved is not None
        ):
            # The effective top-left in scene space is rect().topLeft() + pos().
            origin = self.rect().topLeft() + value
            self.on_moved(int(round(origin.x())), int(round(origin.y())))
        return super().itemChange(change, value)


class ImageEditorDialog(QDialog):
    """Modal dialog for cropping, resizing, and adding text to images."""

    def __init__(
        self,
        image: PILImage,
        mode: Mode,
        callsign: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Image")
        self.setMinimumSize(800, 600)
        self.resize(1000, 700)

        self._mode = mode
        self._spec = MODE_TABLE[mode]
        self._callsign = callsign
        self._text_overlays: list[dict] = []
        self._result_image: PILImage | None = None
        self._result_base_image: PILImage | None = None

        # Target aspect ratio — use display_height for PD modes where
        # spec.height is the sync-pulse count (half the actual image height).
        self._target_w = self._spec.width
        self._target_h = self._spec.display_height
        self._aspect = self._target_w / self._target_h

        # Pre-shrink oversized sources so the editor viewport stays
        # comfortable to work in.  Phone photos routinely come in at
        # 4000+ pixels wide; strict-1:1 rendering of those was the
        # user's "scrolling nightmare" complaint.  The working image
        # is capped at the smaller of 3× target or 1280 × 960, which
        # is plenty of resolution for the final LANCZOS resize to
        # target to produce an indistinguishable result.  The original
        # (pre-shrink) image is kept only implicitly — Reset reverts
        # to the shrunk copy, because everything the editor does
        # (crop, rotate, flip, text overlays) operates in working-image
        # coordinates that already get down-scaled to target on OK.
        self._original_image = _shrink_source_for_editor(
            image, self._target_w, self._target_h,
        )
        self._working_image = self._original_image.copy()

        layout = QHBoxLayout(self)

        # Left: image view
        left = QVBoxLayout()
        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHints(
            self._view.renderHints()
            | self._view.renderHints().__class__.Antialiasing
        )
        self._view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        # v0.1.34: always render the scene at 1:1 native pixel scale.
        # Previously the view used ``fitInView`` (which stretches to
        # fill the viewport) or a mixed-mode "fit if bigger, 1:1 if
        # smaller" approach; both made Apply Crop's pixel-count change
        # invisible because the viewport was filled either way.  With
        # strict 1:1 display, a 800×600 source image is visibly bigger
        # than a 320×240 cropped result in the same viewport, so the
        # resolution change is unmissable.  Large images scroll via
        # the native scrollbars; the image is centered in the viewport
        # when it fits entirely.
        self._view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # Darker canvas so the image edges are visibly distinct from
        # the viewport background, especially after a shrink.
        self._view.setBackgroundBrush(QBrush(QColor(40, 40, 40)))
        left.addWidget(self._view, stretch=1)

        # Toolbar rows: crop actions on row 1, transforms on row 2.
        # Split across two rows so buttons don't get cramped at small sizes.
        crop_toolbar = QHBoxLayout()
        crop_toolbar.addWidget(
            QLabel(f"Target: {self._target_w}\u00d7{self._target_h}")
        )
        crop_toolbar.addStretch()
        self._fit_btn = QPushButton("Auto-fit Crop")
        self._fit_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._fit_btn.clicked.connect(self._auto_fit_crop)
        crop_toolbar.addWidget(self._fit_btn)
        self._apply_crop_btn = QPushButton("Apply Crop")
        self._apply_crop_btn.setToolTip(
            "Crop to the yellow selection and resize to the target mode's "
            "native dimensions.  The preview updates immediately to the "
            "exact pixel size that will be transmitted."
        )
        self._apply_crop_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._apply_crop_btn.clicked.connect(self._apply_crop)
        crop_toolbar.addWidget(self._apply_crop_btn)
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._reset_btn.clicked.connect(self._reset_image)
        crop_toolbar.addWidget(self._reset_btn)
        left.addLayout(crop_toolbar)

        transform_toolbar = QHBoxLayout()
        self._rot_left_btn = QPushButton("\u21b6 Rotate Left")
        self._rot_left_btn.setToolTip("Rotate 90\u00b0 counter-clockwise")
        self._rot_left_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._rot_left_btn.clicked.connect(lambda: self._rotate(90))
        transform_toolbar.addWidget(self._rot_left_btn)
        self._rot_right_btn = QPushButton("\u21b7 Rotate Right")
        self._rot_right_btn.setToolTip("Rotate 90\u00b0 clockwise")
        self._rot_right_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._rot_right_btn.clicked.connect(lambda: self._rotate(-90))
        transform_toolbar.addWidget(self._rot_right_btn)
        self._flip_h_btn = QPushButton("\u2194 Flip H")
        self._flip_h_btn.setToolTip("Flip horizontally")
        self._flip_h_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._flip_h_btn.clicked.connect(self._flip_horizontal)
        transform_toolbar.addWidget(self._flip_h_btn)
        self._flip_v_btn = QPushButton("\u2195 Flip V")
        self._flip_v_btn.setToolTip("Flip vertically")
        self._flip_v_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._flip_v_btn.clicked.connect(self._flip_vertical)
        transform_toolbar.addWidget(self._flip_v_btn)
        transform_toolbar.addStretch()
        left.addLayout(transform_toolbar)

        layout.addLayout(left, stretch=2)

        # Right: tools panel — fixed minimum width so controls never collapse.
        right = QVBoxLayout()

        # --- Crop controls ---
        crop_group = QGroupBox("Crop")
        crop_group.setMinimumWidth(220)
        crop_form = QVBoxLayout(crop_group)
        crop_row1 = QHBoxLayout()
        crop_row1.addWidget(QLabel("X:"))
        self._crop_x = QSpinBox()
        self._crop_x.setRange(0, max(1, image.width - 1))
        self._crop_x.setMinimumWidth(70)
        crop_row1.addWidget(self._crop_x, stretch=1)
        crop_row1.addWidget(QLabel("Y:"))
        self._crop_y = QSpinBox()
        self._crop_y.setRange(0, max(1, image.height - 1))
        self._crop_y.setMinimumWidth(70)
        crop_row1.addWidget(self._crop_y, stretch=1)
        crop_form.addLayout(crop_row1)

        crop_row2 = QHBoxLayout()
        crop_row2.addWidget(QLabel("W:"))
        self._crop_w = QSpinBox()
        self._crop_w.setRange(1, image.width)
        self._crop_w.setValue(image.width)
        self._crop_w.setMinimumWidth(70)
        crop_row2.addWidget(self._crop_w, stretch=1)
        crop_row2.addWidget(QLabel("H:"))
        self._crop_h = QSpinBox()
        self._crop_h.setRange(1, image.height)
        self._crop_h.setValue(image.height)
        self._crop_h.setMinimumWidth(70)
        crop_row2.addWidget(self._crop_h, stretch=1)
        crop_form.addLayout(crop_row2)

        self._lock_aspect = QPushButton("Lock Aspect Ratio")
        self._lock_aspect.setCheckable(True)
        self._lock_aspect.setChecked(True)
        crop_form.addWidget(self._lock_aspect)
        self._crop_w.valueChanged.connect(self._on_crop_w_changed)
        # Typing X or Y into the spinboxes must move the visual crop rect.
        # _on_crop_rect_dragged blocks these signals on drag-sync so there
        # is no circular feedback loop between the spinboxes and the item.
        self._crop_x.valueChanged.connect(lambda _: self._update_crop_rect())
        self._crop_y.valueChanged.connect(lambda _: self._update_crop_rect())

        right.addWidget(crop_group)

        # --- Text overlay controls ---
        text_group = QGroupBox("Text Overlay")
        text_group.setMinimumWidth(220)
        text_form = QVBoxLayout(text_group)

        self._text_input = QLineEdit()
        self._text_input.setPlaceholderText("Enter text (e.g. callsign)")
        if callsign:
            self._text_input.setText(callsign)
        text_form.addWidget(self._text_input)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Size:"))
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 120)
        self._font_size.setValue(24)
        self._font_size.setMinimumWidth(60)
        size_row.addWidget(self._font_size, stretch=1)

        self._text_color_btn = QPushButton("Color")
        self._text_color = QColor(255, 255, 255)
        self._text_color_btn.setStyleSheet(
            f"background-color: {self._text_color.name()};"
        )
        self._text_color_btn.clicked.connect(self._pick_text_color)
        size_row.addWidget(self._text_color_btn)
        text_form.addLayout(size_row)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Position:"))
        self._text_position = QComboBox()
        self._text_position.addItems([
            "Top Left", "Top Center", "Top Right",
            "Center",
            "Bottom Left", "Bottom Center", "Bottom Right",
            "Custom",
        ])
        self._text_position.setCurrentText("Bottom Left")
        self._text_position.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._text_position.currentTextChanged.connect(self._on_position_preset_changed)
        pos_row.addWidget(self._text_position, stretch=1)
        text_form.addLayout(pos_row)

        # Fine X/Y adjustment — auto-filled from the Position preset,
        # manually editable for pixel-precise placement.
        xy_row = QHBoxLayout()
        xy_row.addWidget(QLabel("X:"))
        self._text_x = QSpinBox()
        self._text_x.setRange(0, self._target_w)
        self._text_x.setSingleStep(5)
        self._text_x.setSuffix(" px")
        self._text_x.valueChanged.connect(self._on_text_xy_changed)
        xy_row.addWidget(self._text_x, stretch=1)
        xy_row.addWidget(QLabel("Y:"))
        self._text_y = QSpinBox()
        self._text_y.setRange(0, self._target_h)
        self._text_y.setSingleStep(5)
        self._text_y.setSuffix(" px")
        self._text_y.valueChanged.connect(self._on_text_xy_changed)
        xy_row.addWidget(self._text_y, stretch=1)
        text_form.addLayout(xy_row)
        # Seed initial X/Y from the default preset.
        self._sync_xy_from_preset()

        add_row = QHBoxLayout()
        self._add_text_btn = QPushButton("Add Text")
        self._add_text_btn.clicked.connect(self._add_text_overlay)
        add_row.addWidget(self._add_text_btn)
        self._remove_text_btn = QPushButton("Remove")
        self._remove_text_btn.clicked.connect(self._remove_text_overlay)
        add_row.addWidget(self._remove_text_btn)
        text_form.addLayout(add_row)

        self._text_list = QListWidget()
        self._text_list.setMinimumHeight(60)
        self._text_list.setMaximumHeight(120)
        text_form.addWidget(self._text_list)

        right.addWidget(text_group)

        # --- Preview info ---
        # Styled with a slight background + bold so the current pixel
        # size is obvious at a glance.  Users reported overlooking the
        # plain label (same font weight as other labels) and thinking
        # Apply Crop had done nothing when the numbers were the only
        # visible indicator that the resolution had changed.
        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet(
            "QLabel { "
            "  background: palette(alternate-base); "
            "  border: 1px solid palette(mid); "
            "  border-radius: 3px; "
            "  padding: 6px 8px; "
            "  font-weight: bold; "
            "}"
        )
        right.addWidget(self._info_label)

        right.addStretch()

        # --- Dialog buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)

        layout.addLayout(right, stretch=1)

        # Initial display
        self._crop_rect_item: _CropRect | None = None
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._refresh_preview()
        self._auto_fit_crop()

    # === Preview ===

    def _refresh_preview(self) -> None:
        """Rebuild the scene from the working image + overlays."""
        display = self._build_display_image()
        pixmap = _pil_to_pixmap(display)

        # Save the crop rect geometry *before* scene.clear() destroys
        # the underlying C++ QGraphicsRectItem.
        saved_rect: QRectF | None = None
        if self._crop_rect_item is not None:
            try:
                saved_rect = self._crop_rect_item.rect()
            except RuntimeError:
                # C++ object already deleted (e.g. by a prior clear)
                saved_rect = None
        self._crop_rect_item = None

        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))

        # v0.1.34: render the scene at 1:1 pixel scale unconditionally.
        # ``fitInView`` and the prior mixed-mode approach both scaled
        # the viewport to match the scene (or vice-versa), which made
        # Apply Crop's resolution change visually invisible when the
        # cropped image kept the same aspect ratio as the source.
        # Resetting the transform to identity once per refresh and
        # relying on the view's own ``AlignCenter`` alignment +
        # ``ScrollBarAsNeeded`` policy (set in ``__init__``) means
        # the image always occupies exactly ``image_width × image_height``
        # pixels of the viewport.  A 320×240 preview after Apply Crop
        # takes up noticeably less real estate than the 800×600
        # source it came from, so the resolution change is visible
        # at a glance without having to read the info label.
        self._view.resetTransform()
        # Force a redraw right away so the user doesn't have to wait
        # for the next event-loop iteration to see the change.
        self._view.viewport().update()

        # Restore crop rect from saved geometry
        if saved_rect is not None:
            self._crop_rect_item = _CropRect(saved_rect)
            self._crop_rect_item.on_moved = self._on_crop_rect_dragged
            self._scene.addItem(self._crop_rect_item)

        iw, ih = self._working_image.size
        self._info_label.setText(
            f"Image: {iw}x{ih}\n"
            f"Target: {self._target_w}x{self._target_h} ({self._mode.value})\n"
            f"Text layers: {len(self._text_overlays)}"
        )

    def _build_display_image(self) -> PILImage:
        """Working image with text overlays burned in."""
        img = self._working_image.copy()
        draw = ImageDraw.Draw(img)
        for overlay in self._text_overlays:
            self._draw_text(draw, img.size, overlay)
        return img

    @staticmethod
    def _draw_text(
        draw: ImageDraw.ImageDraw,
        image_size: tuple[int, int],
        overlay: dict,
    ) -> None:
        """Render a single text overlay onto the draw context."""
        draw_text_overlay(
            draw,
            image_size,
            text=overlay["text"],
            position=overlay["position"],
            size=overlay["size"],
            color=overlay["color"],
            x=overlay.get("x"),
            y=overlay.get("y"),
        )

    # === Crop ===

    def _auto_fit_crop(self) -> None:
        """Calculate the largest centered crop at the target aspect ratio."""
        iw, ih = self._working_image.size
        # Find largest rect at target aspect that fits within the image
        if iw / ih > self._aspect:
            # Image is wider than target: constrain by height
            crop_h = ih
            crop_w = int(round(ih * self._aspect))
        else:
            # Image is taller than target: constrain by width
            crop_w = iw
            crop_h = int(round(iw / self._aspect))

        crop_x = (iw - crop_w) // 2
        crop_y = (ih - crop_h) // 2

        # Update spinboxes (block signals to avoid feedback loop)
        self._crop_x.blockSignals(True)
        self._crop_y.blockSignals(True)
        self._crop_w.blockSignals(True)
        self._crop_h.blockSignals(True)
        self._crop_x.setMaximum(max(0, iw - 1))
        self._crop_y.setMaximum(max(0, ih - 1))
        self._crop_w.setMaximum(iw)
        self._crop_h.setMaximum(ih)
        self._crop_x.setValue(crop_x)
        self._crop_y.setValue(crop_y)
        self._crop_w.setValue(crop_w)
        self._crop_h.setValue(crop_h)
        self._crop_x.blockSignals(False)
        self._crop_y.blockSignals(False)
        self._crop_w.blockSignals(False)
        self._crop_h.blockSignals(False)

        # Update visual crop rect
        self._update_crop_rect()

    def _on_crop_w_changed(self, w: int) -> None:
        if self._lock_aspect.isChecked():
            h = max(1, int(round(w / self._aspect)))
            self._crop_h.blockSignals(True)
            self._crop_h.setValue(h)
            self._crop_h.blockSignals(False)
        self._update_crop_rect()

    def _update_crop_rect(self) -> None:
        """Sync the visual crop rectangle to the spinbox values."""
        x = self._crop_x.value()
        y = self._crop_y.value()
        w = self._crop_w.value()
        h = self._crop_h.value()

        rect = QRectF(x, y, w, h)
        if self._crop_rect_item is not None:
            try:
                self._scene.removeItem(self._crop_rect_item)
            except RuntimeError:
                pass  # C++ object already deleted by scene.clear()
        self._crop_rect_item = _CropRect(rect)
        self._crop_rect_item.on_moved = self._on_crop_rect_dragged
        self._scene.addItem(self._crop_rect_item)

    def _on_crop_rect_dragged(self, x: int, y: int) -> None:
        """Called when the user drags the crop rectangle in the viewport.

        Syncs the X/Y spinboxes to the effective scene-space origin so
        ``_apply_crop`` reads the user's actual drag position, not the
        stale auto-fit coordinates.  Width and height stay unchanged
        (dragging only moves, it doesn't resize).
        """
        # Block signals to avoid a circular update (spinbox valueChanged →
        # _update_crop_rect → new _CropRect → loses drag state).
        self._crop_x.blockSignals(True)
        self._crop_y.blockSignals(True)
        self._crop_x.setValue(x)
        self._crop_y.setValue(y)
        self._crop_x.blockSignals(False)
        self._crop_y.blockSignals(False)

    def _apply_crop(self) -> None:
        """Crop the working image to the current selection *and* resize
        to the target SSTV mode's native dimensions.

        Performs both operations at the button click so the editor
        preview immediately reflects the final TX geometry.  Prior to
        v0.1.30 the resize to target dimensions only happened silently
        in ``_on_accept`` when the dialog was closed; if the loaded
        image already matched the target aspect ratio (e.g. an 800×600
        photo into a 4:3 Robot 36 slot), Auto-fit Crop produced a crop
        box covering the whole image and Apply Crop then cropped to
        the same size — a visual no-op that left the user thinking the
        button was broken.  They had to hit OK and reopen the editor
        to see the final resolution.  Now the resize happens in line
        with the crop so "what you see is what gets encoded."
        """
        x = self._crop_x.value()
        y = self._crop_y.value()
        w = self._crop_w.value()
        h = self._crop_h.value()

        iw, ih = self._working_image.size
        # Clamp to image bounds
        x = max(0, min(x, iw - 1))
        y = max(0, min(y, ih - 1))
        w = min(w, iw - x)
        h = min(h, ih - y)

        if w < 1 or h < 1:
            return

        # Crop to the user's selection, then resize to the target mode's
        # native resolution with LANCZOS — same filter _on_accept uses,
        # so an Apply-Crop-then-OK sequence is pixel-equivalent to the
        # old OK-only path for any image whose cropped dimensions
        # already matched the target.
        cropped = self._working_image.crop((x, y, x + w, y + h))
        self._working_image = cropped.resize(
            (self._target_w, self._target_h),
            Image.Resampling.LANCZOS,
        )
        self._crop_rect_item = None
        self._refresh_preview()
        self._auto_fit_crop()
        self._info_label.setText(
            f"Image: {self._target_w}×{self._target_h} (resized to target)\n"
            f"Target: {self._target_w}×{self._target_h} ({self._mode.value})\n"
            f"Text layers: {len(self._text_overlays)}"
        )

    def _reset_image(self) -> None:
        """Revert to the original image."""
        self._working_image = self._original_image.copy()
        self._text_overlays.clear()
        self._text_list.clear()
        self._crop_rect_item = None
        self._refresh_preview()
        self._auto_fit_crop()

    # === Rotate / flip ===

    def _rotate(self, degrees: int) -> None:
        """Rotate the working image by ``degrees`` (positive = CCW)."""
        self._working_image = self._working_image.rotate(
            degrees, expand=True,
        )
        self._crop_rect_item = None
        self._refresh_preview()
        self._auto_fit_crop()

    def _flip_horizontal(self) -> None:
        self._working_image = self._working_image.transpose(
            Image.Transpose.FLIP_LEFT_RIGHT,
        )
        self._refresh_preview()

    def _flip_vertical(self) -> None:
        self._working_image = self._working_image.transpose(
            Image.Transpose.FLIP_TOP_BOTTOM,
        )
        self._refresh_preview()

    # === Text overlay ===

    def _pick_text_color(self) -> None:
        color = QColorDialog.getColor(self._text_color, self, "Text Color")
        if color.isValid():
            self._text_color = color
            self._text_color_btn.setStyleSheet(
                f"background-color: {color.name()};"
            )

    def _sync_xy_from_preset(self) -> None:
        """Compute X/Y from the current Position preset and update the
        spin boxes.  Called when the dropdown changes to a named preset.

        Pillow >= 10.1 (pinned in pyproject) supports ``size=`` on
        ``load_default``; the pre-10.1 TypeError fallback was dropped
        in v0.1.29 (OP-32).
        """
        pos = self._text_position.currentText()
        if pos == "Custom":
            return
        text = self._text_input.text().strip() or "Ag"  # placeholder for bbox
        from PIL import Image as _PILImage
        from PIL import ImageDraw, ImageFont
        font = ImageFont.load_default(size=self._font_size.value())
        tmp = _PILImage.new("RGB", (self._target_w, self._target_h))
        draw = ImageDraw.Draw(tmp)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x, y = position_to_xy(
            pos, (self._target_w, self._target_h), (tw, th),
        )
        self._text_x.blockSignals(True)
        self._text_y.blockSignals(True)
        self._text_x.setValue(x)
        self._text_y.setValue(y)
        self._text_x.blockSignals(False)
        self._text_y.blockSignals(False)

    def _on_position_preset_changed(self, text: str) -> None:
        """When the user picks a named preset, auto-fill X/Y."""
        if text != "Custom":
            self._sync_xy_from_preset()

    def _on_text_xy_changed(self) -> None:
        """When the user manually edits X/Y, switch the dropdown to Custom."""
        if self._text_position.currentText() != "Custom":
            self._text_position.blockSignals(True)
            self._text_position.setCurrentText("Custom")
            self._text_position.blockSignals(False)

    def _add_text_overlay(self) -> None:
        text = self._text_input.text().strip()
        if not text:
            return
        pos = self._text_position.currentText()
        overlay: dict = {
            "text": text,
            "size": self._font_size.value(),
            "color": (
                self._text_color.red(),
                self._text_color.green(),
                self._text_color.blue(),
            ),
            "position": pos,
        }
        # Store explicit x/y when set (always populated from spinboxes).
        overlay["x"] = self._text_x.value()
        overlay["y"] = self._text_y.value()
        self._text_overlays.append(overlay)
        if pos == "Custom":
            label = f'"{text}" {overlay["size"]}px @ ({overlay["x"]},{overlay["y"]})'
        else:
            label = f'"{text}" {overlay["size"]}px @ {pos}'
        self._text_list.addItem(label)
        self._refresh_preview()

    def _remove_text_overlay(self) -> None:
        row = self._text_list.currentRow()
        if row >= 0 and row < len(self._text_overlays):
            self._text_overlays.pop(row)
            self._text_list.takeItem(row)
            self._refresh_preview()

    # === Dialog result ===

    def _on_accept(self) -> None:
        """Build the final image: apply overlays, resize to mode, accept."""
        size = (self._target_w, self._target_h)
        # Base image: cropped/rotated/filtered but NO text overlays.
        # Used by TxPanel as the "clean" baseline that Clear Text reverts to.
        self._result_base_image = self._working_image.copy().resize(
            size, Image.Resampling.LANCZOS,
        )
        # Full image: base + text overlays baked in.
        img = self._build_display_image()
        img = img.resize(size, Image.Resampling.LANCZOS)
        self._result_image = img
        self.accept()

    def result_image(self) -> PILImage | None:
        """Return the edited image with text overlays, or None if cancelled."""
        return self._result_image

    def result_base_image(self) -> PILImage | None:
        """Return the edited image WITHOUT text overlays, or None if cancelled.

        This is the crop/rotation/filter result before any text was drawn.
        TxPanel stores it as the "clean" baseline that Clear Text reverts to,
        so Clear Text removes both template-applied AND manually-added text.
        """
        return self._result_base_image


__all__ = ["ImageEditorDialog"]
