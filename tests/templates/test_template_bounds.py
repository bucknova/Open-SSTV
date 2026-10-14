# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.6.14 audit S-M5: template TOML → unbounded ``Image.new``.

A shared template with ``shadow = { offset_x = 4000, … }`` made the text
rasteriser allocate ~1.9 GB for a 320×256 frame.  The loader now clamps
every numeric field and rejects NaN/inf; the renderer refuses any
intermediate image beyond 4× the canvas and skips just that layer.
"""
from __future__ import annotations

import tracemalloc
from pathlib import Path

import PIL.Image

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import (
    PatternLayer,
    PhotoLayer,
    QSOState,
    RectLayer,
    ShadowSpec,
    Template,
    TextLayer,
    TXContext,
)
from open_sstv.templates.renderer import render_template
from open_sstv.templates.toml_io import load_template

_HOSTILE = """
[template]
name = "hostile"

[[layer]]
type = "text"
id = "t"
text = "W0AEZ"
font_size_pct = 1e9
line_height_mult = nan
stroke = { color = [0, 0, 0, 255], width_px = 100000 }
shadow = { offset_x = 4000.0, offset_y = -inf, color = [0, 0, 0, 255], blur_px = 1e6 }

[[layer]]
type = "gradient"
id = "g"
angle_deg = 1e300

[[layer]]
type = "pattern"
id = "p"
cell_size_pct = 0.0

[[layer]]
type = "rect"
id = "r"
opacity = nan
width_pct = inf
"""


def test_hostile_numbers_are_clamped_on_load(tmp_path: Path) -> None:
    p = tmp_path / "hostile.toml"
    p.write_text(_HOSTILE)
    t = load_template(p)
    text, grad, pat, rect = t.layers
    assert isinstance(text, TextLayer)
    assert text.font_size_pct == 100.0
    assert text.line_height_mult == 1.0  # NaN → default
    assert text.stroke.width_px == 32
    assert text.shadow.offset_x == 64.0
    assert text.shadow.offset_y == 0.0  # -inf → default
    assert text.shadow.blur_px == 32.0
    assert grad.angle_deg == 360.0
    assert pat.cell_size_pct == 0.1
    assert rect.opacity == 1.0  # NaN → default, not the upper bound
    assert rect.width_pct is None  # inf → default


def test_hostile_template_renders_in_bounded_memory(tmp_path: Path) -> None:
    p = tmp_path / "hostile.toml"
    p.write_text(_HOSTILE)
    t = load_template(p)
    tracemalloc.start()
    try:
        img = render_template(
            t, QSOState(), AppConfig(callsign="W0AEZ"), TXContext(frame_size=(320, 256))
        )
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert img.size == (320, 256)
    assert peak < 200 * 1024 * 1024


def test_renderer_skips_oversize_layer_but_keeps_the_rest() -> None:
    """An in-memory template (no loader clamps) with a huge shadow offset
    trips the renderer guard; that layer is skipped, others still render."""
    tpl = Template(
        name="t",
        layers=[
            RectLayer(id="bg", anchor="FILL", fill=(255, 0, 0, 255)),
            TextLayer(
                id="t", text_raw="X",
                shadow=ShadowSpec(offset_x=4000.0, offset_y=4000.0, color=(0, 0, 0, 255)),
            ),
        ],
    )
    img = render_template(tpl, QSOState(), AppConfig(), TXContext(frame_size=(320, 256)))
    assert img.getpixel((5, 5)) == (255, 0, 0)


def test_tall_photo_cover_into_wide_box_is_refused() -> None:
    photo = PIL.Image.new("RGB", (1, 20000))
    tpl = Template(
        name="t",
        layers=[
            RectLayer(id="bg", anchor="FILL", fill=(0, 0, 255, 255)),
            PhotoLayer(id="p", anchor="FILL", fit="cover"),
        ],
    )
    img = render_template(
        tpl, QSOState(), AppConfig(), TXContext(frame_size=(320, 256), photo_image=photo)
    )
    assert img.getpixel((5, 5)) == (0, 0, 255)


def test_normal_pattern_still_renders() -> None:
    tpl = Template(name="t", layers=[PatternLayer(id="p", anchor="FILL")])
    img = render_template(tpl, QSOState(), AppConfig(), TXContext(frame_size=(320, 256)))
    assert img.size == (320, 256)
