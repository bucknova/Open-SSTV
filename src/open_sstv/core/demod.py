# SPDX-License-Identifier: GPL-3.0-or-later
"""FM demodulation primitives.

SSTV is FM-modulated audio: sync at 1200 Hz, color tones spanning 1500 Hz
(black) to 2300 Hz (white). This module owns the conversion from raw audio
samples to an instantaneous-frequency track and from frequencies to luma.

The decoder pipeline is::

    samples (float32, mono)
        ──> analytic_signal      # complex baseband via Hilbert
        ──> instantaneous_frequency  # one Hz value per sample
        ──> freq_to_luma         # one 0..255 byte per sample

Slicing the resulting luma stream into pixels is the per-mode decoder's job
(``core/decoder.py``); this module just hands back a frequency / luma track.

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

freq_to_luma(freq_hz) -> np.ndarray[uint8] | int
    Linear map of 1500..2300 Hz to 0..255 byte luma, clipped at the
    endpoints. Accepts a scalar (returns int) or an array (returns uint8).
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


def freq_to_luma(
    freq_hz: NDArray | float,
) -> NDArray[np.uint8] | int:
    """Map an SSTV color tone to a 0..255 byte luma.

    Linear FM: 1500 Hz → 0, 2300 Hz → 255. Frequencies outside the
    [black, white] range are clipped to the endpoints (the decoder sees
    plenty of out-of-band frequencies during sync pulses, porches, and
    noisy passages — clipping is the right behavior, not an error).

    Returns ``int`` for scalar input and ``np.ndarray[uint8]`` for array
    input, so callers can use either pattern naturally.
    """
    arr = np.asarray(freq_hz, dtype=np.float64)
    span = SSTV_WHITE_HZ - SSTV_BLACK_HZ
    luma = np.clip((arr - SSTV_BLACK_HZ) * (255.0 / span), 0.0, 255.0)
    if luma.ndim == 0:
        return int(round(float(luma)))
    return luma.astype(np.uint8)


__all__ = [
    "SSTV_BLACK_HZ",
    "SSTV_SYNC_HZ",
    "SSTV_WHITE_HZ",
    "analytic_signal",
    "freq_to_luma",
    "instantaneous_frequency",
]
