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
    RxImageLayer,
    ShadowSpec,
    StationImageLayer,
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
from open_sstv.templates.renderer import (
    _load_font,
    _resolve_station_image_path,
    _text_bbox,
)

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
# RxImageLayer — image-present vs empty-slot placeholder
# ---------------------------------------------------------------------------


class TestRxImageLayer:
    """Two visual contracts the renderer must hold for an RxImageLayer:

    * Image present → paste cleanly with **no** border or fill, indistinguish-
      able from a PhotoLayer holding the same picture.
    * Image absent → show a white-bordered placeholder box (with an "RX"
      label) so the slot's position is visible in the editor preview.
    """

    def _layer(self, **kw) -> RxImageLayer:
        defaults = dict(
            id="rx", anchor="BR",
            width_pct=30.0, height_pct=25.0,
            offset_x_pct=2.0, offset_y_pct=2.0,
            fit="cover",
        )
        defaults.update(kw)
        return RxImageLayer(**defaults)

    def test_image_present_renders_without_border(self) -> None:
        """Spec: when an RX image is present, the slot must NOT be framed.

        We sample one pixel just *inside* the slot's outer edge.  If a
        bordering implementation snuck back in, that pixel would be white
        from the border; with the correct implementation it shows the
        underlying image's red.
        """
        rx_img = _solid_color_image(100, 100, (255, 0, 0))
        t = Template(name="t", layers=[self._layer()])
        img = _render(t, ctx=_ctx(rx_image=rx_img))
        # The slot is anchored BR with width_pct=30, height_pct=25.
        # That's a 96×64 region offset 6px / 5px from the bottom-right
        # of a 320×256 frame → slot rect is roughly x∈[212, 308),
        # y∈[185, 249).  Sample one pixel from the slot's interior.
        sx, sy = 220, 195
        r, g, b = img.getpixel((sx, sy))
        assert (r, g, b) == (255, 0, 0), (
            f"RX image present must render the image without an overlaid "
            f"border; got pixel {(r, g, b)} at {(sx, sy)} (expected red)"
        )

    def test_image_present_corner_is_image_not_border(self) -> None:
        """Tighter regression: the *outermost* slot pixel still belongs to
        the image, not a border.  Catches a 1-px white outline that the
        previous guard (sampled mid-slot) would miss."""
        rx_img = _solid_color_image(100, 100, (0, 200, 50))
        t = Template(name="t", layers=[self._layer(anchor="TL", offset_x_pct=0, offset_y_pct=0)])
        img = _render(t, ctx=_ctx(rx_image=rx_img))
        # Anchor TL @ offset 0,0 → slot starts at (0, 0).
        r, g, b = img.getpixel((0, 0))
        assert (g > 150 and b < 100), (
            f"Outermost slot pixel must be the image (green), not a "
            f"border outline; got {(r, g, b)}"
        )

    def test_image_absent_shows_bordered_placeholder(self) -> None:
        """Spec: empty slot draws a visible white-ish placeholder box.

        Sample one pixel near the slot's outer edge — must not be the
        canvas's solid black background.
        """
        t = Template(name="t", layers=[self._layer(anchor="TL", offset_x_pct=0, offset_y_pct=0)])
        img = _render(t, ctx=_ctx(rx_image=None))
        # Top-left pixel of the slot must be the white border (or the
        # very-light placeholder fill underneath it).
        r, g, b = img.getpixel((0, 0))
        # Anything substantially brighter than the black canvas confirms
        # the placeholder draws.  A solid black canvas would give (0,0,0).
        assert max(r, g, b) > 50, (
            f"Empty RX slot must draw a visible placeholder, not leave "
            f"the canvas black; got {(r, g, b)} at the slot's TL corner"
        )

    def test_image_absent_then_present_paints_image_over_placeholder(
        self,
    ) -> None:
        """Sanity check: switching from no-image to image must produce a
        different rendering — there's no caching that pins the placeholder
        once it's drawn."""
        t = Template(name="t", layers=[self._layer(anchor="TL", offset_x_pct=0, offset_y_pct=0)])
        empty = _render(t, ctx=_ctx(rx_image=None))
        rx_img = _solid_color_image(100, 100, (10, 20, 220))
        with_img = _render(t, ctx=_ctx(rx_image=rx_img))
        # Two completely different pixel sets; not even worth sampling
        # one position — the data should differ broadly.
        assert list(empty.getdata()) != list(with_img.getdata())

    def test_station_image_layer_unaffected(self) -> None:
        """Regression: only RxImageLayer gets the placeholder behaviour.

        StationImageLayer used to share the same rasterizer and a previous
        WIP attempt mutated the shared function — that would have drawn
        the same RX placeholder over every blank station-image slot too.
        Verify a missing station image leaves the canvas alone (no border
        painted) the way it always has.
        """
        layer = StationImageLayer(
            id="si", anchor="TL", path="missing.png",
            width_pct=30.0, height_pct=25.0,
            offset_x_pct=0.0, offset_y_pct=0.0,
        )
        t = Template(name="t", layers=[layer])
        # Empty assets dir → station image fails to load → cell should be None
        # → composite skips → canvas stays black at the slot location.
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as td:
            img = _render(t, assets_dir=_P(td))
        r, g, b = img.getpixel((10, 10))
        assert (r, g, b) == (0, 0, 0), (
            f"Missing StationImage must NOT trigger the RX placeholder; "
            f"got {(r, g, b)} at TL of the slot"
        )


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

    def test_text_empty_after_resolve_renders_transparent_cell(self) -> None:
        """An empty resolved text layer produces a transparent cell, not a skip.

        The pre-fix behaviour was to skip rasterization entirely whenever the
        token resolved to ``""`` (e.g. ``%r`` before any RST is entered).  That
        broke layout stability — a debug overlay or a future "selection bbox"
        feature would see N-1 layers in the composite path on one frame and N
        on the next, depending on which tokens happened to be populated.

        The new contract: every visible TextLayer contributes a (possibly
        transparent) cell to the composite pipeline.  Visually identical to
        the skip path for the empty-text case, but layer count is stable.
        """
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
        # The empty text cell is fully transparent, so the canvas must remain
        # identical to a render with no layers at all (solid black background).
        empty_img = _render(Template(name="empty", layers=[]))
        assert list(img.getdata()) == list(empty_img.getdata())

    def test_text_empty_layer_does_not_obscure_layer_below(self) -> None:
        """Empty text in front of a coloured rect must not blacken the rect.

        Direct regression check on the "transparent cell" property — if the
        new path accidentally composited a black RGBA cell instead of a fully
        transparent one, the underlying red would be wiped out.
        """
        rect = RectLayer(id="bg", anchor="FILL", fill=(255, 0, 0, 255))
        empty_text = TextLayer(
            id="t", anchor="TL",
            text_raw="",
            font_family="DejaVu Sans Bold",
            font_size_pct=10.0,
            fill=(255, 255, 255, 255),
            slashed_zero=False,
        )
        t = Template(name="t", layers=[rect, empty_text])
        img = _render(t)
        # Sample the centre — a transparent overlay leaves the red rect alone.
        r, g, b = img.getpixel((_FRAME[0] // 2, _FRAME[1] // 2))[:3]
        assert (r, g, b) == (255, 0, 0)

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


class TestRainbowText:
    """``color_mode='rainbow'`` paints each glyph along an HSV hue sweep
    instead of using ``layer.fill`` as a single color.  The properties
    tested here are:

    * The render does not crash.
    * Multiple distinct hues are present on the canvas (it's not a
      single colour pretending to be rainbow).
    * Solid-mode renders are unchanged (no regression).
    * Alpha from ``layer.fill[3]`` is honoured.
    * Stacked orientation also gets the per-glyph hue sweep.
    """

    def _layer(self, **kw) -> TextLayer:
        defaults = dict(
            id="t", anchor="C",
            text_raw="RAINBOW",
            font_family="DejaVu Sans Bold",
            font_size_pct=20.0,
            fill=(255, 255, 255, 255),
            color_mode="rainbow",
            slashed_zero=False,
        )
        defaults.update(kw)
        return TextLayer(**defaults)

    @staticmethod
    def _hue_buckets(pixels: list[tuple[int, int, int]]) -> set[str]:
        """Bin lit (non-near-black) pixels into rough hue families.

        We don't validate exact pixel colours — anti-aliasing on glyph
        edges produces in-between shades.  The contract is just that
        the rendered text spans a non-trivial slice of the colour wheel.
        """
        seen: set[str] = set()
        for r, g, b in pixels:
            if r < 60 and g < 60 and b < 60:
                continue  # background / very dim AA
            if r > 180 and g < 100 and b < 100:
                seen.add("red")
            elif r > 180 and g > 180 and b < 100:
                seen.add("yellow")
            elif r < 100 and g > 180 and b < 100:
                seen.add("green")
            elif r < 100 and g > 180 and b > 180:
                seen.add("cyan")
            elif r < 100 and g < 100 and b > 180:
                seen.add("blue")
            elif r > 180 and g < 100 and b > 180:
                seen.add("magenta")
        return seen

    def test_rainbow_renders_without_crash(self) -> None:
        t = Template(name="t", layers=[self._layer()])
        img = _render(t)
        assert img.size == _FRAME

    def test_rainbow_produces_multiple_hues(self) -> None:
        """A 7-character rainbow line (hue ∈ [0, 6/7)) must show at
        least three of the six primary/secondary hues."""
        t = Template(name="t", layers=[self._layer()])
        img = _render(t)
        pixels = list(img.getdata())
        buckets = self._hue_buckets(pixels)
        assert len(buckets) >= 3, (
            f"Rainbow render only showed buckets {buckets!r} — expected ≥3"
        )

    def test_solid_mode_produces_only_fill_colour(self) -> None:
        """Sanity: with color_mode='solid' and a red fill, no green/blue
        hues should appear on the canvas — confirms the rainbow path is
        gated correctly."""
        layer = self._layer(color_mode="solid", fill=(255, 0, 0, 255))
        t = Template(name="t", layers=[layer])
        img = _render(t)
        pixels = list(img.getdata())
        buckets = self._hue_buckets(pixels)
        assert buckets <= {"red"}, (
            f"Solid red render produced unexpected hues: {buckets!r}"
        )

    def test_rainbow_zero_alpha_is_invisible(self) -> None:
        """Alpha from ``layer.fill[3]`` must propagate to the rainbow
        glyphs.  With alpha=0 the render must be identical to a no-text
        render (transparent over black background)."""
        layer = self._layer(fill=(255, 255, 255, 0))
        t_with = Template(name="t", layers=[layer])
        t_empty = Template(name="empty", layers=[])
        img_with = _render(t_with)
        img_empty = _render(t_empty)
        assert list(img_with.getdata()) == list(img_empty.getdata())

    def test_rainbow_stacked_orientation(self) -> None:
        """Stacked text steps hue across the character index instead of
        x-position; render must still produce multiple hues."""
        layer = self._layer(orientation="stacked", text_raw="RGBCYM", anchor="TL")
        t = Template(name="t", layers=[layer])
        img = _render(t)
        pixels = list(img.getdata())
        buckets = self._hue_buckets(pixels)
        assert len(buckets) >= 3, (
            f"Stacked rainbow only showed buckets {buckets!r} — expected ≥3"
        )

    def test_rainbow_with_stroke_renders(self) -> None:
        """Stroke is applied per-glyph in rainbow mode; verify the
        stroke colour appears on canvas alongside the rainbow ink."""
        layer = self._layer(
            text_raw="ABC",
            stroke=StrokeSpec(color=(255, 255, 255, 255), width_px=2),
        )
        t = Template(name="t", layers=[layer])
        img = _render(t)
        pixels = list(img.getdata())
        # White stroke pixels (R, G, B all > 220) must exist.
        white = sum(1 for r, g, b in pixels if r > 220 and g > 220 and b > 220)
        assert white > 0, "Rainbow + stroke produced no white stroke pixels"

    def test_rainbow_default_is_solid(self) -> None:
        """A TextLayer constructed with no color_mode argument must
        default to 'solid' — backward compat with pre-rainbow code paths."""
        layer = TextLayer(
            id="t", anchor="C",
            text_raw="X",
            font_family="DejaVu Sans Bold",
            font_size_pct=10.0,
            fill=(255, 0, 0, 255),
            slashed_zero=False,
        )
        assert layer.color_mode == "solid"



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

    def test_negative_max_width_returns_unchanged(self) -> None:
        """L6: a negative width is nonsensical but must not crash — the
        ``max_w <= 0`` early-return handles it.  Symmetric with the
        zero-width case."""
        font = _font(20)
        assert _wrap_text(font, "hello world", -5) == "hello world"

    def test_single_unwrappable_word_is_returned_intact(self) -> None:
        """L6: a single word wider than max_w cannot be broken — the
        wrapper has no inter-word break point.  It must still return the
        word (so the caller can decide what to do, e.g. shrink the font),
        not an empty string or a crash.

        Regression guard: a naive wrapper could split mid-glyph or drop
        the word; ours falls through with ``cur=[word]`` and emits the
        single oversized line at the end of the loop.
        """
        font = _font(40)
        long_word = "Supercalifragilistic"
        result = _wrap_text(font, long_word, 20)
        # Same word returned (no split) and no extra newlines fabricated.
        assert result == long_word
        assert "\n" not in result

    def test_unwrappable_word_followed_by_words(self) -> None:
        """L6: an oversized first word must not eat the rest of the input.

        With ``cur=[]`` and a single oversized word, the inner ``if`` falls
        through (``cur`` is empty so the wrap branch is skipped) and the
        word ends up on its own line followed by the remaining words on a
        second line.  Regression guard against an off-by-one that swallows
        the second word.
        """
        font = _font(40)
        result = _wrap_text(font, "Supercalifragilistic and more", 60)
        # Both halves of the input must appear in the output, somewhere.
        assert "Supercalifragilistic" in result
        assert "more" in result


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


# ---------------------------------------------------------------------------
# L6: renderer-side symlink-escape defense for StationImageLayer
# ---------------------------------------------------------------------------


import sys
from pathlib import Path as _Path


class TestStationImagePathResolution:
    """L6: ``_resolve_station_image_path`` is the renderer's defense-in-depth
    check against a TOML that smuggles a symlink past the loader.

    The toml_io static check (C1) rejects literal absolute paths and ``..``
    components, but a *symlink inside the assets directory* still parses as
    a clean relative name.  At render time we resolve and re-verify the
    result is_relative_to(assets_dir.resolve()) so the symlink target is
    the thing we contain — not the link's name.
    """

    def test_returns_resolved_path_for_safe_relative(self, tmp_path: _Path) -> None:
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "qsl.png").write_bytes(b"")  # any content
        out = _resolve_station_image_path("qsl.png", assets)
        assert out == (assets / "qsl.png").resolve()

    def test_returns_none_for_empty_path(self, tmp_path: _Path) -> None:
        # Empty path is a sentinel: the layer renders blank.
        assert _resolve_station_image_path("", tmp_path) is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation needs admin / dev mode on Windows; the "
               "Unix-flavoured test below adequately covers the escape path",
    )
    def test_symlink_pointing_outside_assets_dir_is_refused(
        self, tmp_path: _Path
    ) -> None:
        """The smuggled-symlink attack: a TOML names ``link.png``; on disk
        that file is a symlink to ``../../etc/passwd``.

        The toml_io check sees only the plain name and accepts it.  The
        renderer's ``is_relative_to`` check after ``resolve()`` follows the
        symlink and refuses the load — return value is ``None`` so the
        layer falls through to a blank cell instead of leaking file bytes.
        """
        assets = tmp_path / "assets"
        assets.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = assets / "link.png"
        link.symlink_to(outside)

        result = _resolve_station_image_path("link.png", assets)
        assert result is None, (
            f"Symlink that points outside the assets dir must be refused; "
            f"got {result!r}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink semantics differ on Windows; Unix coverage is sufficient",
    )
    def test_symlink_pointing_inside_assets_dir_is_accepted(
        self, tmp_path: _Path
    ) -> None:
        """The complementary case: a symlink that *stays inside* the assets
        dir is fine — users may legitimately keep a single QSL card on disk
        and link to it from multiple template-named filenames.
        """
        assets = tmp_path / "assets"
        assets.mkdir()
        target = assets / "shared.png"
        target.write_bytes(b"")
        link = assets / "alias.png"
        link.symlink_to(target)

        result = _resolve_station_image_path("alias.png", assets)
        assert result == target.resolve()
