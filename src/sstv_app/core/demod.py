# SPDX-License-Identifier: GPL-3.0-or-later
"""FM demodulation primitives.

SSTV is FM-modulated audio: sync at 1200 Hz, color tones spanning roughly
1500 Hz (black) to 2300 Hz (white). This module owns the conversion from raw
audio samples to an instantaneous-frequency track and from frequencies to
luma values.

Public API:
    analytic_signal(x)             -> Hilbert transform of a real-valued buffer.
    instantaneous_frequency(x, fs) -> instantaneous frequency in Hz per sample.
    freq_to_luma(freq_hz)          -> 0..255 byte luma, clipped to mode range.

Phase 0 stub. Implemented in Phase 2 step 10 of the v1 plan.
"""
from __future__ import annotations
