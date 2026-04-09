# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync pulse detection within a demodulated frequency track.

After FM demodulation we have an instantaneous-frequency time series. SSTV
modes mark the start of each scan line with a 1200 Hz sync pulse (length and
porch length vary by mode). This module finds the leading 1900/1200/1900
header that precedes the VIS, and the per-line 1200 Hz sync pulses that
let us slice the rest of the transmission into rows.

Public API:
    find_leader(freq_track, fs)             -> sample index of VIS start, or None
    find_line_starts(freq_track, fs, spec)  -> list[int] of per-line start indices

Phase 0 stub. Implemented in Phase 2 step 12 of the v1 plan.
"""
from __future__ import annotations
