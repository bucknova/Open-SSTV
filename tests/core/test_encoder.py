# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.encoder``.

These run without any audio hardware: the encoder is pure-Python and returns
a NumPy array. We test against a tiny synthetic gradient image (no on-disk
fixtures) so the suite stays fast and deterministic.

Acceptance bound on duration: encoded length must be within 5% of the
mode's body duration *plus* the ~600 ms VIS leader. PySSTV always emits
the leader, so a strict equality check would fail.
"""
from __future__ import annotations

import pytest
from PIL import Image

import numpy as np

from open_sstv.core.encoder import DEFAULT_SAMPLE_RATE, encode
from open_sstv.core.modes import MODE_TABLE, Mode

# 5% tolerance is comfortably above the VIS-leader overhead (~0.9 s) on the
# longest mode (Martin M1, ~114 s body) where 0.9/114 ≈ 0.8%.
DURATION_TOLERANCE = 0.05


@pytest.fixture(scope="module")
def gradient_image() -> Image.Image:
    """A 200x200 RGB gradient that exercises the resize-to-mode path.

    Deliberately *not* a mode-native size so every test verifies the
    facade's Pillow resize step is wired up.
    """
    img = Image.new("RGB", (200, 200))
    pixels = img.load()
    assert pixels is not None
    for y in range(200):
        for x in range(200):
            pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
    return img


@pytest.mark.parametrize("mode", list(Mode))
def test_encode_returns_int16_mono(gradient_image: Image.Image, mode: Mode) -> None:
    samples = encode(gradient_image, mode)

    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.int16
    assert samples.ndim == 1
    assert samples.size > 0


@pytest.mark.parametrize("mode", list(Mode))
def test_encode_duration_within_tolerance(
    gradient_image: Image.Image, mode: Mode
) -> None:
    samples = encode(gradient_image, mode)
    actual_s = samples.size / DEFAULT_SAMPLE_RATE
    expected_s = MODE_TABLE[mode].total_duration_s

    # Encoded length includes the VIS leader, so it can only run *longer*
    # than the body-only duration; never shorter.
    assert actual_s >= expected_s, (
        f"{mode.value}: encoded {actual_s:.3f}s shorter than body {expected_s:.3f}s"
    )
    assert actual_s <= expected_s * (1 + DURATION_TOLERANCE), (
        f"{mode.value}: encoded {actual_s:.3f}s exceeds {DURATION_TOLERANCE * 100:.0f}% "
        f"slop above body {expected_s:.3f}s"
    )


@pytest.mark.parametrize("mode", list(Mode))
def test_encode_uses_full_int16_range(
    gradient_image: Image.Image, mode: Mode
) -> None:
    """A real SSTV waveform sweeps 1500–2300 Hz at full amplitude — the
    quantizer should be using close to the full int16 range. If something
    is silently halving the amplitude (e.g. wrong ``bits`` arg) the peak
    will collapse and this test will catch it."""
    samples = encode(gradient_image, mode)
    peak = int(np.max(np.abs(samples)))
    assert peak > 30_000, (
        f"{mode.value}: peak amplitude {peak} is suspiciously low — encoder "
        f"may be quantizing to the wrong bit depth"
    )


def test_encode_accepts_path(tmp_path, gradient_image: Image.Image) -> None:
    png_path = tmp_path / "fixture.png"
    gradient_image.save(png_path)

    samples = encode(png_path, Mode.ROBOT_36)

    assert samples.size > 0
    assert samples.dtype == np.int16


def test_encode_resizes_non_native_image(gradient_image: Image.Image) -> None:
    """The 200x200 fixture is not Robot36's 320x240 native size; the
    facade must resize before handing the image to PySSTV. If the resize
    is missing PySSTV's ``getpixel`` will fall off the right edge of the
    image and either crash or wrap into garbage."""
    samples = encode(gradient_image, Mode.ROBOT_36)
    assert samples.size > 0


def test_encode_rejects_unknown_mode(gradient_image: Image.Image) -> None:
    with pytest.raises(ValueError, match="Unsupported SSTV mode"):
        encode(gradient_image, "not_a_mode")  # type: ignore[arg-type]


def test_encode_accepts_rgba_image() -> None:
    """RGBA / palette / grayscale inputs all need to flow through the
    facade — `_prepare_image` converts to RGB up front."""
    rgba = Image.new("RGBA", (50, 50), (10, 20, 30, 128))
    samples = encode(rgba, Mode.ROBOT_36)
    assert samples.size > 0


def test_encode_accepts_grayscale_image() -> None:
    gray = Image.new("L", (50, 50), 128)
    samples = encode(gray, Mode.ROBOT_36)
    assert samples.size > 0
