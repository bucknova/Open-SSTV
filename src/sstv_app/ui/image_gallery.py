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

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image as PILImageModule
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QListView, QMenu

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

#: Qt user-data role for the on-disk path (str) of the saved image.
#: Storing a Path keeps the PIL Image out of memory between activations.
_IMAGE_PATH_ROLE = Qt.ItemDataRole.UserRole + 1
_MODE_ROLE = Qt.ItemDataRole.UserRole + 2

#: Module-level temp directory; all gallery images are saved here.
#: Cleaned up automatically when the process exits.
_GALLERY_TMPDIR: str = tempfile.mkdtemp(prefix="open-sstv-gallery-")
atexit.register(shutil.rmtree, _GALLERY_TMPDIR, ignore_errors=True)


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
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.doubleClicked.connect(self._on_double_clicked)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def add_image(self, image: "PILImage", mode: Mode) -> None:
        """Prepend a freshly decoded image to the gallery strip.

        The PIL image is written to a temp file immediately so the
        in-memory object can be released by the caller. Thumbnails are
        rendered from the original before it is saved so we never need
        to reload just for the strip. Oldest entries beyond ``_MAX_IMAGES``
        have their temp files deleted before the item is removed from the
        model.
        """
        pixmap = _pil_to_pixmap(image).scaled(
            _THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Save image to the gallery temp dir; use row count to make a
        # unique-ish name (timestamp would work too, but monotonic count
        # is simpler and avoids the strftime dependency).
        img_path = Path(_GALLERY_TMPDIR) / f"img_{id(image)}.png"
        image.save(str(img_path))

        item = QStandardItem(QIcon(pixmap), mode.value)
        item.setEditable(False)
        item.setData(str(img_path), _IMAGE_PATH_ROLE)
        item.setData(mode, _MODE_ROLE)
        self._model.insertRow(0, item)

        while self._model.rowCount() > _MAX_IMAGES:
            last_row = self._model.rowCount() - 1
            evicted = self._model.item(last_row)
            if evicted is not None:
                old_path = evicted.data(_IMAGE_PATH_ROLE)
                if old_path:
                    Path(old_path).unlink(missing_ok=True)
            self._model.removeRow(last_row)

    def clear(self) -> None:
        self._model.clear()

    def count(self) -> int:
        return self._model.rowCount()

    def _on_double_clicked(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        image = _load_item_image(item)
        mode = item.data(_MODE_ROLE)
        if image is not None and mode is not None:
            self.image_activated.emit(image, mode)

    def _on_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        mode = item.data(_MODE_ROLE)
        if mode is None:
            return

        menu = QMenu(self)
        save_action = menu.addAction("Save As\u2026")
        copy_action = menu.addAction("Copy to Clipboard")
        action = menu.exec(self.mapToGlobal(pos))

        if action == save_action or action == copy_action:
            image = _load_item_image(item)
            if image is None:
                return
            if action == save_action:
                self.image_activated.emit(image, mode)
            else:
                QApplication.clipboard().setPixmap(_pil_to_pixmap(image))


def _load_item_image(item: QStandardItem) -> "PILImage | None":
    """Load the PIL Image for a gallery item from its on-disk temp file."""
    path_str = item.data(_IMAGE_PATH_ROLE)
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    return PILImageModule.open(path).copy()  # .copy() detaches from the file handle


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
