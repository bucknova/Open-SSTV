# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoded image gallery widget.

A horizontal strip of thumbnails showing the most recent N decoded SSTV
images. New images are prepended so the newest decode is always
left-most. Listens to nothing directly — the parent panel calls
``add_image`` in response to ``RxWorker.image_complete``.

Interaction model:

* Single-click: fires ``image_preview_requested(PIL.Image, Mode)`` so
  the parent panel can swap its main preview to the clicked image —
  lets the user review older decodes at full size without having to
  save them to disk first (v0.2.7).
* Double-click: fires ``image_activated(PIL.Image, Mode)`` for the
  parent to surface in a save dialog.
* Right-click: context menu with *View* (same as single-click),
  *Save As…*, and *Copy to Clipboard*.
"""
from __future__ import annotations

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

    image_activated = Signal(object, object)  # (PIL.Image, Mode) — save dialog
    #: v0.2.7: emitted on single-click (or context-menu *View*) so the
    #: parent panel can swap its main preview to the clicked thumbnail.
    #: Lets the user review prior decodes at full size without the
    #: previous save-and-reopen dance.
    image_preview_requested = Signal(object, object)  # (PIL.Image, Mode)

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
        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # Try to create a per-instance temp directory for disk-backed storage.
        # On failure, _tmpdir is None and we fall back to in-memory PIL images.
        # OP-21: cleanup is wired to QCoreApplication.aboutToQuit (scoped to
        # the app lifetime) instead of atexit (scoped to the interpreter).
        # This avoids accumulating one atexit callback per widget instance
        # in long test sessions, and runs cleanup at the proper moment in
        # Qt's shutdown sequence.
        self._tmpdir: str | None = None
        # OP2-04: monotonic counter for temp-file names so memory-address
        # reuse after GC can't make two gallery entries point at the same file.
        self._image_counter: int = 0
        try:
            self._tmpdir = tempfile.mkdtemp(prefix="open-sstv-gallery-")
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._cleanup_tmpdir)
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
            img_path = Path(self._tmpdir) / f"img_{self._image_counter}.png"
            self._image_counter += 1
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
                # In the in-memory fallback path the PIL image lives in
                # the item's data; ``removeRow`` should drop the QStandardItem
                # but Qt's PyObject ownership across the C++ boundary has bit
                # us before — explicitly null the role so the PIL handle is
                # released even if a stray Python reference (e.g. a slot
                # closure) still holds the item itself.
                evicted.setData(None, _PIL_IMAGE_ROLE)
            self._model.removeRow(last_row)

    def clear(self) -> None:
        # Delete all temp files before clearing the model.
        for row in range(self._model.rowCount()):
            item = self._model.item(row)
            if item is not None:
                old_path = item.data(_IMAGE_PATH_ROLE)
                if old_path:
                    Path(old_path).unlink(missing_ok=True)
                # Symmetric with the eviction path: drop the in-memory PIL
                # ref before model.clear() so the handle is gone even if
                # something else still references the item.
                item.setData(None, _PIL_IMAGE_ROLE)
        self._model.clear()

    def count(self) -> int:
        return self._model.rowCount()

    @staticmethod
    def _coerce_mode(mode: object) -> Mode | None:
        """Rehydrate a ``Mode`` from Qt item data.

        Qt's ``QStandardItem.setData`` flattens ``StrEnum`` values to
        plain ``str``, so ``item.data(_MODE_ROLE)`` returns ``"robot_36"``
        rather than ``Mode.ROBOT_36``.  Coerce back here so every signal
        this widget emits carries a real ``Mode`` — consumers shouldn't
        have to know about Qt's unwrapping quirk.  Returns ``None`` if
        the stored value isn't a recognised mode (defensive — should
        never happen in practice).
        """
        if mode is None:
            return None
        if isinstance(mode, Mode):
            return mode
        try:
            return Mode(str(mode))
        except ValueError:
            return None

    def _on_clicked(self, index) -> None:
        """Single-click: surface the thumbnail in the parent's main preview.

        Emits ``image_preview_requested`` so ``RxPanel`` can swap the
        large preview pixmap to the clicked image (v0.2.7).  Double-click
        still routes to ``image_activated`` → save dialog; the two
        signals are intentionally distinct so a user who just wants to
        look at an older decode doesn't get a save dialog in their face.
        """
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        image = _load_item_image(item)
        mode = self._coerce_mode(item.data(_MODE_ROLE))
        if image is not None and mode is not None:
            self.image_preview_requested.emit(image, mode)

    def _on_double_clicked(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        image = _load_item_image(item)
        mode = self._coerce_mode(item.data(_MODE_ROLE))
        if image is not None and mode is not None:
            self.image_activated.emit(image, mode)

    def _on_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        if self._coerce_mode(item.data(_MODE_ROLE)) is None:
            return

        menu = QMenu(self)
        # v0.2.7: *View* mirrors the single-click behaviour so the
        # interaction is discoverable via the context menu too.  Kept
        # as the first entry because "look at it" is the most common
        # intent after "what images do I have?".
        menu.addAction("View")
        menu.addAction("Save As\u2026")
        menu.addAction("Copy to Clipboard")
        action = menu.exec(self.mapToGlobal(pos))
        if action is not None:
            self._dispatch_context_action(item, action.text())

    def _dispatch_context_action(
        self, item: QStandardItem, label: str
    ) -> None:
        """Run a context-menu action on ``item``.

        Extracted from ``_on_context_menu`` so the action handling is
        testable without having to drive a live ``QMenu`` — monkey-
        patching ``QMenu.exec`` at the Python level doesn't replace the
        C++-backed slot, so tests call this helper directly with the
        desired action label.
        """
        mode = self._coerce_mode(item.data(_MODE_ROLE))
        if mode is None:
            return
        image = _load_item_image(item)
        if image is None:
            return
        if label == "View":
            self.image_preview_requested.emit(image, mode)
        elif label.startswith("Save As"):
            self.image_activated.emit(image, mode)
        elif label == "Copy to Clipboard":
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
