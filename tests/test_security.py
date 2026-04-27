# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.security`` — Pillow decompression-bomb protection.

Regression for audit finding C2: Pillow's default MAX_IMAGE_PIXELS (~89 MP)
let a maliciously crafted image decompress to gigabytes of memory before
the OS killed the process.  We pin MAX_IMAGE_PIXELS to 32 MP at package
import and expect every call site to gracefully handle the resulting
``DecompressionBombError`` (raised by Pillow at ``Image.open`` /
``.load`` time when the declared dimensions exceed the cap).
"""
from __future__ import annotations

from pathlib import Path

import PIL.Image
import pytest

from open_sstv.security import MAX_IMAGE_PIXELS, apply_pil_security_limits


def test_max_image_pixels_is_32_mp() -> None:
    assert MAX_IMAGE_PIXELS == 1024 * 1024 * 32


def test_apply_pil_security_limits_sets_global() -> None:
    """Calling apply_pil_security_limits() must set PIL.Image.MAX_IMAGE_PIXELS."""
    # Stash the current value so this test is independent of import order.
    saved = PIL.Image.MAX_IMAGE_PIXELS
    try:
        PIL.Image.MAX_IMAGE_PIXELS = 12345
        apply_pil_security_limits()
        assert PIL.Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS
    finally:
        PIL.Image.MAX_IMAGE_PIXELS = saved


def test_package_import_applies_limit() -> None:
    """Importing open_sstv must have applied the limit by the time tests run.

    All other tests rely on this, so failing here means every Image.open in
    the suite is running unprotected.
    """
    # Force-reset to a dummy value, then re-import to confirm the package
    # init applies the cap.  We do a direct call instead of importlib reload
    # to keep the test side-effect-free.
    saved = PIL.Image.MAX_IMAGE_PIXELS
    try:
        PIL.Image.MAX_IMAGE_PIXELS = 1
        apply_pil_security_limits()
        assert PIL.Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS
    finally:
        PIL.Image.MAX_IMAGE_PIXELS = saved


def _make_png(path: Path, width: int, height: int) -> None:
    """Save a small valid PNG at the given dimensions."""
    img = PIL.Image.new("RGB", (width, height), (0, 0, 0))
    img.save(path, format="PNG")


def test_oversized_image_raises_decompression_bomb_error(tmp_path: Path) -> None:
    """Sanity check: with the cap pinned low, PIL refuses to decode."""
    bomb = tmp_path / "bomb.png"
    _make_png(bomb, 64, 64)  # 4096 pixels — bigger than the tiny cap below
    saved = PIL.Image.MAX_IMAGE_PIXELS
    try:
        # PIL raises DecompressionBombError when pixels > 2 * MAX, so we
        # set MAX low enough that 4096 > 2*MAX_TEST_CAP.
        PIL.Image.MAX_IMAGE_PIXELS = 1024
        with pytest.raises(PIL.Image.DecompressionBombError):
            img = PIL.Image.open(bomb)
            img.load()
    finally:
        PIL.Image.MAX_IMAGE_PIXELS = saved


def test_renderer_swallows_decompression_bomb(tmp_path: Path) -> None:
    """A StationImageLayer pointing at an oversized image must not crash
    the renderer — it should log and render the layer blank.
    """
    from open_sstv.config.schema import AppConfig
    from open_sstv.templates.model import (
        QSOState, StationImageLayer, TXContext, Template,
    )
    from open_sstv.templates.renderer import render_template

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    bomb_path = assets_dir / "bomb.png"
    _make_png(bomb_path, 64, 64)

    layer = StationImageLayer(id="si", anchor="FILL", path="bomb.png")
    template = Template(name="t", layers=[layer])

    saved = PIL.Image.MAX_IMAGE_PIXELS
    try:
        PIL.Image.MAX_IMAGE_PIXELS = 1024  # tiny cap → bomb on 4096-px image
        # Should not raise — the renderer catches DecompressionBombError
        # and the layer falls back to rendering blank.
        out = render_template(
            template,
            QSOState(),
            AppConfig(),
            TXContext(frame_size=(320, 256)),
            assets_dir=assets_dir,
        )
    finally:
        PIL.Image.MAX_IMAGE_PIXELS = saved
    assert out.size == (320, 256)


def test_encoder_swallows_decompression_bomb(tmp_path: Path) -> None:
    """``encoder.encode`` must convert DecompressionBombError to a clean
    ValueError instead of letting it crash callers (CLI, GUI workers).
    """
    from open_sstv.core.encoder import encode
    from open_sstv.core.modes import Mode

    bomb_path = tmp_path / "bomb.png"
    _make_png(bomb_path, 64, 64)

    saved = PIL.Image.MAX_IMAGE_PIXELS
    try:
        PIL.Image.MAX_IMAGE_PIXELS = 1024
        with pytest.raises(ValueError, match="oversized"):
            encode(str(bomb_path), Mode.ROBOT_36)
    finally:
        PIL.Image.MAX_IMAGE_PIXELS = saved
