# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the shared TXContext builder (v0.6.14 audit C-M3 / C-M4).

TX, the template editor, the gallery thumbnails and the remote compose
path all build their render context through ``build_tx_context`` so the
preview is pixel-identical to what goes on the air, and the rig
frequency reaches the ``%f`` / ``%b`` tokens.
"""
from __future__ import annotations

import datetime

from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.templates.context import build_tx_context, needs_photo_precrop
from open_sstv.templates.model import (
    PhotoLayer,
    QSOState,
    Template,
    TextLayer,
)
from open_sstv.templates.renderer import render_template

_NOW = datetime.datetime(2026, 10, 13, 12, 0, tzinfo=datetime.UTC)


def _tpl(*layers) -> Template:
    return Template(name="t", layers=list(layers))


def test_fill_cover_photo_is_precropped_to_frame() -> None:
    tpl = _tpl(PhotoLayer(id="p", anchor="FILL", fit="cover"))
    assert needs_photo_precrop(tpl)
    photo = Image.new("RGB", (640, 480))
    ctx = build_tx_context(tpl, Mode.SCOTTIE_S1, photo)
    spec = MODE_TABLE[Mode.SCOTTIE_S1]
    assert ctx.photo_image.size == (spec.width, spec.display_height)
    assert ctx.frame_size == (spec.width, spec.display_height)


def test_contain_photo_is_not_precropped() -> None:
    """Pre-cropping a ``contain`` photo to the frame turned the author's
    letterbox into a full-frame crop on air while every preview showed
    the letterbox.  The renderer's own fit must decide."""
    tpl = _tpl(PhotoLayer(id="p", anchor="FILL", fit="contain"))
    assert not needs_photo_precrop(tpl)
    photo = Image.new("RGB", (640, 480))
    ctx = build_tx_context(tpl, Mode.SCOTTIE_S1, photo)
    assert ctx.photo_image is photo


def test_sized_photo_layer_is_not_precropped() -> None:
    tpl = _tpl(PhotoLayer(id="p", anchor="TL", fit="cover", width_pct=50.0))
    assert not needs_photo_precrop(tpl)


def test_precrop_is_render_neutral_for_fill_cover() -> None:
    """For the case we still pre-crop, the result must match rendering the
    raw photo — the pre-crop is a pure downscale, never a semantic change."""
    tpl = _tpl(PhotoLayer(id="p", anchor="FILL", fit="cover"))
    photo = Image.new("RGB", (600, 300), (255, 0, 0))
    for x in range(200, 400):
        for y in range(300):
            photo.putpixel((x, y), (0, 255, 0))
    cfg = AppConfig()
    pre = render_template(tpl, QSOState(), cfg, build_tx_context(tpl, Mode.SCOTTIE_S1, photo))
    spec = MODE_TABLE[Mode.SCOTTIE_S1]
    from open_sstv.templates.model import TXContext

    raw = render_template(
        tpl, QSOState(), cfg,
        TXContext(frame_size=(spec.width, spec.display_height), photo_image=photo),
    )
    assert pre.size == raw.size
    # Same centre content (green band) either way.
    c = (pre.width // 2, pre.height // 2)
    assert pre.getpixel(c)[1] > 200 and raw.getpixel(c)[1] > 200


def test_frequency_reaches_freq_and_band_tokens() -> None:
    tpl = _tpl(TextLayer(id="t", text_raw="%f %b {freq} {band}"))
    ctx = build_tx_context(tpl, Mode.MARTIN_M1, None, frequency_hz=14_230_000)
    assert ctx.frequency_hz == 14_230_000
    from open_sstv.templates.tokens import resolve_text

    out = resolve_text(
        "%f %b {band}", QSOState(), AppConfig(), ctx, now_utc=_NOW
    )
    assert out == "14.2300 MHz 20m 20m"


def test_none_template_still_builds_context() -> None:
    ctx = build_tx_context(None, Mode.ROBOT_36, None)
    spec = MODE_TABLE[Mode.ROBOT_36]
    assert ctx.frame_size == (spec.width, spec.display_height)
    assert ctx.frequency_hz is None
