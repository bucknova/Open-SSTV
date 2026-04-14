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
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image as PILImageModule
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QListView, QMenu

from open_sstv.core.modes import Mode
from open_sstv.ui.utils import pil_to_pixmap as _pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_log = logging.getLogger(__name__)

#: Thumbnail size in pixels. 160×120 preserves 4:3 Robot 36 proportions
#: at a size that fits several thumbnails across a 640 px window without
#: crowding the rest of the panel.
_THUMB_SIZE = QSize(160, 120)

#: How many images to keep in the gallery before dropping the oldest.
#: 20 is enough for a casual listening session without growing memory
#: forever when an auto-decode loop runs overnight.
_MAX_IMAGES: int = 20

#: Qt user-data roles.
#: _IMAGE_PATH_ROLE stores the on-disk temp path (str) when disk persistence
#: is available; None when running in in-memory fallback mode.
#: _PIL_IMAGE_ROLE stores the PIL Image directly in fallback mode.
_IMAGE_PATH_ROLE = Qt.ItemDataRole.UserRole + 1
_MODE_ROLE = Qt.ItemDataRole.UserRole + 2
_PIL_IMAGE_ROLE = Qt.ItemDataRole.UserRole + 3


class ImageGalleryWidget(QListView):
    """Horizontal thumbnail strip for decoded SSTV images.

    Normally persists decoded images to a per-instance temp directory so
    large PIL Image objects are released from memory immediately after the
    thumbnail is rendered. If temp directory creation fails (full disk,
    permission denied), falls back to keeping PIL images in Qt item data —
    the same behaviour as before v0.1.5, with a logged warning.
    """

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

        # Try to create a per-instance temp directory for disk-backed storage.
        # On failure, _tmpdir is None and we fall back to in-memory PIL images.
        self._tmpdir: str | None = None
        try:
            self._tmpdir = tempfile.mkdtemp(prefix="open-sstv-gallery-")
            atexit.register(self._cleanup_tmpdir)
        except OSError:
            _log.warning(
                "Could not create gallery temp directory — "
                "decoded images will be kept in memory instead.",
                exc_info=True,
            )

    def _cleanup_tmpdir(self) -> None:
        """Remove the temp directory on process exit (atexit callback)."""
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def add_image(self, image: "PILImage", mode: Mode) -> None:
        """Prepend a freshly decoded image to the gallery strip.

        When a temp directory is available, the PIL image is written to
        disk immediately and the in-memory object can be released by the
        caller. In fallback mode the PIL image is kept in the item's
        user data. Either way, oldest entries beyond ``_MAX_IMAGES`` are
        cleaned up (temp file deleted if applicable) before the item is
        removed from the model.
        """
        pixmap = _pil_to_pixmap(image).scaled(
            _THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        item = QStandardItem(QIcon(pixmap), mode.value)
        item.setEditable(False)
        item.setData(mode, _MODE_ROLE)

        if self._tmpdir is not None:
            img_path = Path(self._tmpdir) / f"img_{id(image)}.png"
            try:
                image.save(str(img_path))
                item.setData(str(img_path), _IMAGE_PATH_ROLE)
            except OSError:
                _log.warning("Could not save gallery image to disk; keeping in memory.", exc_info=True)
                item.setData(image, _PIL_IMAGE_ROLE)
        else:
            # In-memory fallback: store PIL image directly on the item.
            item.setData(image, _PIL_IMAGE_ROLE)

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
        # Delete all temp files before clearing the model.
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is not None:
                old_path = item.data(_IMAGE_PATH_ROLE)
                if old_path:
                    Path(old_path).unlink(missing_ok=True)
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
    """Load the PIL Image for a gallery item.

    Tries the on-disk path first (disk-backed mode); falls back to the
    in-memory PIL image stored directly on the item (fallback mode).
    """
    path_str = item.data(_IMAGE_PATH_ROLE)
    if path_str:
        path = Path(path_str)
        if path.exists():
            return PILImageModule.open(path).copy()  # .copy() detaches from file handle
        return None
    # Fallback: PIL image stored directly in item data.
    return item.data(_PIL_IMAGE_ROLE)  # type: ignore[return-value]


__all__ = ["ImageGalleryWidget"]
