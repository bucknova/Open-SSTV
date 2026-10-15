# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.demod``.

Drives the FM demodulation primitives with synthetic 1500 / 1900 / 2300 Hz
tones (the canonical SSTV black / mid / white frequencies). The tone tests
also act as the first integration check between ``analytic_signal``,
``instantaneous_frequency``, and ``sample_pixels`` — pass these and the
decoder pipeline has a working frequency front-end.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from open_sstv.core.demod import (
    SSTV_BLACK_HZ,
    SSTV_SYNC_HZ,
    SSTV_SYNC_REJECT_HZ,
    SSTV_WHITE_HZ,
    analytic_signal,
    instantaneous_frequency,
    sample_pixels,
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


# === sample_pixels ===


def _sample_pixels_reference(
    inst: np.ndarray,
    start: float,
    span_samples: float,
    width: int,
    track_len: int,
    *,
    chroma: bool = False,
) -> np.ndarray:
    """The pre-v0.6.16 per-column loop, verbatim (v0.6.14 audit D-M3).

    ``sample_pixels`` is now vectorised; this frozen copy pins that the
    vectorised version is numerically identical, byte for byte.
    """
    neutral = 128 if chroma else 0
    out = np.full(width, neutral, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    margin = pixel_span * 0.2
    span_range = SSTV_WHITE_HZ - SSTV_BLACK_HZ
    guard_pixels = max(2, width // 80) if chroma else 0
    for col in range(width - guard_pixels):
        lo = int(round(start + col * pixel_span + margin))
        hi = int(round(start + (col + 1) * pixel_span - margin))
        if hi <= lo:
            hi = lo + 1
        if lo < 0 or hi > track_len:
            continue
        chunk = inst[lo:hi]
        if chunk.size == 0:
            continue
        freq = float(np.median(chunk))
        if chroma and freq < SSTV_SYNC_REJECT_HZ:
            out[col] = neutral
            continue
        norm = (freq - SSTV_BLACK_HZ) / span_range
        if not math.isfinite(norm):
            out[col] = neutral
            continue
        norm = min(1.0, max(0.0, norm))
        out[col] = int(round(norm * 255.0))
    return out


@pytest.mark.parametrize("chroma", [False, True])
def test_sample_pixels_matches_reference_loop(chroma: bool) -> None:
    """Vectorised sampler == old scalar loop on noisy tracks, fractional
    starts / spans, buffer-edge overruns and NaN glitches."""
    rng = np.random.default_rng(1234)
    for trial in range(300):
        n = int(rng.integers(50, 6000))
        inst = rng.uniform(1000.0, 2600.0, n)
        if trial % 7 == 0:
            inst[rng.integers(0, n, 5)] = np.nan
        if trial % 11 == 0:
            inst[rng.integers(0, n, 5)] = np.inf
        width = int(rng.integers(1, 400))
        span = float(rng.uniform(0.5, 1.5)) * n * float(rng.uniform(0.2, 1.2))
        start = float(rng.uniform(-0.2 * n, 0.6 * n))
        # track_len sometimes exceeds the array (callers pass n; the
        # loop's slice truncation must be mirrored anyway).
        track_len = n + int(rng.integers(0, 3)) if trial % 5 == 0 else n
        got = sample_pixels(inst, start, span, width, track_len, chroma=chroma)
        want = _sample_pixels_reference(
            inst, start, span, width, track_len, chroma=chroma
        )
        np.testing.assert_array_equal(got, want, err_msg=f"trial {trial}")


def test_sample_pixels_maps_band_endpoints() -> None:
    inst = np.concatenate([
        np.full(100, SSTV_BLACK_HZ), np.full(100, SSTV_WHITE_HZ),
        np.full(100, 1000.0), np.full(100, 3000.0),
    ])
    out = sample_pixels(inst, 0.0, 400.0, 4, inst.size)
    assert out.tolist() == [0, 255, 0, 255]


def test_sample_pixels_chroma_rejects_sync_band_to_neutral() -> None:
    inst = np.concatenate([np.full(100, SSTV_SYNC_HZ), np.full(300, 1520.0)])
    # width 320 → 4 guard columns; use width 4 → guard 2, so only the
    # first two columns are sampled.
    out = sample_pixels(inst, 0.0, 400.0, 4, inst.size, chroma=True)
    assert out[0] == 128  # 1200 Hz sync leakage → neutral, not byte 0
    assert out[1] == 6  # genuine low chroma decodes as itself
    assert out[2:].tolist() == [128, 128]  # right-edge guard


def test_sample_pixels_out_of_range_windows_stay_neutral() -> None:
    inst = np.full(100, SSTV_WHITE_HZ)
    out = sample_pixels(inst, 50.0, 100.0, 4, inst.size)
    assert out.tolist() == [255, 255, 0, 0]


# === integration: pipeline end-to-end ===


def test_pipeline_white_tone_yields_white_luma() -> None:
    """Round-trip: a 2300 Hz tone → IF → sample_pixels should be ~255."""
    fs = 48_000
    x = _tone(SSTV_WHITE_HZ, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    body = _trim_edges(inst)
    luma = sample_pixels(body, 0.0, float(body.size), 32, body.size)
    assert luma.mean() > 250


def test_pipeline_black_tone_yields_black_luma() -> None:
    fs = 48_000
    x = _tone(SSTV_BLACK_HZ, 0.05, fs)
    inst = instantaneous_frequency(x, fs)
    body = _trim_edges(inst)
    luma = sample_pixels(body, 0.0, float(body.size), 32, body.size)
    assert luma.mean() < 5


def test_pipeline_step_change_recovers_both_levels() -> None:
    """Two concatenated tones (1500 → 2300 Hz) demod into two luma plateaus."""
    fs = 48_000
    half = _tone(SSTV_BLACK_HZ, 0.05, fs)
    other = _tone(SSTV_WHITE_HZ, 0.05, fs)
    x = np.concatenate([half, other])
    inst = instantaneous_frequency(x, fs)
    n = len(half)
    luma = sample_pixels(inst, 0.0, float(2 * n), 16, inst.size)
    # Look in the middle of each half, away from the discontinuity.
    assert luma[1:7].mean() < 10
    assert luma[9:15].mean() > 245
