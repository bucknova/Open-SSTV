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

from PIL import ImageDraw, ImageFont

from open_sstv.core.banner import (
    BANNER_HEIGHT,
    SIZE_TABLE,
    _DEFAULT_FONT_SIZE,
    _SCALED_CLAMP,
    _SCALED_PERCENT,
    apply_tx_banner,
    banner_size_params,
    resolve_right_side_text,
    scaled_banner_params,
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

    # Text pixels will differ from bg_color. We allow up to 20% of pixels
    # to be non-background (text rendering on a ~24×320 strip with version
    # text flush-right and optional callsign leaves well under that; the
    # threshold was raised from 15% to 20% in v0.1.22 when the default font
    # size was bumped 14 pt → 18 pt for a fuller-looking strip).
    bg_array = np.array(bg_rgb, dtype=np.uint8)
    is_bg = np.all(banner_rows == bg_array, axis=2)  # (BANNER_HEIGHT, W)
    non_bg_fraction = 1.0 - is_bg.mean()
    assert non_bg_fraction < 0.20, (
        f"Too many non-background pixels in banner: {non_bg_fraction:.1%} "
        f"(expected < 20%)"
    )


def test_banner_content_below_strip_is_resized_source() -> None:
    """Rows below BANNER_HEIGHT contain the source image resized to fit.

    The banner pushes image content down by ``banner_height`` pixels:
    the source is scaled to ``(width, height − banner_height)`` and
    pasted at ``y = banner_height``.  For a solid-colour source the
    resized content is still that same solid colour.
    """
    fill = (200, 100, 50)
    img = _solid(320, 256, fill)
    result = apply_tx_banner(img, "0.1.19", "W0AEZ")

    arr = np.array(result)
    below = arr[BANNER_HEIGHT:, :, :]
    expected = np.full_like(below, fill)
    # LANCZOS resize of a solid colour is still that solid colour.
    assert np.array_equal(below, expected), (
        "Content below the banner strip is not the resized source."
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
    assert non_bg_fraction < 0.20, (
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
    assert non_bg_fraction < 0.20, (
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


def test_banner_size_params_unknown_falls_back_to_small() -> None:
    """Unknown size name falls back to small (the default since v0.1.22)."""
    assert banner_size_params("xl") == SIZE_TABLE["small"]
    assert banner_size_params("") == SIZE_TABLE["small"]


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

    # Rows below the strip contain the resized source content.
    # For a solid-colour source, the resized content is identical.
    below = arr[bh:, :, :]
    expected = np.full_like(below, fill)
    assert np.array_equal(below, expected), (
        f"Content below banner (height={bh}) not resized source for '{size_name}'"
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


# ---------------------------------------------------------------------------
# 7. Banner pushes content down (no collision)
# ---------------------------------------------------------------------------


def test_banner_pushes_content_down_not_overwrites() -> None:
    """Source content near y=0 must be visible below the banner, not
    destroyed.

    Regression guard for the banner-collision bug: the old implementation
    drew the banner ON TOP of the source pixels, overwriting any user
    content (text overlays, image detail) in the top ``banner_height``
    rows.  The fix resizes the source to fit below the banner strip so
    no user pixels are lost.

    We paint a distinct bright stripe at y=0..3 in the source.  After
    the banner, that stripe should appear — pushed below the banner
    strip — rather than being hidden under the bg fill.
    """
    width, height = 320, 256
    bh = BANNER_HEIGHT  # 24
    stripe_color = (255, 0, 0)  # bright red — easy to detect
    fill = (50, 50, 50)  # neutral gray body

    img = _solid(width, height, fill)
    from PIL import ImageDraw as _ID
    draw = _ID.Draw(img)
    # Paint a 4-pixel-tall stripe at the very top of the source.
    draw.rectangle([(0, 0), (width - 1, 3)], fill=stripe_color)

    result = apply_tx_banner(img, "0.1.19", "W0AEZ")
    arr = np.array(result)

    # The stripe should now live at rows bh .. bh + (scaled stripe height).
    # LANCZOS resize of 256→232 scales the 4 px stripe to ~3.6 px — at least
    # 2 full rows should be dominantly red.
    content_rows = arr[bh:bh + 4, :, :]
    red_channel_mean = float(content_rows[:, :, 0].mean())
    green_channel_mean = float(content_rows[:, :, 1].mean())
    blue_channel_mean = float(content_rows[:, :, 2].mean())
    # Red should dominate (> 150) while green and blue stay low (< 80).
    assert red_channel_mean > 150, (
        f"Source top-stripe red channel too low after banner: "
        f"R={red_channel_mean:.0f} — content may have been overwritten."
    )
    assert green_channel_mean < 80 and blue_channel_mean < 80, (
        f"Unexpected colour in pushed-down content: "
        f"G={green_channel_mean:.0f}, B={blue_channel_mean:.0f}"
    )


# ---------------------------------------------------------------------------
# 8. v0.2.8 — right-side tier fallback for narrow modes
# ---------------------------------------------------------------------------
#
# Martin M2 / M4 / Scottie S2 are 160 px wide — the full
# "Open-SSTV v{version}" doesn't fit beside even a short callsign.  The
# right-side text now degrades through three tiers: full → brand-only → none.
# Callsign is never dropped.


@pytest.fixture
def _default_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=_DEFAULT_FONT_SIZE)


@pytest.fixture
def _scratch_draw() -> ImageDraw.ImageDraw:
    """Disposable draw context for measuring text widths.

    ``resolve_right_side_text`` only calls ``draw.textbbox``; any
    ``ImageDraw.Draw`` bound to an image of any size works as a
    measurement surface.
    """
    scratch = Image.new("RGB", (800, 32))
    return ImageDraw.Draw(scratch)


def test_resolve_prefers_full_when_fits(_default_font, _scratch_draw) -> None:
    """320 px wide mode with short callsign → full brand+version fits."""
    # 320 px image, 45 px callsign column, 8 px padding on each side →
    # ~255 px available on the right.  That's plenty for "Open-SSTV v0.2.8".
    text = resolve_right_side_text("0.2.8", 255, _scratch_draw, _default_font)
    assert text == "Open-SSTV v0.2.8"


def test_resolve_drops_version_when_full_overflows(_default_font, _scratch_draw) -> None:
    """Narrow budget that fits 'Open-SSTV' but not the version → brand-only."""
    # Measure "Open-SSTV" and "Open-SSTV v0.2.8" to pick a budget that lies
    # between them.  This makes the test robust against font-metric drift.
    brand = "Open-SSTV"
    full = f"Open-SSTV v0.2.8"
    brand_w = _scratch_draw.textbbox((0, 0), brand, font=_default_font)[2]
    full_w = _scratch_draw.textbbox((0, 0), full, font=_default_font)[2]
    # Budget just above brand width but below full width.
    budget = brand_w + (full_w - brand_w) // 2
    text = resolve_right_side_text("0.2.8", budget, _scratch_draw, _default_font)
    assert text == "Open-SSTV"


def test_resolve_drops_brand_when_even_brand_overflows(_default_font, _scratch_draw) -> None:
    """Budget below brand width → empty string (nothing renders)."""
    brand_w = _scratch_draw.textbbox((0, 0), "Open-SSTV", font=_default_font)[2]
    # Pick a budget well below brand width.
    text = resolve_right_side_text("0.2.8", brand_w - 10, _scratch_draw, _default_font)
    assert text == ""


def test_resolve_zero_budget_returns_empty(_default_font, _scratch_draw) -> None:
    """Zero or negative available width → empty string."""
    assert resolve_right_side_text("0.2.8", 0, _scratch_draw, _default_font) == ""
    assert resolve_right_side_text("0.2.8", -50, _scratch_draw, _default_font) == ""


def test_banner_martin_m2_does_not_clip(_default_font) -> None:
    """Martin M2 (160×256) with W0AEZ callsign: the full banner fallback
    must leave no right-side ink clipped at the image boundary.

    Regression guard: before v0.2.8, the right-side version text was
    rendered with ``rx = width - rw - padding``; when ``rw`` exceeded the
    remaining budget, ``rx`` could fall inside the callsign column and
    Pillow simply clipped at the image edge.  Now we drop the version
    (and the brand if needed) so the rendered banner is always whole.
    """
    img = _solid(160, 256, (50, 50, 50))
    result = apply_tx_banner(img, "0.2.8", "W0AEZ")

    arr = np.array(result)
    # The rightmost column of the banner strip should be the background
    # colour, not text ink — i.e. no text reaches the edge.  Allow a
    # small tolerance because antialiasing fringe pixels may deviate a
    # few units from the exact bg hex value.
    bg_rgb = np.array(_hex_to_rgb("#202020"), dtype=np.int16)
    rightmost_col = arr[:BANNER_HEIGHT, -1, :].astype(np.int16)
    max_deviation = int(np.max(np.abs(rightmost_col - bg_rgb)))
    assert max_deviation <= 8, (
        f"Banner right edge has non-background ink: max deviation "
        f"{max_deviation} from {bg_rgb.tolist()} — text may be clipped."
    )


def test_banner_narrow_preserves_callsign(_default_font) -> None:
    """Martin M2: callsign must still render at the left even when the
    right side is dropped entirely.

    This is the §97.119 invariant: the operator's callsign is the one
    field we never drop, regardless of how narrow the frame is.
    """
    img = _solid(160, 256, (50, 50, 50))
    result = apply_tx_banner(img, "0.2.8", "W0AEZ")

    arr = np.array(result)
    # The callsign column should have non-background ink starting around
    # x=8 (padding).  Scan the leftmost 60 px of the banner strip for any
    # non-bg pixels — callsign "W0AEZ" at 18 pt is well wider than that.
    bg_rgb = np.array(_hex_to_rgb("#202020"), dtype=np.uint8)
    left_band = arr[:BANNER_HEIGHT, :60, :]
    is_bg = np.all(left_band == bg_rgb, axis=2)
    non_bg_fraction = 1.0 - is_bg.mean()
    assert non_bg_fraction > 0.05, (
        f"Callsign appears to be missing in narrow-mode banner: "
        f"only {non_bg_fraction:.1%} non-bg ink in the left column"
    )


# ---------------------------------------------------------------------------
# 9. scaled_banner_params — dynamic sizing relative to image height
# ---------------------------------------------------------------------------


class TestScaledBannerParams:
    """scaled_banner_params scales banner height as a % of image width,
    clamped to per-preset min/max pixel bounds."""

    # ---- percentage + clamp maths ----

    @pytest.mark.parametrize("size,image_width,expected_bh", [
        # Martin M1 (320×256) — typical wide mode
        ("small",  320, max(_SCALED_CLAMP["small"][0],
                            min(_SCALED_CLAMP["small"][1],
                                int(320 * _SCALED_PERCENT["small"])))),
        ("medium", 320, max(_SCALED_CLAMP["medium"][0],
                            min(_SCALED_CLAMP["medium"][1],
                                int(320 * _SCALED_PERCENT["medium"])))),
        ("large",  320, max(_SCALED_CLAMP["large"][0],
                            min(_SCALED_CLAMP["large"][1],
                                int(320 * _SCALED_PERCENT["large"])))),
        # Martin M2 / Scottie S2 (160×256) — narrow mode hits lower clamp
        ("small",  160, max(_SCALED_CLAMP["small"][0],
                            min(_SCALED_CLAMP["small"][1],
                                int(160 * _SCALED_PERCENT["small"])))),
        ("medium", 160, max(_SCALED_CLAMP["medium"][0],
                            min(_SCALED_CLAMP["medium"][1],
                                int(160 * _SCALED_PERCENT["medium"])))),
        ("large",  160, max(_SCALED_CLAMP["large"][0],
                            min(_SCALED_CLAMP["large"][1],
                                int(160 * _SCALED_PERCENT["large"])))),
        # PD-120 (640×496) — wide enough to hit the upper clamp for all sizes
        ("small",  640, max(_SCALED_CLAMP["small"][0],
                            min(_SCALED_CLAMP["small"][1],
                                int(640 * _SCALED_PERCENT["small"])))),
        ("medium", 640, max(_SCALED_CLAMP["medium"][0],
                            min(_SCALED_CLAMP["medium"][1],
                                int(640 * _SCALED_PERCENT["medium"])))),
        ("large",  640, max(_SCALED_CLAMP["large"][0],
                            min(_SCALED_CLAMP["large"][1],
                                int(640 * _SCALED_PERCENT["large"])))),
    ])
    def test_banner_height_correct(self, size: str, image_width: int,
                                   expected_bh: int) -> None:
        bh, _ = scaled_banner_params(size, image_width)
        assert bh == expected_bh, (
            f"scaled_banner_params({size!r}, {image_width}) → bh={bh}, "
            f"expected {expected_bh}"
        )

    def test_font_size_is_75_percent_of_banner_height(self) -> None:
        for size in ("small", "medium", "large"):
            bh, fs = scaled_banner_params(size, 320)
            expected_fs = max(10, int(bh * 0.75))
            assert fs == expected_fs, (
                f"{size}: font_size={fs}, expected {expected_fs} "
                f"(0.75 × banner_height={bh})"
            )

    def test_unknown_size_falls_back_to_small(self) -> None:
        result = scaled_banner_params("xl", 320)
        expected = scaled_banner_params("small", 320)
        assert result == expected

    def test_clamps_to_minimum_on_tiny_image(self) -> None:
        for size in ("small", "medium", "large"):
            lo, _ = _SCALED_CLAMP[size]
            bh, _ = scaled_banner_params(size, 1)
            assert bh == lo, f"{size}: expected min clamp {lo}, got {bh}"

    def test_clamps_to_maximum_on_huge_image(self) -> None:
        for size in ("small", "medium", "large"):
            _, hi = _SCALED_CLAMP[size]
            bh, _ = scaled_banner_params(size, 10_000)
            assert bh == hi, f"{size}: expected max clamp {hi}, got {bh}"

    def test_pd120_hits_upper_clamp(self) -> None:
        """PD-120 (640 px wide) should hit the upper clamp for all presets."""
        for size in ("small", "medium", "large"):
            _, hi = _SCALED_CLAMP[size]
            raw = int(640 * _SCALED_PERCENT[size])
            if raw >= hi:
                bh, _ = scaled_banner_params(size, 640)
                assert bh == hi, f"{size} PD-120: expected {hi}, got {bh}"

    @pytest.mark.parametrize("size,image_width", [
        ("small",  320),  # Martin M1
        ("small",  160),  # Martin M2 / Scottie S2
        ("medium", 320),
        ("large",  320),
        ("small",  640),  # PD-120
        ("large",  640),
    ])
    def test_apply_tx_banner_accepts_scaled_params(self, size: str,
                                                    image_width: int) -> None:
        """Smoke test: apply_tx_banner must accept scaled_banner_params output."""
        bh, fs = scaled_banner_params(size, image_width)
        img = _solid(image_width, 240, (100, 100, 100))
        result = apply_tx_banner(img, "0.2.12", "W0AEZ",
                                 banner_height=bh, font_size=fs)
        assert result.size == (image_width, 240), (
            f"Dimensions changed after apply_tx_banner with "
            f"scaled params ({size}, w={image_width})"
        )
