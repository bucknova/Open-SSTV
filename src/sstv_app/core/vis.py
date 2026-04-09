# SPDX-License-Identifier: GPL-3.0-or-later
"""VIS (Vertical Interval Signaling) header detection.

The VIS header identifies which SSTV mode follows. It's an 1900 Hz leader,
a 1200 Hz start bit, 8 data bits + 1 parity bit (each 30 ms long, 1100 Hz = 1
or 1300 Hz = 0), and a 1200 Hz stop bit. This module owns detecting that
pattern in a stream of audio samples and returning the decoded VIS code so
the decoder can dispatch into the right per-mode pixel layout.

Public API:
    detect_vis(samples, fs) -> tuple[vis_code: int, end_sample_index: int] | None

Phase 0 stub. Implemented in Phase 2 step 11 of the v1 plan.
"""
from __future__ import annotations
