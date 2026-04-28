# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the v0.3 TOML round-trip serialisation (``toml_io``).

Covers:
- All seven layer types survive a save→load round-trip.
- StrokeSpec and ShadowSpec round-trip correctly.
- RGBA lists ↔ tuples conversion.
- "text" key in TOML maps to text_raw in TextLayer.
- Schema version validation (schema_version > 1 raises SchemaVersionError).
- Unknown fields silently ignored (forward-compat).
- Unknown layer types silently skipped.
- Missing required "id" field logs and skips layer.
- Missing "name" field raises TemplateLoadError.
- Atomic write (tmp file then replace).
- All 8 bundled starter templates load without error.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

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
from open_sstv.templates.toml_io import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    TemplateLoadError,
    load_template,
    save_template,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_load(template: Template, tmp_path: Path) -> Template:
    """Round-trip: save to a temp file, load and return."""
    p = tmp_path / "test.toml"
    save_template(template, p)
    return load_template(p)


def _minimal() -> Template:
    return Template(name="Minimal", description="test template")


# ---------------------------------------------------------------------------
# Round-trip: Template metadata
# ---------------------------------------------------------------------------


class TestTemplateMetadata:
    def test_name_survives(self, tmp_path: Path) -> None:
        t = Template(name="My Template")
        rt = _save_load(t, tmp_path)
        assert rt.name == "My Template"

    def test_role_survives(self, tmp_path: Path) -> None:
        for role in ("cq", "reply", "closing", "custom"):
            t = Template(name="t", role=role)
            rt = _save_load(t, tmp_path)
            assert rt.role == role

    def test_reference_frame_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", reference_frame=(640, 496))
        rt = _save_load(t, tmp_path)
        assert rt.reference_frame == (640, 496)

    def test_reference_frame_floats_are_rounded_with_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """L3: ``reference_frame = [320.7, 256.3]`` should round (not
        truncate) and surface a warning so the author notices the field
        is integer-only.

        Pre-fix the loader did ``int(320.7) → 320`` and ``int(256.3) → 256``
        silently — the rounding *direction* matters for percentage-based
        layer placement on the rendered canvas.
        """
        p = tmp_path / "f.toml"
        p.write_text(
            '[template]\nname = "t"\nreference_frame = [320.7, 256.3]\n'
        )
        with caplog.at_level("WARNING", logger="open_sstv.templates.toml_io"):
            rt = load_template(p)
        # 320.7 → 321 (round-half-to-even still rounds UP from .7), 256.3 → 256.
        assert rt.reference_frame == (321, 256)
        assert any(
            "reference_frame" in rec.message and "rounding" in rec.message
            for rec in caplog.records
        )

    def test_reference_frame_ints_do_not_warn(
        self, tmp_path: Path, caplog
    ) -> None:
        """The complementary path: clean integer input is silent."""
        p = tmp_path / "i.toml"
        p.write_text(
            '[template]\nname = "t"\nreference_frame = [320, 256]\n'
        )
        with caplog.at_level("WARNING", logger="open_sstv.templates.toml_io"):
            rt = load_template(p)
        assert rt.reference_frame == (320, 256)
        assert not any(
            "reference_frame" in rec.message for rec in caplog.records
        )

    def test_reference_frame_rejects_non_numeric(self, tmp_path: Path) -> None:
        p = tmp_path / "s.toml"
        p.write_text(
            '[template]\nname = "t"\nreference_frame = ["320", "256"]\n'
        )
        with pytest.raises(TemplateLoadError, match="reference_frame"):
            load_template(p)

    def test_reference_frame_rejects_wrong_arity(self, tmp_path: Path) -> None:
        p = tmp_path / "a.toml"
        p.write_text(
            '[template]\nname = "t"\nreference_frame = [320]\n'
        )
        with pytest.raises(TemplateLoadError, match="reference_frame"):
            load_template(p)

    def test_reference_frame_rejects_non_positive(self, tmp_path: Path) -> None:
        p = tmp_path / "z.toml"
        p.write_text(
            '[template]\nname = "t"\nreference_frame = [0, 256]\n'
        )
        with pytest.raises(TemplateLoadError, match="positive"):
            load_template(p)

    def test_schema_version_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", schema_version=1)
        rt = _save_load(t, tmp_path)
        assert rt.schema_version == 1

    def test_description_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", description="A nice template")
        rt = _save_load(t, tmp_path)
        assert rt.description == "A nice template"

    def test_empty_layers(self, tmp_path: Path) -> None:
        t = Template(name="t")
        rt = _save_load(t, tmp_path)
        assert rt.layers == []


