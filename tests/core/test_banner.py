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

from open_sstv.core.banner import (
    BANNER_HEIGHT,
    SIZE_TABLE,
    apply_tx_banner,
    banner_size_params,
)


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


# ---------------------------------------------------------------------------
# 6. SIZE_TABLE and banner_size_params
# ---------------------------------------------------------------------------

def test_size_table_contains_all_presets() -> None:
    """SIZE_TABLE must have entries for small, medium, and large."""
    for key in ("small", "medium", "large"):
        assert key in SIZE_TABLE, f"SIZE_TABLE missing preset '{key}'"
        height, font_size = SIZE_TABLE[key]
        assert height > 0, f"SIZE_TABLE[{key!r}] height must be positive"
        assert font_size > 0, f"SIZE_TABLE[{key!r}] font_size must be positive"


def test_size_table_ordering() -> None:
    """Each larger preset must have a strictly larger height and font size."""
    small_h, small_f = SIZE_TABLE["small"]
    medium_h, medium_f = SIZE_TABLE["medium"]
    large_h, large_f = SIZE_TABLE["large"]
    assert small_h < medium_h < large_h, "Strip heights must increase small < medium < large"
    assert small_f < medium_f < large_f, "Font sizes must increase small < medium < large"


def test_banner_size_params_known_keys() -> None:
    """banner_size_params returns the table entry for known keys."""
    for key in ("small", "medium", "large"):
        assert banner_size_params(key) == SIZE_TABLE[key]


def test_banner_size_params_unknown_falls_back_to_medium() -> None:
    """Unknown size name falls back to medium without raising."""
    assert banner_size_params("xl") == SIZE_TABLE["medium"]
    assert banner_size_params("") == SIZE_TABLE["medium"]


@pytest.mark.parametrize("size_name", ["small", "medium", "large"])
def test_banner_parametrized_height_fills_correct_rows(size_name: str) -> None:
    """apply_tx_banner with explicit params fills exactly the right rows."""
    bh, fs = SIZE_TABLE[size_name]
    fill = (10, 20, 30)
    img = _solid(320, 256, fill)
    result = apply_tx_banner(img, "0.1.20", "W0AEZ",
                             banner_height=bh, font_size=fs)
    arr = np.array(result)

    # Image dimensions unchanged.
    assert result.size == (320, 256)

    # Rows below the strip must be pixel-identical to the source.
    below = arr[bh:, :, :]
    expected = np.full_like(below, fill)
    assert np.array_equal(below, expected), (
        f"Rows below banner (height={bh}) were modified for size '{size_name}'"
    )

    # Top rows must be dominated by the bg colour (#202020).
    bg_rgb = _hex_to_rgb("#202020")
    bg_array = np.array(bg_rgb, dtype=np.uint8)
    banner_rows = arr[:bh, :, :]
    is_bg = np.all(banner_rows == bg_array, axis=2)
    non_bg_fraction = 1.0 - is_bg.mean()
    # Larger presets use proportionally larger text, so allow up to 20%.
    assert non_bg_fraction < 0.20, (
        f"Too many non-bg pixels in '{size_name}' banner: {non_bg_fraction:.1%}"
    )
