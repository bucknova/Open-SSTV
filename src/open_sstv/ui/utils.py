# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Qt/PIL utility helpers for the UI package.

Kept small intentionally — only helpers that are genuinely used by more
than one UI module belong here. DSP helpers live in ``core/``; audio
helpers live in ``audio/``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from PySide6.QtWidgets import QWidget


def pil_to_qimage(image: PILImage) -> QImage:
    """Convert a PIL ``Image`` to a ``QImage`` without leaking buffers.

    Going via ``Image.tobytes`` + ``QImage`` is the portable path —
    ``PIL.ImageQt`` is deprecated in newer Pillow releases and depends on
    Qt bindings being present at Pillow import time.

    ``QImage.copy()`` is mandatory: the raw-data ``QImage`` constructor
    keeps a pointer into the Python bytes object, which gets freed when
    this function returns. Copying gives Qt its own buffer.
    """
    rgb = image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    return QImage(
        data,
        rgb.width,
        rgb.height,
        rgb.width * 3,
        QImage.Format.Format_RGB888,
    ).copy()


def pil_to_pixmap(image: PILImage) -> QPixmap:
    """Convert a PIL ``Image`` to a ``QPixmap`` (see :func:`pil_to_qimage`).

    Prefer :func:`pil_to_qimage` for data that may outlive the GUI —
    e.g. clipboard contents. A ``QPixmap`` placed on the clipboard
    becomes a pasteboard *promise* that requires a living
    ``QGuiApplication`` to honour; if the app quits first, Qt warns
    ("Cannot keep promise…") and teardown can crash. ``QImage`` carries
    no such dependency.
    """
    return QPixmap.fromImage(pil_to_qimage(image))


def scaled_pixmap_for(
    source: QPixmap,
    widget: QWidget,
    size: QSize | None = None,
    aspect: Qt.AspectRatioMode = Qt.AspectRatioMode.KeepAspectRatio,
) -> QPixmap:
    """Scale *source* to fill *size* (default: *widget*'s size) sharply.

    Scaling to the logical size and letting Qt draw it on a 2x display
    upscales a second time, so previews looked soft on HiDPI screens
    (v0.6.14 audit).  This scales to device pixels and tags the result with
    the ratio, so Qt draws it 1:1.
    """
    dpr = widget.devicePixelRatioF()
    target = size if size is not None else widget.size()
    out = source.scaled(
        target * dpr, aspect, Qt.TransformationMode.SmoothTransformation
    )
    out.setDevicePixelRatio(dpr)
    return out


__all__ = ["pil_to_pixmap", "pil_to_qimage", "scaled_pixmap_for"]