# ---------------------------------------------------------------------------
# Round-trip: PhotoLayer
# ---------------------------------------------------------------------------


class TestPhotoLayerRoundTrip:
    def _layer(self, **kw) -> PhotoLayer:
        return PhotoLayer(id="p1", anchor="FILL", **kw)

    def test_basic(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(fit="cover")])
        rt = _save_load(t, tmp_path)
        assert len(rt.layers) == 1
        layer = rt.layers[0]
        assert isinstance(layer, PhotoLayer)
        assert layer.id == "p1"
        assert layer.fit == "cover"
        assert layer.anchor == "FILL"

    def test_contain_fit(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(fit="contain")])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].fit == "contain"


# ---------------------------------------------------------------------------
# Round-trip: RxImageLayer
# ---------------------------------------------------------------------------


class TestRxImageLayerRoundTrip:
    def test_basic(self, tmp_path: Path) -> None:
        layer = RxImageLayer(id="rx1", anchor="BL", width_pct=30.0, height_pct=25.0)
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert isinstance(out, RxImageLayer)
        assert out.width_pct == pytest.approx(30.0)
        assert out.height_pct == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Round-trip: StationImageLayer
# ---------------------------------------------------------------------------


class TestStationImageLayerRoundTrip:
    def test_path_survives(self, tmp_path: Path) -> None:
        layer = StationImageLayer(id="si1", anchor="TL", path="qsl_card.png")
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert isinstance(out, StationImageLayer)
        assert out.path == "qsl_card.png"

    def test_subdir_path_survives(self, tmp_path: Path) -> None:
        layer = StationImageLayer(id="si1", anchor="TL", path="cards/qsl.png")
        rt = _save_load(Template(name="t", layers=[layer]), tmp_path)
        assert rt.layers[0].path == "cards/qsl.png"


