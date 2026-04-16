# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable text-overlay renderer for PIL images.

Extracted from ``ImageEditorDialog._draw_text`` so the QSO template
system and the image editor can share the same rendering logic.
"""
from __future__ import annotations

from PIL import ImageDraw, ImageFont


#: Position names accepted by :func:`draw_text_overlay`.
POSITIONS = (
    "Top Left",
    "Top Center",
    "Top Right",
    "Center",
    "Bottom Left",
    "Bottom Center",
    "Bottom Right",
)


_MARGIN: int = 8


def position_to_xy(
    position: str,
    image_size: tuple[int, int],
    text_size: tuple[int, int],
) -> tuple[int, int]:
    """Compute pixel ``(x, y)`` for a named position preset.

    Parameters
    ----------
    position:
        One of :data:`POSITIONS`.
    image_size:
        ``(width, height)`` of the target image.
    text_size:
        ``(text_width, text_height)`` of the rendered text bounding box.

    Returns
    -------
    tuple[int, int]
        ``(x, y)`` in image-space pixels.
    """
    iw, ih = image_size
    tw, th = text_size
    margin = _MARGIN
    pos_map = {
        "Top Left": (margin, margin),
        "Top Center": ((iw - tw) // 2, margin),
        "Top Right": (iw - tw - margin, margin),
        "Center": ((iw - tw) // 2, (ih - th) // 2),
        "Bottom Left": (margin, ih - th - margin),
        "Bottom Center": ((iw - tw) // 2, ih - th - margin),
        "Bottom Right": (iw - tw - margin, ih - th - margin),
    }
    return pos_map.get(position, (margin, margin))


def draw_text_overlay(
    draw: ImageDraw.ImageDraw,
    image_size: tuple[int, int],
    text: str,
    position: str = "Bottom Center",
    size: int = 24,
    color: tuple[int, int, int] = (255, 255, 255),
    *,
    x: int | None = None,
    y: int | None = None,
) -> None:
    """Render *text* onto *draw* with a 4-direction shadow for readability.

    Parameters
    ----------
    draw:
        A ``PIL.ImageDraw.ImageDraw`` context.
    image_size:
        ``(width, height)`` of the target image.
    text:
        The string to render.
    position:
        One of :data:`POSITIONS`.  Ignored when both *x* and *y* are
        provided.
    size:
        Font size in pixels.
    color:
        RGB tuple for the text fill.
    x, y:
        Explicit pixel coordinates.  When both are set, they override the
        *position* preset.  When either is ``None``, the position preset
        is used.
    """
    # Pillow >= 10.1 (pinned in pyproject) supports the size kwarg.
    # The TypeError fallback that used to guard 10.0 was dropped in
    # v0.1.29 when the minimum was bumped (OP-32).
    font = ImageFont.load_default(size=size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    if x is not None and y is not None:
        px, py = x, y
    else:
        px, py = position_to_xy(position, image_size, (tw, th))

    shadow_color = (0, 0, 0)
    for dx, dy in [(1, 1), (-1, -1), (1, -1), (-1, 1)]:
        draw.text((px + dx, py + dy), text, fill=shadow_color, font=font)
    draw.text((px, py), text, fill=color, font=font)


__all__ = ["POSITIONS", "draw_text_overlay", "position_to_xy"]
