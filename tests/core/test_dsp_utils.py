# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.core.dsp_utils``.

These exercise the three primitives the decoder pipeline depends on:
audio format conversion, sample-rate conversion, and bandpass filter
construction. All run on synthetic NumPy buffers — no audio device, no
PySSTV — so they're fast and headless-CI safe.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import sosfiltfilt

from sstv_app.core.dsp_utils import bandpass_sos, resample_to, to_mono_float32


# === to_mono_float32 ===


def test_to_mono_float32_int16_scales_to_unit_range() -> None:
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    out = to_mono_float32(samples)
    assert out.dtype == np.float32
    assert -1.0 <= float(out.min())
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


# === resample_to ===


def test_resample_to_no_op_when_rates_match() -> None:
    samples = np.linspace(0.0, 1.0, 100, dtype=np.float32)
    out = resample_to(samples, 48_000, 48_000)
    np.testing.assert_array_equal(out, samples)


def test_resample_to_halves_length_when_downsampling() -> None:
    samples = np.zeros(48_000, dtype=np.float32)
    out = resample_to(samples, 48_000, 24_000)
    # resample_poly may differ by a few samples at the boundary.
    assert abs(len(out) - 24_000) <= 2


def test_resample_to_doubles_length_when_upsampling() -> None:
    samples = np.zeros(24_000, dtype=np.float32)
    out = resample_to(samples, 24_000, 48_000)
    assert abs(len(out) - 48_000) <= 2


def test_resample_to_preserves_a_pure_tone() -> None:
    """A 1 kHz tone resampled 48k → 24k should still be a 1 kHz tone."""
    fs_in, fs_out, freq = 48_000, 24_000, 1_000.0
    t = np.arange(fs_in) / fs_in
    tone = np.sin(2 * np.pi * freq * t).astype(np.float32)
    resampled = resample_to(tone, fs_in, fs_out)
    # Sanity: still a sinusoid with similar amplitude.
    assert resampled.std() == pytest.approx(tone.std(), rel=0.05)


def test_resample_to_rejects_non_positive_rates() -> None:
    samples = np.zeros(100, dtype=np.float32)
    with pytest.raises(ValueError, match="positive"):
        resample_to(samples, 0, 48_000)
    with pytest.raises(ValueError, match="positive"):
        resample_to(samples, 48_000, -1)


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
