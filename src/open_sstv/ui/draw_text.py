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


def draw_text_overlay(
    draw: ImageDraw.ImageDraw,
    image_size: tuple[int, int],
    text: str,
    position: str = "Bottom Center",
    size: int = 24,
    color: tuple[int, int, int] = (255, 255, 255),
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
        One of :data:`POSITIONS`.
    size:
        Font size in pixels.
    color:
        RGB tuple for the text fill.
    """
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:
        # Pillow < 10.1 doesn't support size= on load_default
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    iw, ih = image_size
    margin = 8

    pos_map = {
        "Top Left": (margin, margin),
        "Top Center": ((iw - tw) // 2, margin),
        "Top Right": (iw - tw - margin, margin),
        "Center": ((iw - tw) // 2, (ih - th) // 2),
        "Bottom Left": (margin, ih - th - margin),
        "Bottom Center": ((iw - tw) // 2, ih - th - margin),
        "Bottom Right": (iw - tw - margin, ih - th - margin),
    }
    x, y = pos_map.get(position, (margin, margin))

    shadow_color = (0, 0, 0)
    for dx, dy in [(1, 1), (-1, -1), (1, -1), (-1, 1)]:
        draw.text((x + dx, y + dy), text, fill=shadow_color, font=font)
    draw.text((x, y), text, fill=color, font=font)


__all__ = ["POSITIONS", "draw_text_overlay"]