class TestStationImagePathTraversalRejected:
    """Regression for C1: malicious StationImageLayer paths must be rejected
    at TOML load time so they never reach ``PIL.Image.open``.
    """

    def _toml_with_path(self, path: str) -> bytes:
        # TOML literal strings (single quotes) don't interpret backslashes,
        # so we can pass Windows-style paths through unmodified.
        return (
            b'[template]\nname = "Test"\nschema_version = 1\n\n'
            b'[[layer]]\ntype = "station_image"\nid = "si"\nanchor = "TL"\n'
            b"path = '" + path.encode("utf-8") + b"'\n"
        )

    def test_absolute_unix_path_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "evil.toml"
        p.write_bytes(self._toml_with_path("/etc/passwd"))
        with pytest.raises(TemplateLoadError, match="absolute"):
            load_template(p)

    def test_absolute_windows_path_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "evil.toml"
        p.write_bytes(self._toml_with_path("C:/Windows/System32/config/SAM"))
        with pytest.raises(TemplateLoadError, match="absolute"):
            load_template(p)

    def test_dotdot_segment_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "evil.toml"
        p.write_bytes(self._toml_with_path("../../../etc/passwd"))
        with pytest.raises(TemplateLoadError, match=r"\.\."):
            load_template(p)

    def test_nested_dotdot_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "evil.toml"
        p.write_bytes(self._toml_with_path("subdir/../../../secret"))
        with pytest.raises(TemplateLoadError, match=r"\.\."):
            load_template(p)

    def test_backslash_dotdot_rejected(self, tmp_path: Path) -> None:
        # Windows-style separators should not bypass the .. check.
        p = tmp_path / "evil.toml"
        p.write_bytes(self._toml_with_path("..\\..\\etc\\passwd"))
        with pytest.raises(TemplateLoadError, match=r"\.\."):
            load_template(p)

    def test_safe_relative_path_ok(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.toml"
        p.write_bytes(self._toml_with_path("cards/my_qsl.png"))
        t = load_template(p)
        assert len(t.layers) == 1
        assert t.layers[0].path == "cards/my_qsl.png"


# ---------------------------------------------------------------------------
# Round-trip: TextLayer
# ---------------------------------------------------------------------------


class TestTextLayerRoundTrip:
    def _layer(self, **kw) -> TextLayer:
        defaults = dict(
            id="tx1",
            anchor="BC",
            text_raw="CQ de %c",
            font_family="DejaVu Sans Bold",
            font_size_pct=10.0,
            fill=(255, 255, 255, 255),
            slashed_zero=True,
        )
        defaults.update(kw)
        return TextLayer(**defaults)

    def test_text_raw_toml_key_is_text(self, tmp_path: Path) -> None:
        """Verify TOML key is 'text', not 'text_raw'."""
        p = tmp_path / "t.toml"
        t = Template(name="t", layers=[self._layer()])
        save_template(t, p)
        with p.open("rb") as f:
            raw = tomllib.load(f)
        assert "text" in raw["layer"][0]
        assert "text_raw" not in raw["layer"][0]

    def test_text_raw_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(text_raw="Hello %o")])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].text_raw == "Hello %o"

    def test_fill_rgba_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(fill=(255, 128, 0, 200))])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].fill == (255, 128, 0, 200)

    def test_stroke_survives(self, tmp_path: Path) -> None:
        stroke = StrokeSpec(color=(0, 0, 0, 255), width_px=3)
        t = Template(name="t", layers=[self._layer(stroke=stroke)])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert out.stroke is not None
        assert out.stroke.color == (0, 0, 0, 255)
        assert out.stroke.width_px == 3

    def test_shadow_survives(self, tmp_path: Path) -> None:
        shadow = ShadowSpec(offset_x=4.0, offset_y=4.0, color=(0, 0, 0, 180), blur_px=2.5)
        t = Template(name="t", layers=[self._layer(shadow=shadow)])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert out.shadow is not None
        assert out.shadow.offset_x == pytest.approx(4.0)
        assert out.shadow.blur_px == pytest.approx(2.5)
        assert out.shadow.color == (0, 0, 0, 180)

    def test_no_stroke_no_shadow(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(stroke=None, shadow=None)])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert out.stroke is None
        assert out.shadow is None

    def test_align_survives(self, tmp_path: Path) -> None:
        for align in ("left", "center", "right"):
            t = Template(name="t", layers=[self._layer(align=align)])
            rt = _save_load(t, tmp_path)
            assert rt.layers[0].align == align

    def test_stacked_orientation(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(orientation="stacked")])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].orientation == "stacked"

    def test_slashed_zero_false(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(slashed_zero=False)])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].slashed_zero is False

    def test_font_family_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(font_family="Press Start 2P")])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].font_family == "Press Start 2P"

    def test_font_size_pct_survives(self, tmp_path: Path) -> None:
        t = Template(name="t", layers=[self._layer(font_size_pct=22.5)])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].font_size_pct == pytest.approx(22.5)


# ---------------------------------------------------------------------------
# Round-trip: RectLayer
# ---------------------------------------------------------------------------


class TestRectLayerRoundTrip:
    def test_fill_survives(self, tmp_path: Path) -> None:
        layer = RectLayer(id="r1", anchor="TL", width_pct=100.0, height_pct=8.0,
                          fill=(0, 130, 0, 235))
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert isinstance(out, RectLayer)
        assert out.fill == (0, 130, 0, 235)


