# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level SSTV decoder orchestrator.

Pull-model API: callers feed audio chunks via ``Decoder.feed(samples)`` and
the decoder yields ``DecoderEvent`` objects describing what happened
(``VISDetected``, ``LineDecoded``, ``ImageComplete``). Internally it owns a
small ring buffer, runs ``vis.detect_vis`` while idle, and dispatches into a
mode-specific decode loop once a VIS is locked.

This is the highest-risk module in v1: the per-mode pixel layouts and the
sync-locking heuristics are where everything goes wrong. Algorithms mirror
the C reference implementation ``slowrx`` (GPL).

Phase 0 stub. Implemented incrementally across Phase 2 steps 13–15 of the
v1 plan, starting with Robot 36, then Martin M1 and Scottie S1.
"""
from __future__ import annotations
