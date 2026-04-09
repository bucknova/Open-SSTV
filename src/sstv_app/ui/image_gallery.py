# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoded image gallery widget.

A horizontal strip of thumbnails showing the most recent N decoded SSTV
images. New images are prepended so the newest decode is always
left-most. Listens to nothing directly — the parent panel calls
``add_image`` in response to ``RxWorker.image_complete``.

v1 keeps interaction minimal: single-click selects a thumbnail (so the
user can see which one they've picked) and double-click fires the
``image_activated(PIL.Image, Mode)`` signal for the parent to surface
in a save dialog. Auto-save-to-disk is a Phase 3 setting.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QListView

from sstv_app.core.modes import Mode

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


#: Thumbnail size in pixels. 160×120 preserves 4:3 Robot 36 proportions
#: at a size that fits several thumbnails across a 640 px window without
#: crowding the rest of the panel.
_THUMB_SIZE = QSize(160, 120)

#: How many images to keep in the gallery before dropping the oldest.
#: 20 is enough for a casual listening session without growing memory
#: forever when an auto-decode loop runs overnight.
_MAX_IMAGES: int = 20

#: Qt user-data role for stashing the original PIL image on the model
#: item. We can't just keep the QPixmap — the ``image_activated`` signal
#: needs to hand the parent a ``PIL.Image`` for save-to-disk.
_PIL_IMAGE_ROLE = Qt.ItemDataRole.UserRole + 1
_MODE_ROLE = Qt.ItemDataRole.UserRole + 2


class ImageGalleryWidget(QListView):
    """Horizontal thumbnail strip for decoded SSTV images."""

    image_activated = Signal(object, object)  # (PIL.Image, Mode)

    def __init__(self, parent: QListView | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setIconSize(_THUMB_SIZE)
        self.setSpacing(6)
        # Reserve enough vertical room for the thumbnail plus the mode
        # label Qt auto-renders underneath it.
        self.setMinimumHeight(_THUMB_SIZE.height() + 40)
        self.setUniformItemSizes(True)

        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.doubleClicked.connect(self._on_double_clicked)

    def add_image(self, image: "PILImage", mode: Mode) -> None:
        """Prepend a freshly decoded image to the gallery strip.

        Oldest images beyond ``_MAX_IMAGES`` are dropped from the tail
        so memory stays bounded across long listening sessions.
        """
        pixmap = _pil_to_pixmap(image).scaled(
            _THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        item = QStandardItem(QIcon(pixmap), mode.value)
        item.setEditable(False)
        item.setData(image, _PIL_IMAGE_ROLE)
        item.setData(mode, _MODE_ROLE)
        self._model.insertRow(0, item)

        while self._model.rowCount() > _MAX_IMAGES:
            self._model.removeRow(self._model.rowCount() - 1)

    def clear(self) -> None:
        self._model.clear()

    def count(self) -> int:
        return self._model.rowCount()

    def _on_double_clicked(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        image = item.data(_PIL_IMAGE_ROLE)
        mode = item.data(_MODE_ROLE)
        if image is not None and mode is not None:
            self.image_activated.emit(image, mode)


def _pil_to_pixmap(image: "PILImage") -> QPixmap:
    """Convert a PIL ``Image`` to a ``QPixmap`` without leaking buffers.

    Going via ``Image.tobytes`` + ``QImage`` is the portable way to do
    this — ``PIL.ImageQt`` is deprecated in newer Pillow releases and
    depends on Qt bindings being installed at Pillow import time, which
    we don't want to force on users running just the CLI decoder.

    ``QImage.copy()`` is mandatory: the raw-data ``QImage`` constructor
    keeps a pointer into the Python bytes object, which gets freed when
    we return. Copying gives Qt its own buffer.
    """
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


__all__ = ["ImageGalleryWidget"]
