# SPDX-License-Identifier: GPL-3.0-or-later
"""TX header banner — stamps a thin identification strip onto the top of an image.

The banner is a full-width rectangle ``banner_height`` pixels tall. It is
drawn on a copy of the image so the original is never modified.

Layout
------
- **Centre:** "Open-SSTV v{version}"
- **Right:**  "{callsign}"  (omitted when *callsign* is empty)

The strip sits within the image bounds — dimensions are never changed — so
every SSTV mode's pixel geometry is preserved exactly.

Size presets
------------
Three named sizes are available via :data:`SIZE_TABLE` and
:func:`banner_size_params`:

.. code-block:: text

    "small"  →  24 px strip, 18 pt text   (recommended default)
    "medium" →  32 px strip, 24 pt text
    "large"  →  40 px strip, 30 pt text

Font sizes were bumped +4 pt across all presets in v0.1.22 so text fills
more of the strip without looking undersized.  Older configs that saved
``tx_banner_size: "medium"`` continue to work — only the point size changed.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default (small) banner height in pixels — kept for backward compatibility.
BANNER_HEIGHT: int = 24

#: Right-edge margin (pixels) for the callsign text.
_CALLSIGN_MARGIN: int = 4

#: Default (small) font size in points.  Raised from 14 pt → 18 pt in v0.1.22
#: so the default "small" preset fills more of its 24 px strip.
_DEFAULT_FONT_SIZE: int = 18

#: Mapping from size name → (banner_height, font_size).  Font sizes were bumped
#: +4 pt uniformly in v0.1.22 so every preset has a fuller fill ratio
#: (~75 %) than before.
SIZE_TABLE: dict[str, tuple[int, int]] = {
    "small":  (24, 18),
    "medium": (32, 24),
    "large":  (40, 30),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def banner_size_params(size: str) -> tuple[int, int]:
    """Return ``(banner_height, font_size)`` for a named size preset.

    Unknown names fall back to ``"small"`` (the recommended default since
    v0.1.22).  Case-insensitive.

    Parameters
    ----------
    size:
        One of ``"small"``, ``"medium"``, or ``"large"``.

    Returns
    -------
    tuple[int, int]
        ``(banner_height_px, font_size_pt)``
    """
    return SIZE_TABLE.get(size.lower(), SIZE_TABLE["small"])


def _load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Return the best available embedded font at *size* points.

    Pillow >= 10.1 accepts a ``size`` argument to ``load_default()``; older
    10.x releases do not.  We try the size-aware form first and fall back to
    the tiny legacy bitmap font if it is not available.
    """
    try:
        return ImageFont.load_default(size=size)  # type: ignore[call-arg]
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_tx_banner(
    image: Image.Image,
    version: str,
    callsign: str = "",
    bg_color: str = "#202020",
    text_color: str = "#FFFFFF",
    banner_height: int = BANNER_HEIGHT,
    font_size: int = _DEFAULT_FONT_SIZE,
) -> Image.Image:
    """Return a copy of *image* with a header banner stamped across the top.

    Parameters
    ----------
    image:
        Source PIL image (any mode; typically ``"RGB"``).
    version:
        Application version string, e.g. ``"0.1.20"``.
    callsign:
        Operator callsign, e.g. ``"W0AEZ"``.  Empty string → right column
        is omitted.
    bg_color:
        CSS hex colour for the banner background, e.g. ``"#202020"``.
    text_color:
        CSS hex colour for the banner text, e.g. ``"#FFFFFF"``.
    banner_height:
        Height of the banner strip in pixels.  Defaults to :data:`BANNER_HEIGHT`
        (24 px, the *small* preset).  Use :func:`banner_size_params` to look
        up the right value for a named size.
    font_size:
        Font size in points.  Defaults to :data:`_DEFAULT_FONT_SIZE` (18 pt
        since v0.1.22, was 14 pt previously).

    Returns
    -------
    PIL.Image.Image
        New image with identical dimensions; top *banner_height* rows carry
        the banner.
    """
    out = image.copy()
    draw = ImageDraw.Draw(out)
    width = out.width

    # Fill the banner rectangle.
    draw.rectangle([(0, 0), (width - 1, banner_height - 1)], fill=bg_color)

    font = _load_font(font_size)

    # Horizontal padding: at least 8 px, or ~2 % of width for wide images.
    padding = max(8, width // 50)

    def _vcenter(bbox: tuple[int, int, int, int]) -> int:
        """Return the y offset that vertically centres *bbox* in the strip."""
        text_h = bbox[3] - bbox[1]
        return (banner_height - text_h) // 2 - bbox[1]

    # --- Left side: callsign (flush-left) ---
    callsign_right_x = 0  # right edge of callsign column (0 when absent)
    if callsign:
        bbox_l = draw.textbbox((0, 0), callsign, font=font)
        lw = bbox_l[2] - bbox_l[0]
        draw.text((padding, _vcenter(bbox_l)), callsign, fill=text_color, font=font)
        callsign_right_x = padding + lw

    # --- Right side: app version (flush-right) ---
    version_text = f"Open-SSTV v{version}"
    bbox_r = draw.textbbox((0, 0), version_text, font=font)
    rw = bbox_r[2] - bbox_r[0]
    rx = width - rw - padding

    # If the version text would overlap the callsign, push it right until it
    # clears the callsign column.  Any remaining overflow is clipped naturally
    # by the image boundary — no callsign pixels are ever overwritten.
    if callsign and rx < callsign_right_x + padding:
        rx = callsign_right_x + padding

    draw.text((rx, _vcenter(bbox_r)), version_text, fill=text_color, font=font)

    return out


__all__ = ["BANNER_HEIGHT", "SIZE_TABLE", "apply_tx_banner", "banner_size_params"]
