# SPDX-License-Identifier: GPL-3.0-or-later
"""Slant correction for clock-drift between TX and RX.

A receiver clocked at slightly the wrong rate (e.g. 47900 Hz vs nominal 48000)
produces a slanted decoded image because each line is sampled with a small
cumulative offset. We measure that offset from the spacing of detected line
starts and either resample the input or warp the output image to undo it.

Public API:
    estimate_slant(line_starts, expected_line_samples) -> ppm correction
    apply_slant(image, ppm)                            -> corrected image

Phase 0 stub. Implemented in Phase 2 step 15 of the v1 plan.
"""
from __future__ import annotations
