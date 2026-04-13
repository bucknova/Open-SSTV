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
from PySide6.QtCore import QRectF, Qt, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPen,
    QPixmap,
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
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sstv_app.core.modes import MODE_TABLE, Mode
from sstv_app.ui.draw_text import draw_text_overlay

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


def _pil_to_pixmap(image: "PILImage") -> QPixmap:
    """Convert a PIL Image to a QPixmap."""
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    qimage = QImage(
        data,
        rgb.width,
        rgb.height,
        rgb.width * 3,
        QImage.Format.Format_RGB888,
    ).copy()
    return QPixmap.fromImage(qimage)


class _CropRect(QGraphicsRectItem):
    """Draggable crop rectangle constrained to an aspect ratio.

    The user can drag the entire rectangle or resize from the edges.
    For simplicity, drag moves the whole rect; resize isn't interactive
    — the user adjusts crop via spinboxes.
    """

    def __init__(self, rect: QRectF, parent=None) -> None:
        super().__init__(rect, parent)
        self.setPen(QPen(QColor(255, 255, 0), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(255, 255, 0, 30)))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, True)


class ImageEditorDialog(QDialog):
    """Modal dialog for cropping, resizing, and adding text to images."""

    def __init__(
        self,
        image: "PILImage",
        mode: Mode,
        callsign: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Image")
        self.setMinimumSize(800, 600)
        self.resize(1000, 700)

        self._original_image = image.copy()
        self._working_image = image.copy()
        self._mode = mode
        self._spec = MODE_TABLE[mode]
        self._callsign = callsign
        self._text_overlays: list[dict] = []
        self._result_image: PILImage | None = None

        # Target aspect ratio
        self._target_w = self._spec.width
        self._target_h = self._spec.height
        self._aspect = self._target_w / self._target_h

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
        ])
        self._text_position.setCurrentText("Bottom Left")
        self._text_position.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        pos_row.addWidget(self._text_position, stretch=1)
        text_form.addLayout(pos_row)

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
        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
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
        self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        # Restore crop rect from saved geometry
        if saved_rect is not None:
            self._crop_rect_item = _CropRect(saved_rect)
            self._scene.addItem(self._crop_rect_item)

        iw, ih = self._working_image.size
        self._info_label.setText(
            f"Image: {iw}x{ih}\n"
            f"Target: {self._target_w}x{self._target_h} ({self._mode.value})\n"
            f"Text layers: {len(self._text_overlays)}"
        )

    def _build_display_image(self) -> "PILImage":
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
        self._scene.addItem(self._crop_rect_item)

    def _apply_crop(self) -> None:
        """Crop the working image to the current selection."""
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

        self._working_image = self._working_image.crop((x, y, x + w, y + h))
        self._crop_rect_item = None
        self._refresh_preview()
        self._auto_fit_crop()

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

    def _add_text_overlay(self) -> None:
        text = self._text_input.text().strip()
        if not text:
            return
        overlay = {
            "text": text,
            "size": self._font_size.value(),
            "color": (
                self._text_color.red(),
                self._text_color.green(),
                self._text_color.blue(),
            ),
            "position": self._text_position.currentText(),
        }
        self._text_overlays.append(overlay)
        self._text_list.addItem(
            f'"{text}" {overlay["size"]}px @ {overlay["position"]}'
        )
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
        img = self._build_display_image()
        # Resize to mode-native dimensions
        img = img.resize(
            (self._target_w, self._target_h),
            Image.Resampling.LANCZOS,
        )
        self._result_image = img
        self.accept()

    def result_image(self) -> "PILImage | None":
        """Return the edited image, or None if the dialog was cancelled."""
        return self._result_image


__all__ = ["ImageEditorDialog"]
