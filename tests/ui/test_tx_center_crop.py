# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the TX-pipeline center-crop step.

Two surfaces:

* The pure helper ``_center_crop_to_size`` — no Qt, just PIL.
* The integration with ``TxPanel._compose_template`` — verifies that
  the renderer receives a photo at the SSTV mode's native frame size
  even when the loaded image has a different aspect or resolution.

The original ``self._base_image`` must never be mutated so the user
can still pull up the editor on the original.
"""
from __future__ import annotations

from pathlib import Path

import PIL.Image
import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.templates.model import (
    PhotoLayer,
    RectLayer,
    Template,
    TextLayer,
)
from open_sstv.templates.toml_io import save_template
from open_sstv.ui.tx_panel import TxPanel, _center_crop_to_size


# ---------------------------------------------------------------------------
# Pure helper — no Qt
# ---------------------------------------------------------------------------


class TestCenterCropIdentity:
    """Photo whose size already matches must be returned unchanged."""

    def test_returns_same_object_when_size_matches(self) -> None:
        src = Image.new("RGB", (320, 256), (10, 20, 30))
        out = _center_crop_to_size(src, 320, 256)
        assert out is src

    def test_pixel_data_unchanged_when_size_matches(self) -> None:
        src = Image.new("RGB", (320, 256), (200, 100, 50))
        out = _center_crop_to_size(src, 320, 256)
        assert list(out.getdata()) == list(src.getdata())


class TestCenterCropOutputSize:
    """Output dims must always equal the requested target."""

    @pytest.mark.parametrize(
        ("src_w", "src_h", "tgt_w", "tgt_h"),
        [
            (4032, 3024, 320, 256),  # phone landscape → Scottie S1
            (3024, 4032, 320, 256),  # phone portrait → Scottie S1
            (640, 480, 320, 256),    # 4:3 → 5:4
            (1920, 1080, 320, 256),  # 16:9 → 5:4
            (320, 256, 320, 256),    # already match
            (640, 512, 320, 256),    # same aspect, larger
            (160, 128, 320, 256),    # same aspect, smaller (upscale)
            (1, 1, 320, 256),        # degenerate — must still produce target
        ],
    )
    def test_output_is_exactly_target_size(
        self, src_w: int, src_h: int, tgt_w: int, tgt_h: int
    ) -> None:
        src = Image.new("RGB", (src_w, src_h), (128, 128, 128))
        out = _center_crop_to_size(src, tgt_w, tgt_h)
        assert out.size == (tgt_w, tgt_h)


class TestCenterCropDoesNotMutateOriginal:
    def test_landscape_source_size_unchanged(self) -> None:
        src = Image.new("RGB", (4032, 3024), (10, 20, 30))
        original_size = src.size
        _ = _center_crop_to_size(src, 320, 256)
        assert src.size == original_size

    def test_landscape_source_pixels_unchanged(self) -> None:
        """The crop step must not touch the source image's pixel buffer."""
        src = Image.new("RGB", (640, 480), (50, 100, 150))
        before = list(src.getdata())
        _ = _center_crop_to_size(src, 320, 256)
        assert list(src.getdata()) == before


class TestCenterCropContent:
    """The crop must preserve the *centre* of the image, not the corners."""

    def test_wider_than_target_keeps_horizontal_centre(self) -> None:
        """A 2:1 source with an off-centre coloured stripe in the middle
        column must keep that stripe in the cropped output."""
        # 600x300 source: left 200px red, middle 200px green, right 200px blue.
        src = Image.new("RGB", (600, 300), (255, 0, 0))
        for x in range(200, 400):
            for y in range(300):
                src.putpixel((x, y), (0, 255, 0))
        for x in range(400, 600):
            for y in range(300):
                src.putpixel((x, y), (0, 0, 255))

        # Target 320x256 is 1.25 aspect; src is 2.0 — wider.  Crop keeps
        # vertical-centre column.  Centre pixel of output should still
        # be green (preserved from source middle).
        out = _center_crop_to_size(src, 320, 256)
        cx, cy = out.size[0] // 2, out.size[1] // 2
        r, g, b = out.getpixel((cx, cy))
        assert g > 200 and r < 100 and b < 100, (
            f"centre pixel {(r, g, b)} is not green — "
            "wide source did not keep horizontal centre"
        )

    def test_taller_than_target_keeps_vertical_centre(self) -> None:
        """A portrait source with an off-centre coloured band in the
        middle row must keep that band in the output."""
        # 300x600: top 200 red, middle 200 green, bottom 200 blue.
        src = Image.new("RGB", (300, 600), (255, 0, 0))
        for y in range(200, 400):
            for x in range(300):
                src.putpixel((x, y), (0, 255, 0))
        for y in range(400, 600):
            for x in range(300):
                src.putpixel((x, y), (0, 0, 255))

        out = _center_crop_to_size(src, 320, 256)
        cx, cy = out.size[0] // 2, out.size[1] // 2
        r, g, b = out.getpixel((cx, cy))
        assert g > 200 and r < 100 and b < 100, (
            f"centre pixel {(r, g, b)} is not green — "
            "tall source did not keep vertical centre"
        )

    def test_same_aspect_just_resizes(self) -> None:
        """A 640x512 source (same 1.25 aspect as 320x256) must just
        scale down — every column/row should still be present in
        proportion, no horizontal/vertical content trimmed."""
        # Vertical bands across the full source width.
        src = Image.new("RGB", (640, 512), (0, 0, 0))
        for x in range(640):
            for y in range(512):
                # Red ramp left→right covering the FULL width.
                src.putpixel((x, y), (int(x * 255 / 639), 0, 0))

        out = _center_crop_to_size(src, 320, 256)
        # Left edge should be near-black (low R), right edge near-red.
        left = out.getpixel((0, 128))
        right = out.getpixel((319, 128))
        assert left[0] < 30, f"left pixel R={left[0]} — content trimmed"
        assert right[0] > 220, f"right pixel R={right[0]} — content trimmed"


