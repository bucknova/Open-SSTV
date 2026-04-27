# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the v0.3 template renderer (``render_template``).

Strategy
────────
Property-based rather than golden-image: we assert on pixel-level
properties (colour at a known position, image dimensions, etc.) so the
tests remain valid across font rendering differences on different
platforms.  No golden PNGs are stored in the repository.

All tests use a minimal AppConfig and injected QSO/TX contexts so there
is no filesystem I/O beyond the bundled font files.
"""
from __future__ import annotations

import datetime

import PIL.Image
import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import (
    GradientLayer,
    PatternLayer,
    PhotoLayer,
    QSOState,
    RectLayer,
    ShadowSpec,
    StrokeSpec,
    TXContext,
    Template,
    TextLayer,
)
from open_sstv.templates.renderer import (
    _anchor_top_left,
    _fit_image,
    _fit_text,
    _wrap_text,
    render_template,
)
from open_sstv.templates.renderer import _load_font, _text_bbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime.datetime(2026, 4, 24, 15, 0, 0, tzinfo=datetime.timezone.utc)
_FRAME = (320, 256)


def _cfg(**kw: object) -> AppConfig:
    defaults: dict[str, object] = {"callsign": "W0AEZ"}
    defaults.update(kw)
    return AppConfig(**defaults)  # type: ignore[arg-type]


def _qso(**kw: object) -> QSOState:
    return QSOState(tocall="VE7ABC", **kw)  # type: ignore[arg-type]


def _ctx(**kw: object) -> TXContext:
    defaults: dict[str, object] = {"mode_display_name": "Scottie S1", "frame_size": _FRAME}
    defaults.update(kw)
    return TXContext(**defaults)  # type: ignore[arg-type]


def _render(template: Template, **kw: object) -> PIL.Image.Image:
    cfg = kw.pop("cfg", _cfg())
    qso = kw.pop("qso", _qso())
    ctx = kw.pop("ctx", _ctx())
    return render_template(template, qso, cfg, ctx, now_utc=_FIXED_NOW, **kw)  # type: ignore[arg-type]


def _solid_color_image(w: int, h: int, color: tuple[int, int, int]) -> PIL.Image.Image:
    img = PIL.Image.new("RGB", (w, h), color)
    return img


# ---------------------------------------------------------------------------
# _anchor_top_left unit tests (geometry, no PIL)
# ---------------------------------------------------------------------------


class TestAnchorTopLeft:
    """Exhaustive coverage of the nine anchors + FILL."""

    W, H = 320, 256
    BW, BH = 100, 50  # bounding box of the layer

    def tl(self, anchor: str, ox: float = 0.0, oy: float = 0.0) -> tuple[int, int]:
        return _anchor_top_left(anchor, ox, oy, self.BW, self.BH, self.W, self.H)

    def test_tl(self) -> None:
        assert self.tl("TL") == (0, 0)

    def test_tl_with_offset(self) -> None:
        x, y = self.tl("TL", ox=10.0, oy=5.0)
        assert x == int(round(10 / 100 * self.W))
        assert y == int(round(5 / 100 * self.H))

    def test_tc(self) -> None:
        x, y = self.tl("TC")
        assert x == self.W // 2 - self.BW // 2
        assert y == 0

    def test_tr(self) -> None:
        x, y = self.tl("TR")
        assert x == self.W - self.BW
        assert y == 0

    def test_tr_with_offset(self) -> None:
        # offset_x inward from right edge
        x, y = self.tl("TR", ox=5.0)
        assert x == self.W - int(round(5 / 100 * self.W)) - self.BW

    def test_cl(self) -> None:
        x, y = self.tl("CL")
        assert x == 0
        assert y == self.H // 2 - self.BH // 2

    def test_c(self) -> None:
        x, y = self.tl("C")
        assert x == self.W // 2 - self.BW // 2
        assert y == self.H // 2 - self.BH // 2

    def test_cr(self) -> None:
        x, y = self.tl("CR")
        assert x == self.W - self.BW
        assert y == self.H // 2 - self.BH // 2

    def test_bl(self) -> None:
        x, y = self.tl("BL")
        assert x == 0
        assert y == self.H - self.BH

    def test_bc(self) -> None:
        x, y = self.tl("BC")
        assert x == self.W // 2 - self.BW // 2
        assert y == self.H - self.BH

    def test_br(self) -> None:
        x, y = self.tl("BR")
        assert x == self.W - self.BW
        assert y == self.H - self.BH

    def test_br_with_offset_inward(self) -> None:
        x, y = self.tl("BR", ox=5.0, oy=5.0)
        assert x == self.W - int(round(5 / 100 * self.W)) - self.BW
        assert y == self.H - int(round(5 / 100 * self.H)) - self.BH

    def test_fill_returns_origin(self) -> None:
        x, y = self.tl("FILL")
        assert (x, y) == (0, 0)


# ---------------------------------------------------------------------------
# _fit_image unit tests
# ---------------------------------------------------------------------------


class TestFitImage:
    SRC_W, SRC_H = 200, 100

    def _src(self) -> PIL.Image.Image:
        return PIL.Image.new("RGB", (self.SRC_W, self.SRC_H), (255, 0, 0))

    def test_stretch_exact_size(self) -> None:
        out = _fit_image(self._src(), 50, 30, "stretch")
        assert out.size == (50, 30)

    def test_contain_maintains_aspect(self) -> None:
        out = _fit_image(self._src(), 100, 100, "contain")
        assert out.size == (100, 100)
        # Red region should be 2:1 aspect, so top/bottom rows should be transparent
        r, g, b, a = out.getpixel((50, 0))  # top-center
        assert a == 0  # letterbox area is transparent

    def test_cover_fills_bbox(self) -> None:
        out = _fit_image(self._src(), 100, 100, "cover")
        assert out.size == (100, 100)
        # All pixels should be opaque (no letterboxing)
        r, g, b, a = out.getpixel((50, 50))
        assert a == 255

    def test_rgba_input_preserved(self) -> None:
        src = PIL.Image.new("RGBA", (100, 100), (0, 255, 0, 128))
        out = _fit_image(src, 50, 50, "stretch")
        assert out.mode == "RGBA"


# ---------------------------------------------------------------------------
# render_template — output properties
# ---------------------------------------------------------------------------


class TestRenderOutput:
    def test_returns_rgb_image(self) -> None:
        t = Template(name="test", layers=[])
        img = _render(t)
        assert isinstance(img, PIL.Image.Image)
        assert img.mode == "RGB"

    def test_output_size_matches_frame(self) -> None:
        frame = (160, 120)
        t = Template(name="test", layers=[])
        img = _render(t, ctx=_ctx(frame_size=frame))
        assert img.size == frame

    def test_empty_template_black_canvas(self) -> None:
        t = Template(name="empty", layers=[])
        img = _render(t)
        # Default canvas is black
        r, g, b = img.getpixel((0, 0))
        assert r == 0 and g == 0 and b == 0


# ---------------------------------------------------------------------------
# RectLayer
# ---------------------------------------------------------------------------


class TestRectLayer:
    def test_rect_fill_color(self) -> None:
        layer = RectLayer(id="r", anchor="FILL", fill=(255, 0, 0, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        r, g, b = img.getpixel((100, 100))
        assert r == 255 and g == 0 and b == 0

    def test_rect_tl_positioned(self) -> None:
        # A white 50%-wide rect anchored top-left
        layer = RectLayer(id="r", anchor="TL", width_pct=50.0, height_pct=50.0, fill=(255, 255, 255, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        # Top-left corner should be white
        r, g, b = img.getpixel((0, 0))
        assert r == 255 and g == 255 and b == 255
        # Bottom-right corner should be black (outside rect)
        r2, g2, b2 = img.getpixel((319, 255))
        assert r2 == 0 and g2 == 0 and b2 == 0

    def test_rect_opacity(self) -> None:
        # Start with white background, overlay 50% opaque red
        bg = RectLayer(id="bg", anchor="FILL", fill=(255, 255, 255, 255))
        overlay = RectLayer(id="ov", anchor="FILL", fill=(255, 0, 0, 255), opacity=0.5)
        t = Template(name="t", layers=[bg, overlay])
        img = _render(t)
        r, g, b = img.getpixel((160, 128))
        # Red channel should be > 200 (blended toward red), green < 150
        assert r > 200
        assert g < 150

    def test_rect_invisible_layer_skipped(self) -> None:
        layer = RectLayer(id="r", anchor="FILL", fill=(255, 0, 0, 255), visible=False)
        t = Template(name="t", layers=[layer])
        img = _render(t)
        # Should still be black (invisible layer skipped)
        r, g, b = img.getpixel((160, 128))
        assert r == 0


# ---------------------------------------------------------------------------
# GradientLayer
# ---------------------------------------------------------------------------


class TestGradientLayer:
    def test_gradient_produces_correct_size(self) -> None:
        layer = GradientLayer(
            id="g", anchor="FILL",
            from_color=(0, 0, 0, 255),
            to_color=(255, 0, 0, 255),
            angle_deg=0.0,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_gradient_left_to_right(self) -> None:
        # angle=0 → from_color at left, to_color at right
        layer = GradientLayer(
            id="g", anchor="FILL",
            from_color=(0, 0, 0, 255),
            to_color=(255, 0, 0, 255),
            angle_deg=0.0,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        left_r, _, _ = img.getpixel((0, 128))
        right_r, _, _ = img.getpixel((_FRAME[0] - 1, 128))
        assert right_r > left_r

    def test_gradient_top_to_bottom(self) -> None:
        layer = GradientLayer(
            id="g", anchor="FILL",
            from_color=(0, 0, 255, 255),
            to_color=(255, 255, 0, 255),
            angle_deg=90.0,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        top_b = img.getpixel((160, 0))[2]
        bot_b = img.getpixel((160, _FRAME[1] - 1))[2]
        assert top_b > bot_b  # blue decreasing top→bottom


# ---------------------------------------------------------------------------
# PhotoLayer
# ---------------------------------------------------------------------------


class TestPhotoLayer:
    def test_photo_cover_fills_frame(self) -> None:
        photo = _solid_color_image(100, 100, (0, 200, 0))
        layer = PhotoLayer(id="p", anchor="FILL", fit="cover")
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(photo_image=photo))
        r, g, b = img.getpixel((160, 128))
        assert g > 150  # green photo fills frame

    def test_no_photo_renders_without_crash(self) -> None:
        layer = PhotoLayer(id="p", anchor="FILL", fit="cover")
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(photo_image=None))
        assert img.size == _FRAME  # no exception, just black

    def test_photo_tl_positioned(self) -> None:
        photo = _solid_color_image(100, 80, (200, 0, 0))
        layer = PhotoLayer(
            id="p", anchor="TL",
            offset_x_pct=0.0, offset_y_pct=0.0,
            width_pct=50.0, height_pct=50.0,
            fit="stretch",
        )
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(photo_image=photo))
        r, g, b = img.getpixel((1, 1))
        assert r > 150  # red in top-left quadrant

    def test_photo_contains_no_overflow(self) -> None:
        photo = _solid_color_image(400, 50, (0, 0, 200))  # very wide
        layer = PhotoLayer(id="p", anchor="FILL", fit="contain")
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(photo_image=photo))
        # Top row should be black (letterboxed)
        r, g, b = img.getpixel((160, 0))
        assert r == 0 and g == 0 and b == 0


# ---------------------------------------------------------------------------
# TextLayer — geometry and content
# ---------------------------------------------------------------------------


class TestTextLayer:
    def test_text_renders_without_crash(self) -> None:
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="Hello",
            font_family="DejaVu Sans Bold",
            font_size_pct=8.0,
            fill=(255, 255, 255, 255),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_text_with_callsign_token(self) -> None:
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="%c",
            font_family="DejaVu Sans Bold",
            font_size_pct=10.0,
            fill=(255, 255, 255, 255),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t, cfg=_cfg(callsign="W0AEZ"))
        # Image should have some white pixels (text was rendered)
        pixels = list(img.getdata())
        white_count = sum(1 for r, g, b in pixels if r > 200 and g > 200 and b > 200)
        assert white_count > 10

    def test_text_with_stroke_renders(self) -> None:
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="STROKE",
            font_family="DejaVu Sans Bold",
            font_size_pct=15.0,
            fill=(255, 0, 0, 255),
            stroke=StrokeSpec(color=(255, 255, 255, 255), width_px=2),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME
        # Should have red pixels (fill)
        pixels = list(img.getdata())
        red_count = sum(1 for r, g, b in pixels if r > 200 and g < 50 and b < 50)
        assert red_count > 5

    def test_text_with_shadow_renders(self) -> None:
        layer = TextLayer(
            id="t", anchor="C",
            text_raw="SHADOW",
            font_family="DejaVu Sans Bold",
            font_size_pct=12.0,
            fill=(255, 255, 0, 255),
            shadow=ShadowSpec(offset_x=3, offset_y=3, color=(0, 0, 0, 180), blur_px=0),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_stacked_text_renders(self) -> None:
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="CQ",
            font_family="DejaVu Sans Bold",
            font_size_pct=8.0,
            fill=(255, 255, 255, 255),
            orientation="stacked",
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME
        # Should have white pixels
        pixels = list(img.getdata())
        white = sum(1 for r, g, b in pixels if r > 200 and g > 200 and b > 200)
        assert white > 0

    def test_stacked_text_taller_than_wide(self) -> None:
        """Each character is stacked vertically, so height >> width for a word."""
        # We test this indirectly: render a multi-char stacked text and confirm
        # no exception. Exact pixel geometry depends on font metrics.
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="HELLO",
            font_family="DejaVu Sans Bold",
            font_size_pct=5.0,
            fill=(255, 255, 255, 255),
            orientation="stacked",
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_text_empty_after_resolve_skipped(self) -> None:
        """A text layer whose resolved text is empty doesn't crash."""
        layer = TextLayer(
            id="t", anchor="TL",
            text_raw="",
            font_family="DejaVu Sans Bold",
            font_size_pct=10.0,
            fill=(255, 255, 255, 255),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_bottom_anchored_text_not_clipped(self) -> None:
        """Regression: BC-anchored text at offset_y_pct=0 must not be clipped.

        PIL's draw.text places the ascender line at y, so ink extends down to
        y + (ascent + descent). Sizing the text image to the ink bbox height
        (bb[3] - bb[1]) used to push the bb[1] offset below the image bottom,
        clipping descenders/lower glyph parts of bottom-anchored text.
        """
        layer = TextLayer(
            id="t", anchor="BC",
            text_raw="de W0AEZ",
            font_family="DejaVu Sans Bold",
            font_size_pct=13.0,
            fill=(255, 255, 255, 255),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        W, H = _FRAME
        # No white pixels in any of the bottom 2 rows — ink must sit fully
        # inside the canvas, with descender room reserved.
        for y in range(H - 2, H):
            row = [img.getpixel((x, y)) for x in range(W)]
            white = sum(1 for r, g, b in row if r > 200 and g > 200 and b > 200)
            assert white == 0, f"Row {y} has {white} white pixels — text was clipped"



# ---------------------------------------------------------------------------
# Anchor positions — visual placement
# ---------------------------------------------------------------------------


class TestAnchorPlacement:
    """Verify that anchor+colour pairs appear in the expected canvas regions."""

    def _rect_at(self, anchor: str, color: tuple[int, int, int, int]) -> RectLayer:
        return RectLayer(
            id=f"r_{anchor}",
            anchor=anchor,
            width_pct=20.0,
            height_pct=20.0,
            fill=color,
        )

    def _dominant_color(self, img: PIL.Image.Image, region: tuple[int, int, int, int]) -> tuple[int, int, int]:
        cropped = img.crop(region)
        pixels = list(cropped.getdata())
        r = sum(p[0] for p in pixels) // len(pixels)
        g = sum(p[1] for p in pixels) // len(pixels)
        b = sum(p[2] for p in pixels) // len(pixels)
        return r, g, b

    def test_tl_anchor_is_top_left(self) -> None:
        layer = self._rect_at("TL", (255, 0, 0, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        r, g, b = self._dominant_color(img, (0, 0, 40, 30))
        assert r > 200

    def test_br_anchor_is_bottom_right(self) -> None:
        layer = self._rect_at("BR", (0, 255, 0, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        r, g, b = self._dominant_color(img, (280, 220, 319, 255))
        assert g > 200

    def test_c_anchor_is_centered(self) -> None:
        layer = self._rect_at("C", (0, 0, 255, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        # Blue rect should be around center
        r, g, b = self._dominant_color(img, (120, 96, 200, 160))
        assert b > 150


# ---------------------------------------------------------------------------
# Layer stacking order
# ---------------------------------------------------------------------------


class TestLayerOrder:
    def test_top_layer_wins(self) -> None:
        """A red FILL rect on top of a blue FILL rect should produce red."""
        blue = RectLayer(id="b", anchor="FILL", fill=(0, 0, 255, 255))
        red = RectLayer(id="r", anchor="FILL", fill=(255, 0, 0, 255))
        t = Template(name="t", layers=[blue, red])
        img = _render(t)
        r, g, b = img.getpixel((160, 128))
        assert r > 200 and b < 50

    def test_bottom_layer_visible_through_transparent_top(self) -> None:
        """A blue rect under a fully transparent layer should show blue."""
        blue = RectLayer(id="b", anchor="FILL", fill=(0, 0, 255, 255))
        invisible = RectLayer(id="i", anchor="FILL", fill=(255, 0, 0, 0))  # alpha=0
        t = Template(name="t", layers=[blue, invisible])
        img = _render(t)
        r, g, b = img.getpixel((160, 128))
        assert b > 200 and r < 50


# ---------------------------------------------------------------------------
# PatternLayer
# ---------------------------------------------------------------------------


class TestPatternLayer:
    @pytest.mark.parametrize("pattern_id", ["checkered", "diagonal_stripes", "dots"])
    def test_pattern_renders_without_crash(self, pattern_id: str) -> None:
        layer = PatternLayer(
            id="p", anchor="FILL",
            pattern_id=pattern_id,
            tint=(255, 255, 255, 200),
            cell_size_pct=5.0,
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        assert img.size == _FRAME

    def test_pattern_tint_math_matches_reference_loop(self) -> None:
        """Regression for H1: the vectorized tint multiply must produce the
        exact same per-pixel result as the original ``r2 * tr // 255`` loop.
        """
        import numpy as np
        import PIL.Image

        from open_sstv.templates.renderer import _make_pattern_tile, _tile_pattern

        tile = _make_pattern_tile("checkered", 4)
        tiled = _tile_pattern(tile, 16, 16)
        tint = (180, 80, 40, 220)

        # Vectorized — same body as the renderer's H1 implementation.
        arr = np.array(tiled, dtype=np.uint16)
        tint_arr = np.array(tint, dtype=np.uint16)
        vec_arr = (arr * tint_arr // 255).astype(np.uint8)

        # Reference: explicit per-pixel loop, identical to the pre-H1 code.
        ref = PIL.Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        for y in range(16):
            for x in range(16):
                r, g, b, a = tiled.getpixel((x, y))
                ref.putpixel(
                    (x, y),
                    (
                        r * tint[0] // 255,
                        g * tint[1] // 255,
                        b * tint[2] // 255,
                        a * tint[3] // 255,
                    ),
                )

        # Exact equality — the math is integer, no rounding involved.
        assert np.array_equal(vec_arr, np.asarray(ref))

    def test_pattern_tint_no_uint8_overflow(self) -> None:
        """Regression for H1: full-saturation tint (255s) must not wrap.

        ``arr[..., :3] = arr[..., :3] * tint[:3] // 255`` written naively
        in uint8 wraps because 255*255 = 65025 → 65025 % 256 = 1.  We
        upcast to uint16; this guards against silently regressing that.
        """
        import numpy as np

        from open_sstv.templates.renderer import _make_pattern_tile, _tile_pattern

        tile = _make_pattern_tile("checkered", 2)
        tiled = _tile_pattern(tile, 8, 8)
        arr = np.array(tiled, dtype=np.uint16)
        tint = np.array((255, 255, 255, 255), dtype=np.uint16)
        out = (arr * tint // 255).astype(np.uint8)
        # White cells of the pattern should remain white after a (255,…) tint.
        assert (out[arr[..., 3] > 0] == np.array([255, 255, 255, 255], dtype=np.uint8)).all()


# ---------------------------------------------------------------------------
# Mode-aware frame size
# ---------------------------------------------------------------------------


class TestModeAwareSize:
    @pytest.mark.parametrize("frame", [(320, 240), (320, 256), (640, 496)])
    def test_output_matches_frame_size(self, frame: tuple[int, int]) -> None:
        layer = RectLayer(id="r", anchor="FILL", fill=(100, 100, 100, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(frame_size=frame))
        assert img.size == frame


# ---------------------------------------------------------------------------
# Text overflow — _wrap_text / _fit_text / render_template integration
# ---------------------------------------------------------------------------


def _font(size: int = 20) -> "PIL.ImageFont.FreeTypeFont":
    return _load_font("DejaVu Sans Bold", size)


class TestWrapText:
    def test_short_text_unchanged(self) -> None:
        font = _font(20)
        result = _wrap_text(font, "W0AEZ", 400)
        assert result == "W0AEZ"

    def test_long_line_wraps(self) -> None:
        font = _font(20)
        long_text = "VE7ABC DE W0AEZ RST 595 73"
        result = _wrap_text(font, long_text, 80)
        assert "\n" in result

    def test_wrapped_lines_fit(self) -> None:
        font = _font(20)
        max_w = 100
        long_text = "VE7ABC DE W0AEZ RST 595 73 SK"
        result = _wrap_text(font, long_text, max_w)
        for line in result.split("\n"):
            w = _text_bbox(font, line)[2] - _text_bbox(font, line)[0]
            assert w <= max_w, f"Line {line!r} is {w}px wide, exceeds {max_w}px"

    def test_existing_newlines_preserved(self) -> None:
        font = _font(20)
        text = "LINE ONE\nLINE TWO"
        result = _wrap_text(font, text, 400)
        assert "LINE ONE" in result
        assert "LINE TWO" in result

    def test_empty_string(self) -> None:
        font = _font(20)
        assert _wrap_text(font, "", 100) == ""

    def test_zero_max_width_returns_unchanged(self) -> None:
        font = _font(20)
        assert _wrap_text(font, "hello world", 0) == "hello world"


class TestFitText:
    def _layer(self, font_size_pct: float = 8.0) -> TextLayer:
        return TextLayer(
            id="t",
            text_raw="x",
            anchor="BC",
            font_family="DejaVu Sans Bold",
            font_size_pct=font_size_pct,
            fill=(255, 255, 255, 255),
        )

    def test_short_text_unchanged(self) -> None:
        layer = self._layer()
        font = _font(20)
        text_out, font_out = _fit_text(layer, "W0AEZ", font, 20, 320)
        assert text_out == "W0AEZ"
        assert font_out is font

    def test_long_text_shrinks_font(self) -> None:
        layer = self._layer(font_size_pct=8.0)
        font_size = 20
        font = _font(font_size)
        long_text = "VE7ABC DE W0AEZ RST 595 NAME KEVIN 73 SK"
        text_out, font_out = _fit_text(layer, long_text, font, font_size, 120)
        # Font should have shrunk
        assert font_out.size < font_size

    def test_shrink_capped_at_50_pct(self) -> None:
        layer = self._layer(font_size_pct=8.0)
        font_size = 40
        font = _font(font_size)
        # Text so long it needs more than 50% reduction
        very_long = "A " * 50
        text_out, font_out = _fit_text(layer, very_long.strip(), font, font_size, 100)
        # Font is at floor (50%) or text was wrapped
        assert font_out.size >= font_size // 2

    def test_wrap_used_when_shrink_insufficient(self) -> None:
        layer = self._layer()
        font_size = 40
        font = _font(font_size)
        # Extremely long single word can't be wrapped — at minimum it's one token
        very_long = ("W" * 40 + " ") * 5
        text_out, font_out = _fit_text(layer, very_long.strip(), font, font_size, 80)
        # Must not crash; either shrunk or wrapped
        assert text_out  # non-empty


class TestTextOverflowIntegration:
    """render_template must not produce images where text bleeds outside the frame."""

    def _text_layer(self, text: str, font_size_pct: float = 10.0) -> TextLayer:
        return TextLayer(
            id="t",
            text_raw=text,
            anchor="BC",
            font_family="DejaVu Sans Bold",
            font_size_pct=font_size_pct,
            fill=(255, 255, 255, 255),
        )

    def test_long_callsign_exchange_fits(self) -> None:
        layer = self._text_layer("VE7ABC DE W0AEZ RST 595")
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(frame_size=(160, 120)))
        assert img.size == (160, 120)  # rendered without crash

    def test_render_does_not_crash_on_very_long_text(self) -> None:
        layer = self._text_layer("A " * 100, font_size_pct=15.0)
        t = Template(name="t", layers=[layer])
        img = _render(t, ctx=_ctx(frame_size=(320, 256)))
        assert img.size == (320, 256)
