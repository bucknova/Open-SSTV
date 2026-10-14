# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.dsp_utils``.

These exercise the three primitives the decoder pipeline depends on:
audio format conversion, run extraction, and bandpass filter
construction. All run on synthetic NumPy buffers — no audio device, no
PySSTV — so they're fast and headless-CI safe.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import sosfiltfilt

from open_sstv.core.dsp_utils import bandpass_sos, find_runs, to_mono_float32

# === to_mono_float32 ===


def test_to_mono_float32_int16_scales_to_unit_range() -> None:
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    out = to_mono_float32(samples)
    assert out.dtype == np.float32
    assert float(out.min()) >= -1.0
    assert float(out.max()) <= 1.0
    # Mid-scale int16 should land near ±0.5.
    np.testing.assert_allclose(out[1], 0.5, atol=1e-4)
    np.testing.assert_allclose(out[2], -0.5, atol=1e-4)


def test_to_mono_float32_int32_scales_correctly() -> None:
    samples = np.array([0, 2**30, -(2**30)], dtype=np.int32)
    out = to_mono_float32(samples)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[1], 0.5, atol=1e-4)
    np.testing.assert_allclose(out[2], -0.5, atol=1e-4)


def test_to_mono_float32_float32_passthrough() -> None:
    samples = np.linspace(-1.0, 1.0, 100, dtype=np.float32)
    out = to_mono_float32(samples)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, samples)


def test_to_mono_float32_float64_downcasts_to_float32() -> None:
    samples = np.linspace(-1.0, 1.0, 100, dtype=np.float64)
    out = to_mono_float32(samples)
    assert out.dtype == np.float32


def test_to_mono_float32_stereo_mixdown_averages_channels() -> None:
    left = np.full(100, 0.5, dtype=np.float32)
    right = np.full(100, -0.5, dtype=np.float32)
    stereo = np.column_stack([left, right])
    out = to_mono_float32(stereo)
    assert out.shape == (100,)
    np.testing.assert_array_almost_equal(out, np.zeros(100), decimal=6)


def test_to_mono_float32_rejects_3d_input() -> None:
    samples = np.zeros((10, 2, 2), dtype=np.float32)
    with pytest.raises(ValueError, match="1-D or 2-D"):
        to_mono_float32(samples)


def test_to_mono_float32_rejects_unsupported_dtype() -> None:
    samples = np.array(["a", "b"], dtype=object)
    with pytest.raises(ValueError, match="Unsupported audio dtype"):
        to_mono_float32(samples)


def test_to_mono_float32_stereo_int16_is_scaled_before_mixdown() -> None:
    """v0.6.14 audit D-M2: ``mean`` promoted stereo int16 to float64 and
    the float passthrough then returned it unscaled (±32768)."""
    stereo = np.array([[16384, 16384], [-32768, -32768]], dtype=np.int16)
    out = to_mono_float32(stereo)
    np.testing.assert_allclose(out, [0.5, -1.0], atol=1e-6)


@pytest.mark.parametrize(
    ("dtype", "lo", "mid", "hi"),
    [(np.uint8, 0, 128, 255), (np.uint16, 0, 32768, 65535)],
)
def test_to_mono_float32_unsigned_is_recentred(dtype, lo, mid, hi) -> None:
    """v0.6.14 audit D-M2: unsigned PCM (offset binary) used to raise."""
    out = to_mono_float32(np.array([lo, mid, hi], dtype=dtype))
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [-1.0, 0.0, 1.0], atol=1e-2)


def test_find_runs_basic() -> None:
    mask = np.array([1, 1, 0, 0, 1, 0, 1, 1, 1], dtype=bool)
    assert find_runs(mask) == [(0, 2), (4, 5), (6, 9)]
    assert find_runs(np.zeros(0, dtype=bool)) == []
    assert find_runs(np.zeros(4, dtype=bool)) == []


# === bandpass_sos ===


def test_bandpass_sos_passes_in_band_tone_and_blocks_out_of_band() -> None:
    fs = 48_000
    sos = bandpass_sos(1_000, 2_500, fs)
    t = np.arange(fs) / fs  # one second
    in_band = np.sin(2 * np.pi * 1_900 * t).astype(np.float64)
    out_of_band = np.sin(2 * np.pi * 6_000 * t).astype(np.float64)

    in_filt = sosfiltfilt(sos, in_band)
    out_filt = sosfiltfilt(sos, out_of_band)

    # In-band signal mostly preserved (>80% of original RMS).
    assert in_filt.std() > 0.8 * in_band.std()
    # Out-of-band signal heavily attenuated (>40 dB).
    assert out_filt.std() < 0.01 * out_of_band.std()


def test_bandpass_sos_returns_sos_shape() -> None:
    sos = bandpass_sos(1_000, 2_500, 48_000, order=4)
    # SOS form: (n_sections, 6). Order 4 bandpass = 4 sections.
    assert sos.ndim == 2
    assert sos.shape[1] == 6
    assert sos.shape[0] == 4


def test_bandpass_sos_rejects_inverted_band() -> None:
    with pytest.raises(ValueError, match="low_hz < high_hz"):
        bandpass_sos(2_500, 1_000, 48_000)


def test_bandpass_sos_rejects_zero_low_edge() -> None:
    with pytest.raises(ValueError, match="low_hz < high_hz"):
        bandpass_sos(0, 2_500, 48_000)


def test_bandpass_sos_rejects_above_nyquist() -> None:
    with pytest.raises(ValueError, match="Nyquist"):
        bandpass_sos(1_000, 30_000, 48_000)
