# SPDX-License-Identifier: GPL-3.0-or-later
"""FM demodulation primitives.

SSTV is FM-modulated audio: sync at 1200 Hz, color tones spanning 1500 Hz
(black) to 2300 Hz (white). This module owns the conversion from raw audio
samples to an instantaneous-frequency track and from frequencies to luma.

The decoder pipeline is::

    samples (float32, mono)
        ──> analytic_signal          # complex baseband via Hilbert
        ──> instantaneous_frequency  # one Hz value per sample
        ──> sample_pixels            # one 0..255 byte per pixel

Deciding *where* each scan starts is the per-mode decoder's job
(``core/decoder.py`` / ``core/incremental_decoder.py``); this module hands
back a frequency track and turns a span of it into a row of pixel bytes.
(Robot 36 uses its own slowrx-style sampler in ``core/robot36_dsp.py``.)

Constants
---------
SSTV_BLACK_HZ, SSTV_WHITE_HZ:
    Color-tone endpoints. The 1500 / 2300 Hz convention is the same across
    every Martin / Scottie / Robot / PD mode we ship in v1.
SSTV_SYNC_HZ:
    1200 Hz horizontal sync tone. The sync detector lives in ``core/sync.py``
    and uses this constant rather than re-defining it.

Public API
----------
analytic_signal(x) -> np.ndarray[complex]
    Hilbert transform of a real-valued buffer.

instantaneous_frequency(x, fs) -> np.ndarray[float64]
    Per-sample instantaneous frequency in Hz. Output length matches input
    length (the phase-difference is right-padded by one sample so callers
    don't have to special-case off-by-one indexing).

sample_pixels(inst, start, span_samples, width, track_len, *, chroma=False)
    Windowed-median pixel sampler shared by the batch and incremental
    decoders for every RGB / PD mode.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy import signal

if TYPE_CHECKING:
    from numpy.typing import NDArray


#: SSTV black tone — the low end of the color-luma frequency range.
SSTV_BLACK_HZ: float = 1500.0

#: SSTV white tone — the high end of the color-luma frequency range.
SSTV_WHITE_HZ: float = 2300.0

#: SSTV horizontal sync tone. Used by ``core/sync.py``; defined here so the
#: full set of magic frequencies lives in one module.
SSTV_SYNC_HZ: float = 1200.0

#: Chroma sync-band reject threshold. A chroma sampling window that slips
#: into the sync/porch region reads well below ``SSTV_BLACK_HZ``; such a
#: sample is out-of-band leakage, not a genuine byte-0 chroma value, and is
#: clamped to neutral 128 so neighbour interpolation can recover it. Sits
#: 100 Hz below black so every legitimate chroma byte (0-255 → 1500-2300 Hz)
#: decodes as itself. Shared by the batch and incremental decoders — they
#: must agree, or the same signal yields different images depending on
#: whether it arrived live or as a WAV import.
SSTV_SYNC_REJECT_HZ: float = 1400.0


def analytic_signal(x: NDArray) -> NDArray[np.complex128]:
    """Compute the analytic representation of a real-valued buffer.

    Wraps ``scipy.signal.hilbert``: returns ``x + j·H{x}`` so the magnitude
    is the envelope and the unwrapped argument is the instantaneous phase.
    Edge samples have transient artifacts (a few percent of the buffer);
    callers that care about precise frequency at the edges should pad and
    trim.
    """
    arr = np.asarray(x)
    if arr.ndim != 1:
        raise ValueError(f"analytic_signal expects a 1-D buffer, got {arr.ndim}-D")
    return signal.hilbert(arr)


def instantaneous_frequency(x: NDArray, fs: float) -> NDArray[np.float64]:
    """Per-sample instantaneous frequency in Hz.

    Computes the unwrapped phase of the analytic signal, takes the discrete
    derivative, and scales by ``fs / (2π)``. ``np.diff`` returns ``len(x)-1``
    samples; we right-pad with the last value so the output length matches
    the input. This makes the decoder's slicing math simpler — sample
    indices in the IF array line up 1:1 with sample indices in the audio.
    """
    if fs <= 0:
        raise ValueError(f"Sample rate must be positive (got {fs})")
    z = analytic_signal(x)
    phase = np.unwrap(np.angle(z))
    if phase.size < 2:
        return np.zeros_like(phase, dtype=np.float64)
    diffs = np.diff(phase) * (fs / (2.0 * np.pi))
    # Right-pad so len(out) == len(x).
    return np.concatenate([diffs, diffs[-1:]])


def sample_pixels(
    inst: NDArray,
    start: float,
    span_samples: float,
    width: int,
    track_len: int,
    *,
    chroma: bool = False,
) -> NDArray[np.uint8]:
    """Slice a span of the frequency track into ``width`` pixel medians.

    Walks ``width`` evenly-spaced sub-windows over ``[start, start+span)``,
    takes the median frequency in each window's central 60% (matching the
    bit-window dodging trick from ``vis.detect_vis``), and maps 1500..2300 Hz
    linearly to a 0..255 byte. Pixel windows that fall outside
    ``[0, track_len)`` are left at the neutral value.

    When ``chroma=True`` the neutral value is 128 (the YCbCr midpoint)
    instead of 0, and two chroma-specific behaviours apply:

    1. **Sync-band reject** at ``SSTV_SYNC_REJECT_HZ``.  Legitimate low
       chroma decodes as itself — byte 0 is fully saturated (Cb≈0 is
       yellow, Cr≈0 cyan), not grey — but sync-pulse leakage (~1200 Hz)
       clamps to neutral instead of painting the edge green.  Luma falls
       through to the [0, 255] clip (sub-black noise just reads as dark).
    2. **Right-edge guard** of ``max(2, width // 80)`` columns (≈ 4 px at
       width 320) left neutral, because the bandpass filter smears the
       upcoming 1200 Hz sync back 10-15 samples into the end of the
       chroma scan.

    This is the single implementation used by both the batch decoder and
    the incremental decoder (v0.6.14 audit D-M3: they used to carry
    near-identical copies that drifted apart once already).  It is
    vectorised — windows are grouped by length and each group is one
    strided ``np.median`` call — but numerically identical to the old
    per-column ``np.median`` loop (``tests/core/test_demod.py`` pins
    that): same window bounds, same half-to-even rounding.
    """
    neutral: int = 128 if chroma else 0
    out = np.full(width, neutral, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    margin = pixel_span * 0.2  # central 60% of each pixel window
    guard_pixels = max(2, width // 80) if chroma else 0
    max_col = width - guard_pixels
    if max_col <= 0:
        return out

    cols = np.arange(max_col, dtype=np.float64)
    # Same expression order as the scalar loop this replaced, so the
    # float window bounds (and hence the rounded indices) are bit-equal.
    lo = np.round(start + cols * pixel_span + margin).astype(np.int64)
    hi = np.round(start + (cols + 1.0) * pixel_span - margin).astype(np.int64)
    hi = np.maximum(hi, lo + 1)
    valid = (lo >= 0) & (hi <= track_len)
    # ``inst[lo:hi]`` truncates silently past the array end; mirror that.
    hi = np.minimum(hi, inst.shape[0])
    valid &= hi > lo
    if not valid.any():
        return out

    freq = np.full(max_col, np.nan, dtype=np.float64)
    lengths = hi - lo
    for length in np.unique(lengths[valid]):
        sel = np.flatnonzero(valid & (lengths == length))
        idx = lo[sel, None] + np.arange(int(length))
        freq[sel] = np.median(inst[idx], axis=1)

    vals = np.full(max_col, float(neutral), dtype=np.float64)
    norm = (freq - SSTV_BLACK_HZ) / (SSTV_WHITE_HZ - SSTV_BLACK_HZ)
    # v0.4.0 audit high #3: NaN passes BOTH range checks (NaN comparisons
    # are False) — treat a non-finite sample as neutral rather than
    # emitting garbage.
    ok = valid & np.isfinite(norm)
    if chroma:
        # Sync-band reject (see docstring). A reading a hair under
        # SSTV_BLACK_HZ is byte 0, not neutral: demod jitter on a genuine
        # byte-0 chroma scan lands on both sides of the boundary.
        ok &= ~(freq < SSTV_SYNC_REJECT_HZ)
    vals[ok] = np.round(np.clip(norm[ok], 0.0, 1.0) * 255.0)
    out[:max_col] = vals.astype(np.uint8)
    return out


__all__ = [
    "SSTV_BLACK_HZ",
    "SSTV_SYNC_HZ",
    "SSTV_SYNC_REJECT_HZ",
    "SSTV_WHITE_HZ",
    "analytic_signal",
    "instantaneous_frequency",
    "sample_pixels",
]