# ---------------------------------------------------------------------------
# Integration with TxPanel._compose_template
# ---------------------------------------------------------------------------


@pytest.fixture
def tdir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    return d


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(callsign="W0AEZ")


def _full_canvas_template() -> Template:
    return Template(
        name="t",
        role="cq",
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            RectLayer(
                id="banner",
                anchor="BL",
                width_pct=100.0,
                height_pct=20.0,
                fill=(0, 0, 0, 200),
            ),
            TextLayer(
                id="call",
                text_raw="%c",
                anchor="BC",
                font_family="DejaVu Sans Bold",
                font_size_pct=8.0,
                fill=(255, 255, 255, 255),
            ),
        ],
    )


class TestComposeTemplatePreCrop:
    """``TxPanel._compose_template`` must hand the renderer a photo at
    the mode's native frame size, regardless of the source image.

    These tests use ``pytestmark = pytest.mark.gui`` to indicate they
    need a Qt event loop (TxPanel is a QWidget)."""

    pytestmark = pytest.mark.gui

    def _selected_template(self, panel: TxPanel) -> None:
        card = panel._gallery._cards[0]
        panel._gallery._on_card_clicked(card.template)

    def test_renderer_receives_photo_at_mode_size(
        self,
        qtbot,
        tmp_path: Path,
        tdir: Path,
        cfg: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spy on render_template; assert TXContext.photo_image arrives
        at exactly (mode.width, mode.display_height) when the source
        is a 4:3 photo and the mode is 5:4 Scottie S1."""
        save_template(_full_canvas_template(), tdir / "t.toml")

        # 640x480 photo (4:3) at /tmp; well off the Scottie S1 5:4 aspect.
        src = Image.new("RGB", (640, 480), (64, 128, 192))
        src_path = tmp_path / "phone.png"
        src.save(src_path)

        panel = TxPanel(app_config=cfg, templates_dir=tdir)
        qtbot.addWidget(panel)
        panel.load_image(src_path)
        panel._mode_combo.setCurrentIndex(
            panel._mode_combo.findData(Mode("scottie_s1"))
        )

        # Capture what the renderer receives.
        captured: dict[str, object] = {}

        import open_sstv.ui.tx_panel as tx_mod

        original = tx_mod.render_template

        def spy(template, qso, app_cfg, ctx):  # type: ignore[no-untyped-def]
            captured["photo_size"] = ctx.photo_image.size
            captured["frame_size"] = ctx.frame_size
            return original(template, qso, app_cfg, ctx)

        monkeypatch.setattr(tx_mod, "render_template", spy)

        self._selected_template(panel)
        result = panel._compose_template()

        assert result is not None
        spec = MODE_TABLE[Mode("scottie_s1")]
        target = (spec.width, spec.display_height)
        assert captured["frame_size"] == target
        assert captured["photo_size"] == target, (
            f"renderer got photo at {captured['photo_size']} but "
            f"expected mode-native {target}"
        )

    def test_base_image_not_mutated_by_compose(
        self,
        qtbot,
        tmp_path: Path,
        tdir: Path,
        cfg: AppConfig,
    ) -> None:
        """Composing must leave ``self._base_image`` at its original
        dimensions so the editor and preview still see the unmodified
        source."""
        save_template(_full_canvas_template(), tdir / "t.toml")

        src = Image.new("RGB", (640, 480), (64, 128, 192))
        src_path = tmp_path / "phone.png"
        src.save(src_path)

        panel = TxPanel(app_config=cfg, templates_dir=tdir)
        qtbot.addWidget(panel)
        panel.load_image(src_path)
        self._selected_template(panel)

        original_size = panel._base_image.size
        _ = panel._compose_template()
        assert panel._base_image.size == original_size

    def test_already_correct_size_passes_through(
        self,
        qtbot,
        tmp_path: Path,
        tdir: Path,
        cfg: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A photo already at the mode's frame size must reach the
        renderer untouched (same object, no LANCZOS resample)."""
        save_template(_full_canvas_template(), tdir / "t.toml")

        spec = MODE_TABLE[Mode("scottie_s1")]
        src = Image.new("RGB", (spec.width, spec.display_height), (200, 50, 50))
        src_path = tmp_path / "exact.png"
        src.save(src_path)

        panel = TxPanel(app_config=cfg, templates_dir=tdir)
        qtbot.addWidget(panel)
        panel.load_image(src_path)
        panel._mode_combo.setCurrentIndex(
            panel._mode_combo.findData(Mode("scottie_s1"))
        )
        self._selected_template(panel)

        captured: dict[str, object] = {}
        import open_sstv.ui.tx_panel as tx_mod

        original = tx_mod.render_template

        def spy(template, qso, app_cfg, ctx):  # type: ignore[no-untyped-def]
            captured["photo_obj_id"] = id(ctx.photo_image)
            return original(template, qso, app_cfg, ctx)

        monkeypatch.setattr(tx_mod, "render_template", spy)
        _ = panel._compose_template()

        assert captured["photo_obj_id"] == id(panel._base_image), (
            "exact-size photo should be passed through unchanged "
            "(same object identity), not re-cropped/resampled."
        )
