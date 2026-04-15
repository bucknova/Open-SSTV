# SPDX-License-Identifier: GPL-3.0-or-later
"""TX header banner — stamps a thin identification strip onto the top of an image.

The banner is a full-width rectangle ``BANNER_HEIGHT`` pixels tall. It is
drawn *in-place* on a copy of the image so the original is never modified.

Layout
------
- **Centre:** "Open-SSTV v{version}"
- **Right:**  "{callsign}"  (omitted when *callsign* is empty)

The strip sits within the image bounds — dimensions are never changed — so
every SSTV mode's pixel geometry is preserved exactly.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

#: Height of the banner strip in pixels.
BANNER_HEIGHT: int = 24

#: Right-edge margin (pixels) for the callsign text.
_CALLSIGN_MARGIN: int = 4


def _load_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Return the best available embedded font, falling back gracefully.

    Pillow >= 10.1 accepts a ``size`` argument to ``load_default()``; older
    10.x releases do not.  We try the size-aware form first and fall back to
    the tiny legacy bitmap font if it is not available.
    """
    try:
        return ImageFont.load_default(size=14)  # type: ignore[call-arg]
    except TypeError:
        return ImageFont.load_default()


def apply_tx_banner(
    image: Image.Image,
    version: str,
    callsign: str = "",
    bg_color: str = "#202020",
    text_color: str = "#FFFFFF",
) -> Image.Image:
    """Return a copy of *image* with a header banner stamped across the top.

    Parameters
    ----------
    image:
        Source PIL image (any mode; typically ``"RGB"``).
    version:
        Application version string, e.g. ``"0.1.19"``.
    callsign:
        Operator callsign, e.g. ``"W0AEZ"``.  Empty string → right column
        is omitted.
    bg_color:
        CSS hex colour for the banner background, e.g. ``"#202020"``.
    text_color:
        CSS hex colour for the banner text, e.g. ``"#FFFFFF"``.

    Returns
    -------
    PIL.Image.Image
        New image with identical dimensions; top ``BANNER_HEIGHT`` rows carry
        the banner.
    """
    out = image.copy()
    draw = ImageDraw.Draw(out)
    width = out.width

    # Fill the banner rectangle.
    draw.rectangle([(0, 0), (width - 1, BANNER_HEIGHT - 1)], fill=bg_color)

    font = _load_font()

    center_text = f"Open-SSTV v{version}"

    # Measure text so we can position it precisely.
    bbox_c = draw.textbbox((0, 0), center_text, font=font)
    text_w = bbox_c[2] - bbox_c[0]
    text_h = bbox_c[3] - bbox_c[1]
    # Vertical offset: centre within BANNER_HEIGHT, correcting for descent.
    cy = (BANNER_HEIGHT - text_h) // 2 - bbox_c[1]

    # Draw centred text.
    cx = (width - text_w) // 2
    draw.text((cx, cy), center_text, fill=text_color, font=font)

    # Draw callsign flush-right when present.
    if callsign:
        bbox_r = draw.textbbox((0, 0), callsign, font=font)
        rw = bbox_r[2] - bbox_r[0]
        rx = width - rw - _CALLSIGN_MARGIN
        draw.text((rx, cy), callsign, fill=text_color, font=font)

    return out


__all__ = ["BANNER_HEIGHT", "apply_tx_banner"]
