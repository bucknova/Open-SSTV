# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable DSP helpers shared across the encoder and decoder.

Owns format conversion (``to_mono_float32``), run extraction on boolean
masks (``find_runs``), bandpass filter construction (``bandpass_sos`` for
use with ``sosfiltfilt``) and the decoder's SSTV-band pre-demod filter
(``sstv_bandpass``). Kept tiny on purpose — anything mode-specific belongs
in ``modes.py``, not here.

Public API
----------
to_mono_float32(samples) -> np.ndarray[float32]
    Mix any audio buffer down to mono and rescale to [-1.0, 1.0]. Accepts
    signed / unsigned integer and float dtypes, 1-D or (N, channels) 2-D.

find_runs(mask) -> list[tuple[int, int]]
    ``(start, end)`` of every maximal True run in a boolean mask.

bandpass_sos(low_hz, high_hz, fs, order=4) -> np.ndarray
    Butterworth bandpass as second-order sections, ready for
    ``scipy.signal.sosfiltfilt``. SOS form because direct-form ``ba``
    coefficients are numerically fragile at the narrow bandwidths we use.

sstv_bandpass(x, fs) -> np.ndarray
    Zero-phase 1000–2500 Hz bandpass applied before FM demodulation by
    both the batch and the incremental decoder (one implementation, so
    the two can't drift apart).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy import signal
from scipy.signal import sosfiltfilt

if TYPE_CHECKING:
    from numpy.typing import NDArray


def to_mono_float32(samples: NDArray) -> NDArray[np.float32]:
    """Convert any audio array to mono ``float32`` in ``[-1.0, 1.0]``.

    Multi-channel input (shape ``(N, channels)``) is mixed down to mono by
    averaging across channels. Signed integer dtypes are scaled by
    ``2**(bits-1)`` so the result lands in ``[-1.0, 1.0)``; unsigned
    integer dtypes (8-bit WAV is unsigned by spec) are re-centred on their
    midpoint first. Float dtypes are passed through unchanged (we trust
    the caller's normalization).

    v0.6.14 audit D-M2: integers are scaled *before* the channel mix.
    ``mean`` promotes an integer array to float64, so the old
    mix-then-scale order handed stereo int16 through unscaled (±32768)
    and rejected every unsigned dtype outright.

    Raises
    ------
    ValueError
        If ``samples`` is neither 1-D nor 2-D, or has an unsupported dtype.
    """
    arr = np.asarray(samples)
    if arr.ndim not in (1, 2):
        raise ValueError(f"Expected 1-D or 2-D audio array, got {arr.ndim}-D")

    if np.issubdtype(arr.dtype, np.signedinteger):
        # Standard audio convention: divide by 2**(bits-1), not iinfo.max,
        # so int16 -32768 maps to exactly -1.0 (and +32767 to ~+0.9999).
        # Dividing by iinfo.max would push the negative endpoint just
        # outside [-1, 1].
        scale = float(-np.iinfo(arr.dtype).min)  # 32768 for int16, 2**31 for int32
        arr = arr.astype(np.float64) / scale
    elif np.issubdtype(arr.dtype, np.unsignedinteger):
        # Unsigned PCM is offset binary: midpoint (128 for uint8) is silence.
        mid = (float(np.iinfo(arr.dtype).max) + 1.0) / 2.0
        arr = (arr.astype(np.float64) - mid) / mid
    elif not np.issubdtype(arr.dtype, np.floating):
        raise ValueError(f"Unsupported audio dtype: {arr.dtype}")

    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    return arr.astype(np.float32, copy=False)


def find_runs(mask: NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Return ``[(start, end), …]`` for each maximal True run in ``mask``.

    ``end`` is exclusive (slice-friendly). Empty input returns an empty
    list. Shared by the VIS detector and the line-sync detector (they
    used to carry identical private copies).
    """
    if mask.size == 0:
        return []
    diff = np.diff(mask.astype(np.int8))
    starts = (np.where(diff == 1)[0] + 1).tolist()
    ends = (np.where(diff == -1)[0] + 1).tolist()
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(int(mask.size))
    return list(zip(starts, ends, strict=True))


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


# Bandpass edges for the decoder's pre-demod filter. The SSTV signalling
# frequencies span 1100 Hz (VIS '1') through 2300 Hz (white luma); we push
# the edges out to 1000–2500 Hz to leave ~100 Hz of margin for filter
# rolloff and plausible TX/RX clock drift. Butterworth order 4 gives
# ~24 dB/octave stopband attenuation, which — combined with
# ``sosfiltfilt``'s zero-phase response — drops out-of-band noise variance
# enough to recover ~12 dB of sync-detector margin vs. running the Hilbert
# transform on raw audio. Measured in
# tests/core/test_decoder.py::test_decode_wav_robot36_low_snr.
SSTV_BANDPASS_LOW_HZ: float = 1000.0
SSTV_BANDPASS_HIGH_HZ: float = 2500.0
SSTV_BANDPASS_ORDER: int = 4

#: ``sosfiltfilt`` forward/back filters with a default padlen ≈ 3 * filter
#: length; very short inputs (well below any plausible SSTV image) trip
#: that guard. Buffers under this many samples are returned unfiltered —
#: the downstream decode returns ``None`` for them anyway.
SSTV_BANDPASS_MIN_SAMPLES: int = 256


def sstv_bandpass(x: NDArray, fs: int) -> NDArray:
    """Zero-phase bandpass to the SSTV signalling band.

    Returns the input unchanged if ``fs`` is too low to support the
    chosen band edges (guard against pathological test inputs), or if
    the buffer is shorter than ``sosfiltfilt`` can stably pad. All
    other cases run a Butterworth order-4 forward/back filter.
    Zero-phase is mandatory: any group delay would shift sync positions
    and corrupt the per-line pixel slicing downstream.
    """
    if x.size < SSTV_BANDPASS_MIN_SAMPLES:
        return x
    try:
        sos = bandpass_sos(
            SSTV_BANDPASS_LOW_HZ, SSTV_BANDPASS_HIGH_HZ, fs,
            order=SSTV_BANDPASS_ORDER,
        )
    except ValueError:
        # fs too low for the chosen band edges — skip filtering rather
        # than blowing up the decode path with a construction error.
        return x
    return sosfiltfilt(sos, x)


__all__ = [
    "SSTV_BANDPASS_MIN_SAMPLES",
    "bandpass_sos",
    "find_runs",
    "sstv_bandpass",
    "to_mono_float32",
]
