# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the text-overlay renderer.

v0.1.32 regression guards for the Exchange-template-on-narrow-modes
bug: the bottom overlay ``UR {rst} {date}`` at 20 pt used to render
wider than a 160-wide Martin M2 / Scottie S2 / M4 / S4 image, and the
centring math in ``position_to_xy`` produced a negative ``x`` that
spilled text off the right edge of the transmitted image.

The fix is twofold:

* ``draw_text_overlay`` auto-shrinks the font size (one point at a
  time) until the text fits within ``image_width − 2 × _MARGIN``, down
  to ``_MIN_FONT_SIZE``.
* A new ``clamp_xy_to_image`` clamps the final ``(x, y)`` so the 1 px
  drop-shadow ring always stays on-image.

These tests exercise both mechanisms.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from open_sstv.ui.draw_text import (
    _MARGIN,
    _MIN_FONT_SIZE,
    _SHADOW_PAD,
    clamp_xy_to_image,
    draw_text_overlay,
    position_to_xy,
)


# ---------------------------------------------------------------------------
# clamp_xy_to_image
# ---------------------------------------------------------------------------


class TestClampXYToImage:
    def test_within_bounds_unchanged(self) -> None:
        """A point comfortably inside the image with room for the text
        box is returned unchanged."""
        assert clamp_xy_to_image(50, 50, (320, 240), (80, 20)) == (50, 50)

    def test_negative_x_clamped_to_shadow_pad(self) -> None:
        """A preset producing a negative ``x`` (centring math with
        text wider than image) clamps to ``_SHADOW_PAD`` so the shadow
        ring still fits on-image."""
        # Image 160w, text 200w, center → x = (160-200)/2 = -20
        x, y = clamp_xy_to_image(-20, 10, (160, 128), (200, 20))
        assert x == _SHADOW_PAD

    def test_negative_y_clamped(self) -> None:
        """Bottom-centre position on a very small image produces
        negative y; clamp to shadow pad."""
        x, y = clamp_xy_to_image(10, -5, (320, 30), (50, 40))
        assert y == _SHADOW_PAD

    def test_over_right_edge_clamped(self) -> None:
        """A custom X placing the right edge off-image clamps the
        text flush with the right side (shadow-padded)."""
        # Image 320w, text 80w → max x = 320 - 80 - 1 = 239
        x, y = clamp_xy_to_image(300, 20, (320, 240), (80, 20))
        assert x == 320 - 80 - _SHADOW_PAD

    def test_over_bottom_edge_clamped(self) -> None:
        x, y = clamp_xy_to_image(10, 250, (320, 240), (80, 20))
        assert y == 240 - 20 - _SHADOW_PAD

    def test_text_exactly_image_width_pins_to_shadow_pad(self) -> None:
        """Text as wide as the image pins to (1, _) — caller should
        have used auto-shrink in draw_text_overlay to avoid this."""
        x, y = clamp_xy_to_image(0, 10, (160, 128), (160, 20))
        assert x == _SHADOW_PAD

    def test_text_wider_than_image(self) -> None:
        """Text wider than the image pins to (1, _) and accepts
        right-edge clipping.  This is the fallback when auto-shrink
        has already reduced the size to _MIN_FONT_SIZE and the text
        still won't fit."""
        x, y = clamp_xy_to_image(0, 10, (160, 128), (200, 20))
        assert x == _SHADOW_PAD


# ---------------------------------------------------------------------------
# draw_text_overlay — auto-shrink + clamp integration
# ---------------------------------------------------------------------------


