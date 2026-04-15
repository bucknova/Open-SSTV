# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the five new SSTV modes added in v0.1.21.

Martin M3/M4, Scottie S3/S4, PD-50.  Each is a timing-variant of an
existing family; the suite verifies four properties per mode:

1. VIS round-trip — ``mode_from_vis(spec.vis_code) == mode``
2. Encoder mapping — ``_PYSSTV_CLASSES[mode]`` is present
3. Decoder dispatch — ``_PIXEL_DECODERS[mode]`` is present
4. Encode→decode dimension check — decoded image has the expected pixel size
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.decoder import _PIXEL_DECODERS, decode_wav
from open_sstv.core.encoder import _PYSSTV_CLASSES, encode
from open_sstv.core.modes import MODE_TABLE, Mode, mode_from_vis

# ---------------------------------------------------------------------------
# Parametrize over all five new modes
# ---------------------------------------------------------------------------

_NEW_MODES = [
    Mode.MARTIN_M3,
    Mode.MARTIN_M4,
    Mode.SCOTTIE_S3,
    Mode.SCOTTIE_S4,
    Mode.PD_50,
]

# Expected decoded image dimensions (width × height).
# PD family decodes to spec.width × (spec.height * 2); Martin/Scottie to
# spec.width × spec.height.
_EXPECTED_DECODED_SIZE: dict[Mode, tuple[int, int]] = {
    Mode.MARTIN_M3:  (320, 128),
    Mode.MARTIN_M4:  (160, 128),
    Mode.SCOTTIE_S3: (320, 128),
    Mode.SCOTTIE_S4: (160, 128),
    Mode.PD_50:      (320, 256),   # spec.height=128 super-lines → 256 image rows
}


# ---------------------------------------------------------------------------
# 1. VIS round-trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _NEW_MODES)
def test_vis_round_trip(mode: Mode) -> None:
    """mode_from_vis must find each new mode by its VIS code."""
    spec = MODE_TABLE[mode]
    found = mode_from_vis(spec.vis_code)
    assert found == mode, (
        f"{mode.value}: mode_from_vis(0x{spec.vis_code:02X}) returned {found!r}"
    )


# ---------------------------------------------------------------------------
# 2. Encoder mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _NEW_MODES)
def test_encoder_mapping_present(mode: Mode) -> None:
    """_PYSSTV_CLASSES must have an entry for every new mode."""
    assert mode in _PYSSTV_CLASSES, (
        f"{mode.value} missing from _PYSSTV_CLASSES in encoder.py"
    )


# ---------------------------------------------------------------------------
# 3. Decoder dispatch mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _NEW_MODES)
def test_decoder_dispatch_present(mode: Mode) -> None:
    """_PIXEL_DECODERS must have an entry for every new mode."""
    assert mode in _PIXEL_DECODERS, (
        f"{mode.value} missing from _PIXEL_DECODERS in decoder.py"
    )


# ---------------------------------------------------------------------------
# 4. Mode spec sanity — durations and dimensions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", _NEW_MODES)
def test_mode_spec_duration_positive(mode: Mode) -> None:
    spec = MODE_TABLE[mode]
    assert spec.total_duration_s > 0
    assert spec.width > 0
    assert spec.height > 0


def test_martin_m3_matches_m1_timing() -> None:
    """M3 line_time must equal M1 (same scan rate, different height)."""
    assert MODE_TABLE[Mode.MARTIN_M3].line_time_ms == pytest.approx(
        MODE_TABLE[Mode.MARTIN_M1].line_time_ms, rel=1e-6
    )


def test_martin_m4_matches_m2_timing() -> None:
    assert MODE_TABLE[Mode.MARTIN_M4].line_time_ms == pytest.approx(
        MODE_TABLE[Mode.MARTIN_M2].line_time_ms, rel=1e-6
    )


def test_scottie_s3_matches_s1_timing() -> None:
    assert MODE_TABLE[Mode.SCOTTIE_S3].line_time_ms == pytest.approx(
        MODE_TABLE[Mode.SCOTTIE_S1].line_time_ms, rel=1e-6
    )


def test_scottie_s4_matches_s2_timing() -> None:
    assert MODE_TABLE[Mode.SCOTTIE_S4].line_time_ms == pytest.approx(
        MODE_TABLE[Mode.SCOTTIE_S2].line_time_ms, rel=1e-6
    )


def test_pd50_line_time_is_half_of_pd90() -> None:
    """PD-50 pixel time (0.286 ms) is roughly half of PD-90 (0.532 ms),
    so its channel scan and line_time should be roughly half as long."""
    pd50_lt = MODE_TABLE[Mode.PD_50].line_time_ms
    pd90_lt = MODE_TABLE[Mode.PD_90].line_time_ms
    ratio = pd50_lt / pd90_lt
    assert 0.50 < ratio < 0.60, (
        f"PD-50 / PD-90 line_time ratio {ratio:.3f} outside expected 0.50–0.60 band"
    )


# ---------------------------------------------------------------------------
# 5. Encode → decode round-trip (dimensions)
#
# Martin M4 (~29 s) and Scottie S4 (~36 s) are the shortest new modes;
# full round-trips for M3/S3/PD-50 would each take 50–57 s of audio and
# are covered by the encoder's duration tests + the family decoders'
# existing round-trips.  We run all five here but mark M3/S3/PD-50 as
# slow so they can be skipped in time-constrained CI with -m "not slow".
# ---------------------------------------------------------------------------

def _make_test_image(width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height))
    px = img.load()
    assert px is not None
    for x in range(width):
        for y in range(height):
            px[x, y] = (x * 255 // max(width - 1, 1),
                        y * 255 // max(height - 1, 1), 128)
    return img


def _round_trip(mode: Mode) -> Image.Image | None:
    fs = 48_000
    spec = MODE_TABLE[mode]
    # Use the PySSTV class's own dimensions (PD stores half-height in spec)
    cls = _PYSSTV_CLASSES[mode]
    img = _make_test_image(cls.WIDTH, cls.HEIGHT)
    samples = encode(img, mode, sample_rate=fs).astype(np.float64) / 32768.0
    return decode_wav(samples, fs)


@pytest.mark.parametrize("mode", [Mode.MARTIN_M4, Mode.SCOTTIE_S4])
def test_round_trip_dimensions(mode: Mode) -> None:
    """Encode then decode; assert decoded image has the expected dimensions."""
    result = _round_trip(mode)
    assert result is not None, f"{mode.value} round-trip returned None"
    assert result.mode == mode, f"Expected {mode.value}, got {result.mode!r}"
    expected_size = _EXPECTED_DECODED_SIZE[mode]
    assert result.image.size == expected_size, (
        f"{mode.value}: decoded size {result.image.size} != expected {expected_size}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("mode", [Mode.MARTIN_M3, Mode.SCOTTIE_S3, Mode.PD_50])
def test_round_trip_dimensions_slow(mode: Mode) -> None:
    """Same check for the longer new modes (50–57 s audio; marked slow)."""
    result = _round_trip(mode)
    assert result is not None, f"{mode.value} round-trip returned None"
    assert result.mode == mode
    expected_size = _EXPECTED_DECODED_SIZE[mode]
    assert result.image.size == expected_size, (
        f"{mode.value}: decoded size {result.image.size} != expected {expected_size}"
    )
