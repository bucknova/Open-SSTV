# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for open_sstv.core.banner.apply_tx_banner.

Four acceptance criteria:

1. **Dimensions preserved** — apply_tx_banner() never changes image size.
2. **Banner drawn** — the top BANNER_HEIGHT rows are filled with the
   background colour; pixels below are left unchanged (modulo the callsign /
   version text, which may land anywhere inside the strip but not below it).
3. **Callsign omission** — empty callsign is handled gracefully (no crash,
   banner still drawn).
4. **Idempotent copy** — the original image is not modified in-place.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.banner import BANNER_HEIGHT, apply_tx_banner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _solid(width: int, height: int, color: tuple[int, int, int]) -> Image.Image:
    """Create a solid-colour RGB image."""
    return Image.new("RGB", (width, height), color)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert CSS hex string (e.g. '#202020') to (r, g, b)."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ---------------------------------------------------------------------------
# 1. Dimensions preserved
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width,height", [(320, 256), (640, 496), (160, 256)])
def test_banner_preserves_dimensions(width: int, height: int) -> None:
    img = _solid(width, height, (100, 150, 200))
    result = apply_tx_banner(img, "0.1.19", "W0AEZ")
    assert result.size == (width, height), (
        f"Expected {(width, height)}, got {result.size}"
    )


# ---------------------------------------------------------------------------
# 2. Banner background drawn in top strip
# ---------------------------------------------------------------------------

def test_banner_fills_top_rows_with_bg_color() -> None:
    """Every pixel in the top BANNER_HEIGHT rows must be close to bg_color."""
    bg_color = "#202020"
    bg_rgb = _hex_to_rgb(bg_color)

    # Use a bright, obviously different fill so any unfilled pixel stands out.
    img = _solid(320, 256, (255, 128, 0))
    result = apply_tx_banner(img, "0.1.19", callsign="", bg_color=bg_color)

    arr = np.array(result)  # (H, W, 3)
    banner_rows = arr[:BANNER_HEIGHT, :, :]  # (BANNER_HEIGHT, W, 3)

    # Text pixels will differ from bg_color. We allow up to 15% of pixels
    # to be non-background (text rendering on a ~24×320 strip with version
    # text centred and optional callsign leaves well under that).
    bg_array = np.array(bg_rgb, dtype=np.uint8)
    is_bg = np.all(banner_rows == bg_array, axis=2)  # (BANNER_HEIGHT, W)
    non_bg_fraction = 1.0 - is_bg.mean()
    assert non_bg_fraction < 0.15, (
        f"Too many non-background pixels in banner: {non_bg_fraction:.1%} "
        f"(expected < 15%)"
    )


def test_banner_does_not_alter_rows_below_strip() -> None:
    """Rows below BANNER_HEIGHT must be pixel-identical to the source."""
    fill = (200, 100, 50)
    img = _solid(320, 256, fill)
    result = apply_tx_banner(img, "0.1.19", "W0AEZ")

    arr = np.array(result)
    below = arr[BANNER_HEIGHT:, :, :]
    expected = np.full_like(below, fill)
    assert np.array_equal(below, expected), (
        "Pixels below the banner strip were modified — banner overflowed."
    )


# ---------------------------------------------------------------------------
# 3. Callsign omission
# ---------------------------------------------------------------------------

def test_banner_empty_callsign_does_not_crash() -> None:
    """apply_tx_banner with empty callsign must succeed without error."""
    img = _solid(320, 256, (0, 0, 0))
    result = apply_tx_banner(img, "0.1.19", callsign="")
    assert result.size == (320, 256)


def test_banner_with_callsign_still_fills_background() -> None:
    """Banner with callsign set still paints the background strip."""
    bg_color = "#1a1a2e"
    bg_rgb = _hex_to_rgb(bg_color)
    img = _solid(320, 256, (255, 255, 255))
    result = apply_tx_banner(img, "0.1.19", "W0AEZ", bg_color=bg_color)

    arr = np.array(result)
    banner_rows = arr[:BANNER_HEIGHT, :, :]
    bg_array = np.array(bg_rgb, dtype=np.uint8)
    is_bg = np.all(banner_rows == bg_array, axis=2)
    non_bg_fraction = 1.0 - is_bg.mean()
    assert non_bg_fraction < 0.15, (
        f"Background not correctly painted when callsign present: "
        f"{non_bg_fraction:.1%} non-bg pixels"
    )


# ---------------------------------------------------------------------------
# 4. Idempotent copy — original not modified
# ---------------------------------------------------------------------------

def test_banner_does_not_modify_original() -> None:
    """apply_tx_banner must return a copy; the source image is unchanged."""
    img = _solid(320, 256, (10, 20, 30))
    original_arr = np.array(img).copy()
    apply_tx_banner(img, "0.1.19", "W0AEZ")
    assert np.array_equal(np.array(img), original_arr), (
        "apply_tx_banner modified the source image in-place."
    )


# ---------------------------------------------------------------------------
# 5. Colour customisation
# ---------------------------------------------------------------------------

def test_banner_respects_custom_bg_and_text_colors() -> None:
    """Custom bg/text colors are applied (bg dominates the strip)."""
    bg_color = "#ff0000"   # pure red background
    text_color = "#00ff00"  # pure green text
    bg_rgb = _hex_to_rgb(bg_color)

    img = _solid(320, 256, (0, 0, 255))  # blue source — easy to distinguish
    result = apply_tx_banner(img, "0.1.19", "W0AEZ",
                             bg_color=bg_color, text_color=text_color)

    arr = np.array(result)
    banner_rows = arr[:BANNER_HEIGHT, :, :]
    bg_array = np.array(bg_rgb, dtype=np.uint8)
    is_bg = np.all(banner_rows == bg_array, axis=2)
    non_bg_fraction = 1.0 - is_bg.mean()
    assert non_bg_fraction < 0.15, (
        f"Custom background colour not applied: {non_bg_fraction:.1%} non-bg"
    )
    # No blue pixels (original fill) should survive inside the banner strip.
    blue_array = np.array([0, 0, 255], dtype=np.uint8)
    has_blue = np.any(np.all(banner_rows == blue_array, axis=2))
    assert not has_blue, "Source blue pixels leaked through the banner background."
