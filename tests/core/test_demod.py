# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.demod``.

Drives the FM demodulation primitives with synthetic 1500 / 1900 / 2300 Hz
tones (the canonical SSTV black / mid / white frequencies). The tone tests
also act as the first integration check between ``analytic_signal``,
``instantaneous_frequency``, and ``freq_to_luma`` — pass these and the
decoder pipeline has a working frequency front-end.
"""
from __future__ import annotations

import numpy as np
import pytest

from open_sstv.core.demod import (
    SSTV_BLACK_HZ,
    SSTV_SYNC_HZ,
    SSTV_WHITE_HZ,
    analytic_signal,
    freq_to_luma,
    instantaneous_frequency,
)


def _tone(freq_hz: float, duration_s: float, fs: int) -> np.ndarray:
    """A unit-amplitude real sinusoid."""
    t = np.arange(int(duration_s * fs)) / fs
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float64)


def _trim_edges(arr: np.ndarray, frac: float = 0.1) -> np.ndarray:
    """Drop the leading/trailing fraction where Hilbert has ringing."""
    n = int(len(arr) * frac)
    return arr[n:-n] if n > 0 else arr


# === analytic_signal ===


def test_analytic_signal_returns_complex_with_same_shape() -> None:
    x = _tone(1_900.0, 0.01, 48_000)
    z = analytic_signal(x)
    assert np.iscomplexobj(z)
    assert z.shape == x.shape


def test_analytic_signal_envelope_is_constant_for_pure_tone() -> None:
    """The magnitude of the analytic signal of a sine wave is its amplitude."""
    x = _tone(1_900.0, 0.05, 48_000)
    env = np.abs(analytic_signal(x))
    np.testing.assert_allclose(_trim_edges(env).mean(), 1.0, rtol=0.01)


def test_analytic_signal_rejects_2d_input() -> None:
    with pytest.raises(ValueError, match="1-D"):
        analytic_signal(np.zeros((10, 2)))


# === instantaneous_frequency ===


@pytest.mark.parametrize("freq_hz", [SSTV_BLACK_HZ, 1_900.0, SSTV_WHITE_HZ])
def test_instantaneous_frequency_recovers_constant_tone(freq_hz: float) -> None:
    fs = 48_000
    x = _tone(freq_hz, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    # Trim edges where Hilbert has artifacts.
    np.testing.assert_allclose(_trim_edges(inst).mean(), freq_hz, rtol=0.005)


def test_instantaneous_frequency_recovers_sync_tone() -> None:
    """The 1200 Hz horizontal sync tone — the most important detection target."""
    fs = 48_000
    x = _tone(SSTV_SYNC_HZ, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    np.testing.assert_allclose(_trim_edges(inst).mean(), SSTV_SYNC_HZ, rtol=0.005)


def test_instantaneous_frequency_output_length_matches_input() -> None:
    """We right-pad ``np.diff`` so the IF array indexes 1:1 with the audio."""
    fs = 48_000
    x = _tone(1_900.0, 0.01, fs)
    inst = instantaneous_frequency(x, fs)
    assert len(inst) == len(x)


def test_instantaneous_frequency_handles_short_buffer() -> None:
    """Don't crash on a one-sample buffer (a real edge case during draining)."""
    inst = instantaneous_frequency(np.array([0.5]), 48_000)
    assert len(inst) == 1


def test_instantaneous_frequency_rejects_non_positive_fs() -> None:
    with pytest.raises(ValueError, match="positive"):
        instantaneous_frequency(np.zeros(100), 0)


# === freq_to_luma ===


def test_freq_to_luma_black_endpoint() -> None:
    assert freq_to_luma(SSTV_BLACK_HZ) == 0


def test_freq_to_luma_white_endpoint() -> None:
    assert freq_to_luma(SSTV_WHITE_HZ) == 255


def test_freq_to_luma_midpoint() -> None:
    mid_hz = (SSTV_BLACK_HZ + SSTV_WHITE_HZ) / 2.0
    luma = freq_to_luma(mid_hz)
    # Linear map: 1900 Hz → 127 or 128 depending on rounding.
    assert luma in (127, 128)


def test_freq_to_luma_clips_below_black() -> None:
    assert freq_to_luma(1_000.0) == 0
    assert freq_to_luma(SSTV_SYNC_HZ) == 0  # 1200 Hz sync clips to black


def test_freq_to_luma_clips_above_white() -> None:
    assert freq_to_luma(3_000.0) == 255


def test_freq_to_luma_array_input_returns_uint8() -> None:
    arr = np.array([SSTV_BLACK_HZ, 1_900.0, SSTV_WHITE_HZ, 1_000.0, 3_000.0])
    out = freq_to_luma(arr)
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.uint8
    assert out[0] == 0
    assert out[-2] == 0  # 1000 Hz clipped
    assert out[-1] == 255  # 3000 Hz clipped


def test_freq_to_luma_scalar_input_returns_int() -> None:
    out = freq_to_luma(SSTV_WHITE_HZ)
    assert isinstance(out, int)
    assert out == 255


# === integration: pipeline end-to-end ===


def test_pipeline_white_tone_yields_white_luma() -> None:
    """Round-trip: a 2300 Hz tone → IF → freq_to_luma should be (close to) 255."""
    fs = 48_000
    x = _tone(SSTV_WHITE_HZ, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    luma = freq_to_luma(inst)
    # Trim edges and check the body is essentially white.
    body = _trim_edges(luma)
    assert body.mean() > 250


def test_pipeline_black_tone_yields_black_luma() -> None:
    fs = 48_000
    x = _tone(SSTV_BLACK_HZ, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    luma = freq_to_luma(inst)
    body = _trim_edges(luma)
    assert body.mean() < 5


def test_pipeline_step_change_recovers_both_levels() -> None:
    """Two concatenated tones (1500 → 2300 Hz) demod into two luma plateaus."""
    fs = 48_000
    half = _tone(SSTV_BLACK_HZ, 0.05, fs)
    other = _tone(SSTV_WHITE_HZ, 0.05, fs)
    x = np.concatenate([half, other])
    inst = instantaneous_frequency(x, fs)
    luma = freq_to_luma(inst)
    # Look in the middle of each half, away from the discontinuity.
    n = len(half)
    first_body = luma[n // 4 : n - n // 8]
    second_body = luma[n + n // 8 : n + n - n // 4]
    assert first_body.mean() < 10
    assert second_body.mean() > 245