# ---------------------------------------------------------------------------
# Round-trip: GradientLayer
# ---------------------------------------------------------------------------


class TestGradientLayerRoundTrip:
    def test_colors_and_angle(self, tmp_path: Path) -> None:
        layer = GradientLayer(
            id="g1", anchor="BL",
            width_pct=100.0, height_pct=40.0,
            from_color=(0, 0, 0, 0),
            to_color=(0, 0, 0, 200),
            angle_deg=270.0,
        )
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert isinstance(out, GradientLayer)
        assert out.from_color == (0, 0, 0, 0)
        assert out.to_color == (0, 0, 0, 200)
        assert out.angle_deg == pytest.approx(270.0)


# ---------------------------------------------------------------------------
# Round-trip: PatternLayer
# ---------------------------------------------------------------------------


class TestPatternLayerRoundTrip:
    def test_pattern_fields(self, tmp_path: Path) -> None:
        layer = PatternLayer(
            id="pat1", anchor="FILL",
            pattern_id="dots",
            tint=(200, 200, 255, 100),
            cell_size_pct=4.0,
        )
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert isinstance(out, PatternLayer)
        assert out.pattern_id == "dots"
        assert out.tint == (200, 200, 255, 100)
        assert out.cell_size_pct == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# LayerBase fields
# ---------------------------------------------------------------------------


class TestLayerBaseFields:
    def test_offsets_survive(self, tmp_path: Path) -> None:
        layer = RectLayer(id="r", anchor="TR", offset_x_pct=2.5, offset_y_pct=3.0,
                          fill=(255, 0, 0, 255))
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert out.offset_x_pct == pytest.approx(2.5)
        assert out.offset_y_pct == pytest.approx(3.0)

    def test_size_pct_survives(self, tmp_path: Path) -> None:
        layer = RectLayer(id="r", anchor="TL", width_pct=50.0, height_pct=25.0,
                          fill=(0, 0, 0, 255))
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        out = rt.layers[0]
        assert out.width_pct == pytest.approx(50.0)
        assert out.height_pct == pytest.approx(25.0)

    def test_opacity_survives(self, tmp_path: Path) -> None:
        layer = RectLayer(id="r", anchor="FILL", fill=(255, 0, 0, 255), opacity=0.75)
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].opacity == pytest.approx(0.75)

    def test_visible_false_survives(self, tmp_path: Path) -> None:
        layer = RectLayer(id="r", anchor="FILL", fill=(0, 0, 0, 255), visible=False)
        t = Template(name="t", layers=[layer])
        rt = _save_load(t, tmp_path)
        assert rt.layers[0].visible is False

    def test_default_visible_not_emitted(self, tmp_path: Path) -> None:
        """Fields at default should be omitted from TOML to keep files clean."""
        layer = RectLayer(id="r", anchor="FILL", fill=(0, 0, 0, 255))  # visible=True default
        p = tmp_path / "t.toml"
        save_template(Template(name="t", layers=[layer]), p)
        with p.open("rb") as f:
            raw = tomllib.load(f)
        assert "visible" not in raw["layer"][0]

    def test_anchor_always_emitted(self, tmp_path: Path) -> None:
        """Anchor must always be in the TOML (critical for positioning)."""
        layer = RectLayer(id="r", anchor="BR", fill=(0, 0, 0, 255))
        p = tmp_path / "t.toml"
        save_template(Template(name="t", layers=[layer]), p)
        with p.open("rb") as f:
            raw = tomllib.load(f)
        assert raw["layer"][0]["anchor"] == "BR"


# ---------------------------------------------------------------------------
# RGBA short-list warning
# ---------------------------------------------------------------------------


