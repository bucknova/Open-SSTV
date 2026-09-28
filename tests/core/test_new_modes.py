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
    Mode.MARTIN_M4:  (320, 128),
    Mode.SCOTTIE_S3: (320, 128),
    Mode.SCOTTIE_S4: (320, 128),
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
    # Use the PySSTV class's own dimensions (PD stores half-height in spec).
    # We read the mode spec implicitly via the encoder; no local binding needed.
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


# ---------------------------------------------------------------------------
# display_height — user-facing pixel height vs. sync-pulse-count height
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected_display_height",
    [
        # PD modes: display_height == 2 × spec.height
        (Mode.PD_50, 256),
        (Mode.PD_90, 256),
        (Mode.PD_120, 496),
        (Mode.PD_160, 400),
        (Mode.PD_180, 496),
        (Mode.PD_240, 496),
        (Mode.PD_290, 616),
        # Non-PD modes: display_height == spec.height
        (Mode.ROBOT_36, 240),
        (Mode.MARTIN_M1, 256),
        (Mode.MARTIN_M2, 256),
        (Mode.MARTIN_M3, 128),
        (Mode.MARTIN_M4, 128),
        (Mode.SCOTTIE_S1, 256),
        (Mode.SCOTTIE_S2, 256),
        (Mode.SCOTTIE_S3, 128),
        (Mode.SCOTTIE_S4, 128),
        (Mode.SCOTTIE_DX, 256),
        (Mode.WRAASE_SC2_120, 256),
        (Mode.WRAASE_SC2_180, 256),
        (Mode.PASOKON_P3, 496),
        (Mode.PASOKON_P5, 496),
        (Mode.PASOKON_P7, 496),
    ],
)
def test_display_height(mode: Mode, expected_display_height: int) -> None:
    """``ModeSpec.display_height`` returns the actual pixel height for
    user-facing dimensions.

    Regression guard for the PD autocrop bug: the image editor was using
    ``spec.height`` (sync-pulse count = half the image height) to set the
    crop target, offering e.g. 320×128 for PD-50 instead of 320×256.
    """
    spec = MODE_TABLE[mode]
    assert spec.display_height == expected_display_height, (
        f"{mode.value}: display_height={spec.display_height}, "
        f"expected {expected_display_height}"
    )


# ---------------------------------------------------------------------------
# 6. Frame width — the issue #65 guards
# ---------------------------------------------------------------------------
# Martin M2/M4 and Scottie S2/S4 shipped as 160 px wide from v0.1.x through
# v0.6.10.  They are not: those modes halve the *pixel dwell time*, not the
# pixel count.  M2 runs 320 columns at 0.2288 ms each (73.216 ms per channel)
# against M1's 320 at 0.4576 ms; S2 runs 320 at 0.2752 ms.  Cross-checked
# against slowrx ``modespec.c`` (M2/M4/S2 all ImgWidth 320) and the published
# mode tables (Martin 2: 320×256 58 s, Scottie 2: 320×256 71 s).
#
# The 160 came from upstream PySSTV, which sets ``WIDTH = 160`` on MartinM2
# and ScottieS2.  ``core.encoder`` now overrides it.  The consequence of the
# old value was that every landscape picture was squeezed into a 160×256
# portrait before transmission.

#: Modes whose declared width was wrong before the issue #65 fix.
_HALF_CLOCK_MODES = [
    Mode.MARTIN_M2,
    Mode.MARTIN_M4,
    Mode.SCOTTIE_S2,
    Mode.SCOTTIE_S4,
]


@pytest.mark.parametrize("mode", _HALF_CLOCK_MODES)
def test_half_pixel_clock_modes_are_320_wide(mode: Mode) -> None:
    """M2 / M4 / S2 / S4 are 320 px wide, not 160 (issue #65)."""
    assert MODE_TABLE[mode].width == 320, (
        f"{mode.value}: width={MODE_TABLE[mode].width}. These modes halve the "
        "pixel clock, not the pixel count — 160 squeezes every landscape "
        "picture into a portrait frame."
    )


@pytest.mark.parametrize("mode", sorted(MODE_TABLE, key=lambda m: m.value))
def test_spec_dimensions_match_the_encoder_class(mode: Mode) -> None:
    """``MODE_TABLE`` and the PySSTV encoder class must agree on the frame.

    The anti-drift guard for issue #65.  ``encode`` sizes the outgoing image
    from ``sstv_cls.WIDTH``/``HEIGHT`` while every decoder sizes the incoming
    one from ``spec.width``/``display_height``.  When those disagree, TX and
    RX quietly use different frames and nothing in the suite notices — which
    is exactly how the 160 px M2 survived eight minor releases.

    PD modes compare against ``display_height`` because ``spec.height``
    stores the sync-pulse count (half the image rows).
    """
    spec = MODE_TABLE[mode]
    cls = _PYSSTV_CLASSES[mode]
    assert (spec.width, spec.display_height) == (cls.WIDTH, cls.HEIGHT), (
        f"{mode.value}: MODE_TABLE says {spec.width}×{spec.display_height}, "
        f"encoder class {cls.__name__} says {cls.WIDTH}×{cls.HEIGHT}"
    )


@pytest.mark.parametrize(
    ("mode", "expected_scan_ms"),
    [
        # 320 columns × the pixel dwell slowrx lists for each mode.
        (Mode.MARTIN_M1, 320 * 0.4576),
        (Mode.MARTIN_M2, 320 * 0.2288),
        (Mode.MARTIN_M3, 320 * 0.4576),
        (Mode.MARTIN_M4, 320 * 0.2288),
    ],
)
def test_martin_channel_scan_matches_pixel_dwell(
    mode: Mode, expected_scan_ms: float
) -> None:
    """Widening the frame must not have moved the on-air line timing.

    The scan time the decoder derives from ``line_time_ms`` has to stay
    equal to ``320 × dwell``.  If a future edit "fixes" a width by
    rescaling SCAN instead, this fails and the mode stops decoding
    anywhere else.
    """
    spec = MODE_TABLE[mode]
    scan_ms = (spec.line_time_ms - spec.sync_pulse_ms - 4 * spec.sync_porch_ms) / 3
    assert scan_ms == pytest.approx(expected_scan_ms, abs=1e-3), (
        f"{mode.value}: derived scan {scan_ms:.4f} ms, "
        f"expected {expected_scan_ms:.4f} ms"
    )


@pytest.mark.parametrize(
    ("mode", "expected_duration_s"),
    [
        # Published on-air lengths — the numbers in every mode table.
        (Mode.MARTIN_M2, 58.0),
        (Mode.MARTIN_M4, 29.0),
        (Mode.SCOTTIE_S2, 71.0),
        (Mode.SCOTTIE_S4, 36.0),
    ],
)
def test_half_clock_mode_durations_unchanged(
    mode: Mode, expected_duration_s: float
) -> None:
    """The width fix is free: same seconds on air, twice the detail.

    Pixel dwell is ``SCAN / WIDTH``, so doubling WIDTH halves the dwell and
    the line period is untouched.  Bounds at ±2 % against the published
    figures the reporter of issue #65 quoted.
    """
    actual = MODE_TABLE[mode].total_duration_s
    assert actual == pytest.approx(expected_duration_s, rel=0.02), (
        f"{mode.value}: {actual:.2f} s on air, published figure "
        f"{expected_duration_s:.0f} s"
    )
