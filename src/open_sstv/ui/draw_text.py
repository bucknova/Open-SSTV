# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable text-overlay renderer for PIL images.

Extracted from ``ImageEditorDialog._draw_text`` so the QSO template
system and the image editor can share the same rendering logic.

Overlay sizing and placement are both auto-fit:

* **Auto-shrink** — if the text is wider than the image at the
  requested font size (minus margins on both sides), the font size is
  reduced one point at a time down to :data:`_MIN_FONT_SIZE` until it
  fits.  This matters on narrow modes (Martin M2 at 160 × 256, Scottie
  S2, M4, S4) where the Exchange template's ``UR {rst} {date}`` overlay
  at 20 pt used to render wider than 160 pixels and spill off the
  right edge.
* **Clamp to bounds** — the final ``(x, y)`` is clamped so the text's
  four-direction shadow never crosses the image edge.  Applies to both
  named presets (``Top Center`` of an extra-wide string used to produce
  negative ``x``) and explicit Custom X/Y coordinates from the image
  editor spin boxes.
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


#: Pixel margin between text and the nearest image edge when using a
#: named position preset.  Also used as the auto-fit width budget:
#: ``effective_max_width = image_width - 2 × _MARGIN``.
_MARGIN: int = 8

#: Lower bound for the auto-shrink loop.  Below this the text is
#: unreadable anyway, so we stop shrinking and accept clipping for
#: extreme callsigns on the narrowest modes.
_MIN_FONT_SIZE: int = 8

#: Shadow offset in pixels on each side (the 4-direction shadow in
#: :func:`draw_text_overlay` draws at ±1 px).  Used to compute the
#: clamp budget so the shadow ring doesn't spill off-image either.
_SHADOW_PAD: int = 1


def position_to_xy(
    position: str,
    image_size: tuple[int, int],
    text_size: tuple[int, int],
) -> tuple[int, int]:
    """Compute pixel ``(x, y)`` for a named position preset.

    Does **not** clamp to image bounds — the caller is expected to
    clamp the result via :func:`clamp_xy_to_image` before drawing.
    Kept separate so the image editor can offer the preset value as a
    starting point for manual X/Y editing.

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


def clamp_xy_to_image(
    x: int,
    y: int,
    image_size: tuple[int, int],
    text_size: tuple[int, int],
) -> tuple[int, int]:
    """Clamp ``(x, y)`` so a text box of ``text_size`` stays on-image.

    Accounts for the 1 px drop-shadow ring painted by
    :func:`draw_text_overlay`, so the clamp leaves at least 1 px of
    margin on each side for the shadow.  If the text box itself is
    wider (or taller) than the available image area, the clamp pins
    the top-left at 1 px and accepts clipping on the far edge —
    callers should use :func:`draw_text_overlay`'s auto-shrink to
    avoid this when possible.
    """
    iw, ih = image_size
    tw, th = text_size
    # Budget that leaves shadow padding on each side.
    max_x = max(_SHADOW_PAD, iw - tw - _SHADOW_PAD)
    max_y = max(_SHADOW_PAD, ih - th - _SHADOW_PAD)
    clamped_x = max(_SHADOW_PAD, min(x, max_x))
    clamped_y = max(_SHADOW_PAD, min(y, max_y))
    return clamped_x, clamped_y


def _measure(draw: ImageDraw.ImageDraw, text: str, size: int) -> tuple[ImageFont.FreeTypeFont, int, int]:
    """Load the default font at *size* pt and return ``(font, tw, th)``.

    Helper so the auto-shrink loop doesn't duplicate the bbox math.
    """
    font = ImageFont.load_default(size=size)
    bbox = draw.textbbox((0, 0), text, font=font)
    return font, bbox[2] - bbox[0], bbox[3] - bbox[1]


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

    Text is automatically fit to the image:

    * If the requested font *size* produces text wider than
      ``image_width − 2 × _MARGIN``, the size is reduced one point at
      a time down to :data:`_MIN_FONT_SIZE` until it fits.
    * The final ``(x, y)`` — from either the *position* preset or the
      explicit *x* / *y* arguments — is clamped so the shadow ring
      never leaves the image.

    Parameters
    ----------
    draw:
        A ``PIL.ImageDraw.ImageDraw`` context.
    image_size:
        ``(width, height)`` of the target image.
    text:
        The string to render.  Empty strings are no-ops.
    position:
        One of :data:`POSITIONS`.  Ignored when both *x* and *y* are
        provided.
    size:
        Requested font size in pixels.  May be reduced automatically;
        see the auto-shrink description above.
    color:
        RGB tuple for the text fill.
    x, y:
        Explicit pixel coordinates.  When both are set, they override
        the *position* preset.  When either is ``None``, the position
        preset is used.  Either way the result is clamped to image
        bounds.
    """
    if not text:
        return

    iw, ih = image_size
    # Budget for the text width that leaves the named-preset margins
    # on each side.  Text wider than this gets auto-shrunk.
    max_text_width = max(_MIN_FONT_SIZE, iw - 2 * _MARGIN)

    # Pillow >= 10.1 (pinned in pyproject) supports the size kwarg.
    font, tw, th = _measure(draw, text, size)
    effective_size = size
    while tw > max_text_width and effective_size > _MIN_FONT_SIZE:
        effective_size -= 1
        font, tw, th = _measure(draw, text, effective_size)

    if x is not None and y is not None:
        px, py = x, y
    else:
        px, py = position_to_xy(position, image_size, (tw, th))

    # Clamp to image bounds so the shadow never spills off-image and
    # the text's right/bottom edge stays visible when the preset's
    # centring math would otherwise have produced a negative coord
    # (the Exchange template's narrow-mode overflow bug).
    px, py = clamp_xy_to_image(px, py, image_size, (tw, th))

    shadow_color = (0, 0, 0)
    for dx, dy in [(1, 1), (-1, -1), (1, -1), (-1, 1)]:
        draw.text((px + dx, py + dy), text, fill=shadow_color, font=font)
    draw.text((px, py), text, fill=color, font=font)


__all__ = [
    "POSITIONS",
    "clamp_xy_to_image",
    "draw_text_overlay",
    "position_to_xy",
]