def _render_and_read_text_pixels(
    image_size: tuple[int, int],
    text: str,
    **kwargs,
) -> tuple[Image.Image, list[tuple[int, int]]]:
    """Render text onto a fresh black canvas and return the canvas
    plus a list of (x, y) coordinates where white pixels were drawn.
    """
    img = Image.new("RGB", image_size, (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_text_overlay(draw, image_size, text, color=(255, 255, 255), **kwargs)

    # Find every white-ish pixel (shadow is pure black, body is pure
    # white per the call above).
    iw, ih = image_size
    px = img.load()
    assert px is not None
    white: list[tuple[int, int]] = []
    for y in range(ih):
        for x in range(iw):
            if px[x, y] == (255, 255, 255):
                white.append((x, y))
    return img, white


class TestAutoShrinkAndClamp:
    """The v0.1.32 auto-fit integration: text that wouldn't fit at the
    requested font size gets shrunk, and the final position is clamped
    so every pixel ends up inside the image."""

    def test_exchange_template_bottom_overlay_on_narrow_mode(self) -> None:
        """The original bug: Exchange template's ``UR 59 2026-04-16``
        at 20 pt on a 160-wide image used to overflow the right side
        when Bottom Center centring math produced a negative x.  With
        auto-shrink + clamp, every pixel of the rendered text falls
        inside the image bounds.
        """
        img, whites = _render_and_read_text_pixels(
            (160, 128),
            "UR 59 2026-04-16",
            position="Bottom Center",
            size=20,
        )
        assert whites, "text rendered no pixels — load_default may have failed"
        xs = [x for x, _ in whites]
        ys = [y for _, y in whites]
        assert min(xs) >= 0, f"text extends left of image (min x = {min(xs)})"
        assert max(xs) < 160, f"text extends right of image (max x = {max(xs)})"
        assert min(ys) >= 0
        assert max(ys) < 128

    def test_very_long_text_still_stays_in_bounds(self) -> None:
        """Even text that's too wide to render at ``_MIN_FONT_SIZE``
        must not produce any off-image pixels (we clip on the right
        but left edge stays at shadow_pad)."""
        img, whites = _render_and_read_text_pixels(
            (160, 128),
            "CQ CQ CQ DE W0AEZ/QRP/PORTABLE CALLING",
            position="Top Center",
            size=40,
        )
        if not whites:
            # If the default font can't render the shrunken size,
            # skip rather than fail — we'd need a TrueType font to
            # exercise this path and that's out of scope.
            pytest.skip("default font produced no pixels at shrunken size")
        xs = [x for x, _ in whites]
        ys = [y for _, y in whites]
        assert min(xs) >= 0
        assert max(xs) < 160
        assert min(ys) >= 0
        assert max(ys) < 128

    def test_custom_xy_still_clamped(self) -> None:
        """Explicit X/Y coordinates that would put text off-image are
        still clamped.  The image editor's Custom position mode goes
        through this path."""
        img, whites = _render_and_read_text_pixels(
            (320, 240),
            "W0AEZ",
            size=24,
            x=300,   # would put right edge off-screen
            y=230,   # would put bottom edge off-screen
        )
        assert whites
        xs = [x for x, _ in whites]
        ys = [y for _, y in whites]
        assert max(xs) < 320
        assert max(ys) < 240

    def test_short_text_fits_at_requested_size(self) -> None:
        """Short text that fits at the requested size is not
        auto-shrunk and renders at roughly the expected height range."""
        img, whites = _render_and_read_text_pixels(
            (320, 240),
            "K",
            position="Center",
            size=40,
        )
        # With a 40 pt font the rendered height for one character
        # should be at least 15 px in any reasonable font.
        ys = [y for _, y in whites]
        height = max(ys) - min(ys)
        assert height > 10, (
            f"expected a tall rendering for K at 40 pt, got height {height}"
        )

    def test_empty_text_is_noop(self) -> None:
        """Empty text leaves the image untouched."""
        img = Image.new("RGB", (320, 240), (10, 20, 30))
        draw = ImageDraw.Draw(img)
        draw_text_overlay(draw, (320, 240), "", size=24)
        # Every pixel should still be the fill colour.
        assert img.getpixel((10, 10)) == (10, 20, 30)
        assert img.getpixel((310, 230)) == (10, 20, 30)

    def test_martin_m4_160x128_exchange_fits(self) -> None:
        """Martin M4 is the smallest we ship (160 × 128).  Every
        overlay in the built-in Exchange template must render
        entirely on-image at that size."""
        for text, size in [
            ("K9XYZ DE W0AEZ", 24),  # Top Center, Exchange overlay 1
            ("UR 59 2026-04-16", 20),  # Bottom Center, Exchange overlay 2
        ]:
            img, whites = _render_and_read_text_pixels(
                (160, 128),
                text,
                position="Bottom Center" if size == 20 else "Top Center",
                size=size,
            )
            if whites:
                xs = [x for x, _ in whites]
                ys = [y for _, y in whites]
                assert min(xs) >= 0 and max(xs) < 160, (
                    f"{text!r} at {size}pt overflowed width: "
                    f"x range [{min(xs)}, {max(xs)}]"
                )
                assert min(ys) >= 0 and max(ys) < 128, (
                    f"{text!r} at {size}pt overflowed height: "
                    f"y range [{min(ys)}, {max(ys)}]"
                )


# ---------------------------------------------------------------------------
# position_to_xy — unchanged contract, kept for backward compat
# ---------------------------------------------------------------------------


class TestPositionToXY:
    """``position_to_xy`` still returns the raw preset position
    (possibly out of bounds); clamping lives in ``clamp_xy_to_image``
    and ``draw_text_overlay``.  This keeps the image editor's Position
    preset → X/Y auto-fill behaviour intact."""

    def test_top_left_uses_margin(self) -> None:
        assert position_to_xy("Top Left", (320, 240), (80, 20)) == (
            _MARGIN, _MARGIN,
        )

    def test_bottom_right_uses_margin(self) -> None:
        x, y = position_to_xy("Bottom Right", (320, 240), (80, 20))
        assert x == 320 - 80 - _MARGIN
        assert y == 240 - 20 - _MARGIN

    def test_center_allows_negative_for_oversized_text(self) -> None:
        """This is the raw behaviour — clamping is the caller's job.
        Documents why draw_text_overlay now clamps explicitly."""
        x, y = position_to_xy("Top Center", (160, 128), (200, 20))
        assert x < 0
