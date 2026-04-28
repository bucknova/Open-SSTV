# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable DSP helpers shared across the encoder and decoder.

Owns format conversion (``to_mono_float32``), sample-rate conversion
(``resample_to`` via ``scipy.signal.resample_poly``), and bandpass filter
construction (``bandpass_sos`` for use with ``sosfiltfilt``). Kept tiny on
purpose — anything mode-specific belongs in ``modes.py``, not here.

Public API
----------
to_mono_float32(samples) -> np.ndarray[float32]
    Mix any audio buffer down to mono and rescale to [-1.0, 1.0]. Accepts
    int16 / int32 / float32 / float64 and 1-D or (N, channels) 2-D inputs.

resample_to(samples, src_rate, dst_rate) -> np.ndarray
    Polyphase resample of a 1-D float buffer. Returns the input unchanged
    when the rates match (the common case for our 48 kHz pipeline).

bandpass_sos(low_hz, high_hz, fs, order=4) -> np.ndarray
    Butterworth bandpass as second-order sections, ready for
    ``scipy.signal.sosfiltfilt``. SOS form because direct-form ``ba``
    coefficients are numerically fragile at the narrow bandwidths we use.
"""
from __future__ import annotations

from math import gcd
from typing import TYPE_CHECKING

import numpy as np
from scipy import signal

if TYPE_CHECKING:
    from numpy.typing import NDArray


def to_mono_float32(samples: NDArray) -> NDArray[np.float32]:
    """Convert any audio array to mono ``float32`` in ``[-1.0, 1.0]``.

    Multi-channel input (shape ``(N, channels)``) is mixed down to mono by
    averaging across channels. Integer dtypes are scaled by their type's
    max so the result lands in ``[-1.0, 1.0)``; float dtypes are passed
    through unchanged (we trust the caller's normalization).

    Raises
    ------
    ValueError
        If ``samples`` is neither 1-D nor 2-D, or has an unsupported dtype.
    """
    arr = np.asarray(samples)

    if arr.ndim == 2:
        # ``mean`` upcasts integer arrays to float64, which is fine —
        # we're about to convert to float32 anyway.
        arr = arr.mean(axis=1)
    elif arr.ndim != 1:
        raise ValueError(f"Expected 1-D or 2-D audio array, got {arr.ndim}-D")

    if arr.dtype == np.float32:
        return arr
    if arr.dtype == np.float64:
        return arr.astype(np.float32)
    if np.issubdtype(arr.dtype, np.signedinteger):
        # Standard audio convention: divide by 2**(bits-1), not iinfo.max,
        # so int16 -32768 maps to exactly -1.0 (and +32767 to ~+0.9999).
        # Dividing by iinfo.max would push the negative endpoint just
        # outside [-1, 1].
        info = np.iinfo(arr.dtype)
        scale = float(-info.min)  # 32768 for int16, 2**31 for int32
        return (arr.astype(np.float32) / scale).astype(np.float32)

    raise ValueError(f"Unsupported audio dtype: {arr.dtype}")


def resample_to(
    samples: NDArray, src_rate: int, dst_rate: int
) -> NDArray:
    """Resample a 1-D buffer from ``src_rate`` to ``dst_rate``.

    Uses ``scipy.signal.resample_poly`` after reducing the rate ratio with
    ``gcd``. Returns the input unchanged when the rates already match (the
    common case in our 48 kHz pipeline). Output length is approximately
    ``len(samples) * dst_rate / src_rate``; ``resample_poly`` may differ
    by a few samples at the boundary.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"Sample rates must be positive (got {src_rate}, {dst_rate})")
    if src_rate == dst_rate:
        return np.asarray(samples)
    g = gcd(src_rate, dst_rate)
    up = dst_rate // g
    down = src_rate // g
    return signal.resample_poly(samples, up, down)


def bandpass_sos(
    low_hz: float, high_hz: float, fs: float, order: int = 4
) -> NDArray:
    """Butterworth bandpass as second-order sections.

    The decoder uses this to isolate the SSTV passband (~1100–2400 Hz)
    from out-of-band noise before demodulation. Returned as SOS rather
    than ``b, a`` because direct-form coefficients lose precision at the
    narrow normalized bandwidths we use; SOS is what ``sosfiltfilt``
    wants anyway.

    Raises
    ------
    ValueError
        If the band edges are out of order, non-positive, or above the
        Nyquist frequency.
    """
    if not 0 < low_hz < high_hz:
        raise ValueError(
            f"Bandpass edges must satisfy 0 < low_hz < high_hz "
            f"(got low={low_hz}, high={high_hz})"
        )
    nyq = fs / 2.0
    if high_hz >= nyq:
        raise ValueError(
            f"high_hz ({high_hz}) must be below the Nyquist frequency "
            f"({nyq}) for fs={fs}"
        )
    return signal.butter(
        order,
        [low_hz / nyq, high_hz / nyq],
        btype="band",
        output="sos",
    )


__all__ = ["bandpass_sos", "resample_to", "to_mono_float32"]
