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
    """Return the embedded default font at *size* points.

    Requires Pillow >= 10.1 (pinned in ``pyproject.toml``); the size
    kwarg on ``ImageFont.load_default`` landed in that release.  The
    try/except fallback that used to guard pre-10.1 installs was
    removed in v0.1.29 now that the minimum is bumped (OP-32).
    """
    return ImageFont.load_default(size=size)


def resolve_right_side_text(
    version: str,
    available_width_px: int,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> str:
    """Pick the widest right-side banner text that fits in *available_width_px*.

    Tiered fallback (v0.2.8):

    1. ``"Open-SSTV v{version}"`` — preferred.
    2. ``"Open-SSTV"`` — drop version when it doesn't fit.
    3. ``""`` — drop brand entirely when even that is too wide.

    The callsign is the §97.119-critical field and is rendered
    unconditionally by the caller; this helper only decides what (if
    anything) to put on the right-hand side beside it.

    Parameters
    ----------
    version:
        Application version, e.g. ``"0.2.8"``.
    available_width_px:
        Horizontal pixels available for right-side text.  The caller
        computes this as ``image_width − callsign_right_x − padding``.
    draw:
        Pillow drawing context used for width measurement.
    font:
        Font to measure against.  Must be the same font the banner
        actually renders with, or the measurement won't match.

    Returns
    -------
    str
        The chosen right-side text.  Empty string means nothing fits.
    """
    if available_width_px <= 0:
        return ""
    for candidate in (f"Open-SSTV v{version}", "Open-SSTV"):
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= available_width_px:
            return candidate
    return ""


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
    """Return a copy of *image* with a header banner and the content pushed down.

    The source image is resized to ``(width, height − banner_height)`` and
    pasted below a ``banner_height``-px strip, so the banner never
    overwrites user content.  The returned image has the same dimensions
    as the input — SSTV mode pixel geometry is preserved exactly.

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
        the banner, remaining rows carry the source content scaled to fit.
    """
    width, height = image.size
    content_height = height - banner_height

    # OP-23: refuse to stamp a banner that would leave no room for the
    # source image content.  Without this guard, a too-small image (or a
    # too-large banner) would silently produce an output where the entire
    # image is a banner-coloured rectangle and the user's image data is
    # discarded.  Today every shipping mode is at least 128 px tall and
    # the largest banner is 40 px (88 px content), so this never fires
    # in practice — but it would be a worst-of-all-worlds failure mode
    # for any future small mode or hand-edited preset.
    if content_height <= 0:
        raise ValueError(
            f"apply_tx_banner: image height {height}px is not large enough "
            f"for a {banner_height}px banner — at least "
            f"{banner_height + 1}px image height required."
        )

    # Build the output canvas: banner strip at top, resized content below.
    out = Image.new(image.mode or "RGB", (width, height), bg_color)
    shrunk = image.resize((width, content_height), Image.Resampling.LANCZOS)
    out.paste(shrunk, (0, banner_height))

    draw = ImageDraw.Draw(out)
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
    # v0.2.8: tiered fallback for narrow modes.  Martin M2 / M4 / Scottie S2
    # are 160 px wide — the full "Open-SSTV v{version}" text doesn't fit
    # beside even a short callsign.  Previous behaviour rendered whatever
    # hit the image boundary and silently clipped the rest.
    #
    # Callsign is §97.119-critical and never dropped.  Right-side text
    # degrades through three tiers until it fits — see
    # ``resolve_right_side_text`` for the tier definition.
    #
    # Fit budget: one padding of slack between the callsign column and
    # the right-side text, plus the usual right-edge padding.
    gap_after_callsign = padding if callsign else 0
    available_right = width - callsign_right_x - gap_after_callsign - padding
    right_text = resolve_right_side_text(version, available_right, draw, font)

    if right_text:
        right_bbox = draw.textbbox((0, 0), right_text, font=font)
        rw = right_bbox[2] - right_bbox[0]
        rx = width - rw - padding
        draw.text((rx, _vcenter(right_bbox)), right_text, fill=text_color, font=font)

    return out


__all__ = [
    "BANNER_HEIGHT",
    "SIZE_TABLE",
    "apply_tx_banner",
    "banner_size_params",
    "resolve_right_side_text",
]