def test_rgba_three_element_list_warns(tmp_path: Path, caplog) -> None:
    """A ``fill = [R, G, B]`` (no alpha) should load as opaque *and* warn.

    Backwards-compatible: alpha still defaults to 255 so old templates keep
    rendering, but the warning surfaces the omission so authors who forgot
    the channel notice before shipping a template.
    """
    p = tmp_path / "short_rgba.toml"
    p.write_text(
        '[template]\nname = "t"\n\n'
        '[[layer]]\ntype = "rect"\nid = "r"\nanchor = "FILL"\n'
        "fill = [255, 128, 0]\n"
    )
    with caplog.at_level("WARNING", logger="open_sstv.templates.toml_io"):
        tpl = load_template(p)
    assert tpl.layers[0].fill == (255, 128, 0, 255)
    assert any("only 3 elements" in rec.message for rec in caplog.records)


def test_rgba_four_element_list_does_not_warn(tmp_path: Path, caplog) -> None:
    """The complementary path: a complete RGBA list is silent."""
    p = tmp_path / "full_rgba.toml"
    p.write_text(
        '[template]\nname = "t"\n\n'
        '[[layer]]\ntype = "rect"\nid = "r"\nanchor = "FILL"\n'
        "fill = [255, 128, 0, 200]\n"
    )
    with caplog.at_level("WARNING", logger="open_sstv.templates.toml_io"):
        load_template(p)
    assert not any("elements" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Layer ordering
# ---------------------------------------------------------------------------


def test_layer_order_preserved(tmp_path: Path) -> None:
    layers = [
        RectLayer(id="bottom", anchor="FILL", fill=(255, 0, 0, 255)),
        PhotoLayer(id="mid", anchor="FILL"),
        TextLayer(id="top", anchor="BC", text_raw="hello", fill=(255, 255, 255, 255)),
    ]
    t = Template(name="t", layers=layers)
    rt = _save_load(t, tmp_path)
    assert [l.id for l in rt.layers] == ["bottom", "mid", "top"]


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_schema_version_current_ok(tmp_path: Path) -> None:
    t = Template(name="t", schema_version=CURRENT_SCHEMA_VERSION)
    rt = _save_load(t, tmp_path)
    assert rt.schema_version == CURRENT_SCHEMA_VERSION


def test_schema_version_too_new_raises(tmp_path: Path) -> None:
    t = Template(name="t", schema_version=CURRENT_SCHEMA_VERSION + 1)
    p = tmp_path / "future.toml"
    save_template(t, p)
    with pytest.raises(SchemaVersionError, match="schema_version"):
        load_template(p)


# ---------------------------------------------------------------------------
# Forward-compat: unknown fields / layer types
# ---------------------------------------------------------------------------


def test_unknown_field_ignored(tmp_path: Path) -> None:
    """A TOML file with an extra unknown key should load without error."""
    toml_content = b"""
[template]
name = "Test"
role = "cq"
reference_frame = [320, 256]
schema_version = 1
description = ""
future_field = "ignored"

[[layer]]
type = "rect"
id = "r"
anchor = "FILL"
fill = [255, 0, 0, 255]
future_layer_field = "also ignored"
"""
    p = tmp_path / "unknown_fields.toml"
    p.write_bytes(toml_content)
    t = load_template(p)
    assert t.name == "Test"
    assert len(t.layers) == 1


def test_unknown_layer_type_skipped(tmp_path: Path) -> None:
    """An unknown layer type must be silently skipped, not crash the loader."""
    toml_content = b"""
[template]
name = "Test"
schema_version = 1

[[layer]]
type = "hologram"
id = "future_layer"
anchor = "C"

[[layer]]
type = "rect"
id = "known"
anchor = "FILL"
fill = [0, 255, 0, 255]
"""
    p = tmp_path / "unknown_type.toml"
    p.write_bytes(toml_content)
    t = load_template(p)
    assert len(t.layers) == 1
    assert t.layers[0].id == "known"


def test_layer_missing_id_skipped(tmp_path: Path) -> None:
    """A layer without the required 'id' field should be skipped, not crash."""
    toml_content = b"""
[template]
name = "Test"
schema_version = 1

[[layer]]
type = "rect"
anchor = "FILL"
fill = [255, 0, 0, 255]

[[layer]]
type = "rect"
id = "ok"
anchor = "FILL"
fill = [0, 255, 0, 255]
"""
    p = tmp_path / "missing_id.toml"
    p.write_bytes(toml_content)
    t = load_template(p)
    assert len(t.layers) == 1
    assert t.layers[0].id == "ok"


def test_missing_name_raises(tmp_path: Path) -> None:
    """A template missing the 'name' field should raise TemplateLoadError."""
    toml_content = b"""
[template]
schema_version = 1
"""
    p = tmp_path / "no_name.toml"
    p.write_bytes(toml_content)
    with pytest.raises(TemplateLoadError):
        load_template(p)


# ---------------------------------------------------------------------------
# Multi-layer template
# ---------------------------------------------------------------------------


def test_full_cqsstv_template_round_trips(tmp_path: Path) -> None:
    """A realistic multi-layer template survives a round-trip."""
    from open_sstv.templates.model import PhotoLayer
    t = Template(
        name="CQSSTV",
        role="cq",
        description="Test CQ",
        layers=[
            PhotoLayer(id="base", anchor="FILL", fit="cover"),
            RectLayer(id="banner", anchor="TL", width_pct=100.0, height_pct=9.0,
                      fill=(0, 130, 0, 235)),
            TextLayer(id="call", anchor="TL", offset_x_pct=1.0, offset_y_pct=1.0,
                      text_raw="%c", font_family="DejaVu Sans Bold", font_size_pct=6.5,
                      fill=(255, 255, 255, 255), slashed_zero=True),
            TextLayer(id="cq", anchor="TL", offset_x_pct=3.0, offset_y_pct=12.0,
                      text_raw="CQSSTV", font_family="DejaVu Sans Bold", font_size_pct=28.0,
                      fill=(255, 20, 20, 255),
                      stroke=StrokeSpec(color=(255, 255, 255, 255), width_px=4),
                      slashed_zero=False),
        ],
    )
    rt = _save_load(t, tmp_path)
    assert rt.name == "CQSSTV"
    assert len(rt.layers) == 4
    assert rt.layers[2].text_raw == "%c"
    assert rt.layers[3].stroke is not None
    assert rt.layers[3].stroke.width_px == 4


# ---------------------------------------------------------------------------
# Bundled starter templates load correctly
# ---------------------------------------------------------------------------


class TestBundledStarterTemplates:
    """All 8 bundled starter templates must load without error."""

    import importlib.resources as _ir

    def _bundled_dir(self) -> Path:
        import importlib.resources
        anchor = importlib.resources.files("open_sstv") / "assets" / "templates"
        with importlib.resources.as_file(anchor) as p:
            return Path(p)

    @pytest.mark.parametrize("filename", [
        "cqsstv.toml",
        "cq_de_call.toml",
        "reply_exchange.toml",
        "reply_simple.toml",
        "seventy_three.toml",
        "cqsstv_vertical.toml",
        "seventy_three_card.toml",
        "seventy_three_vertical.toml",
    ])
    def test_bundled_template_loads(self, filename: str) -> None:
        path = self._bundled_dir() / filename
        assert path.exists(), f"Bundled template missing: {filename}"
        t = load_template(path)
        assert t.name
        assert t.role in ("cq", "reply", "closing", "custom")
        assert t.schema_version == 1
        assert isinstance(t.layers, list)
        assert len(t.layers) > 0

    def test_all_starters_have_photo_base(self) -> None:
        """Every starter template should have a PhotoLayer as its first layer."""
        bdir = self._bundled_dir()
        for filename in [
            "cqsstv.toml", "cq_de_call.toml", "reply_exchange.toml",
            "reply_simple.toml", "seventy_three.toml",
        ]:
            t = load_template(bdir / filename)
            assert any(isinstance(l, PhotoLayer) for l in t.layers), \
                f"{filename} should have a PhotoLayer"
