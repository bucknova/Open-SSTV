# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical SSTV mode table.

Owns the ``Mode`` enum and the ``ModeSpec`` dataclass that describe each
supported SSTV mode (line layout, color order, sync timing, VIS code, native
resolution). Every other module in ``core/`` reads from ``MODE_TABLE`` rather
than hard-coding mode parameters, so adding a new mode is a single-table edit.

Phase 0 stub. Real ``MODE_TABLE`` for Robot 36, Martin M1, and Scottie S1
lands in Phase 1 step 4 of the v1 plan.
"""
from __future__ import annotations
