# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable DSP helpers shared across the encoder and decoder.

Owns format conversion (``to_mono_float32``), sample-rate conversion
(``resample_to`` via ``scipy.signal.resample_poly``), and bandpass filter
construction (``bandpass_sos`` for use with ``sosfiltfilt``). Kept tiny on
purpose — anything mode-specific belongs in ``modes.py``, not here.

Phase 0 stub. Implemented in Phase 2 step 10 of the v1 plan.
"""
from __future__ import annotations
