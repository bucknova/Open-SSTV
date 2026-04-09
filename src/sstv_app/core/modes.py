# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical SSTV mode table.

Owns the ``Mode`` enum and the ``ModeSpec`` dataclass that describe each
supported SSTV mode (line layout, color order, sync timing, VIS code, native
resolution). Every other module in ``core/`` reads from ``MODE_TABLE`` rather
than hard-coding mode parameters, so adding a new mode is a single-table edit.

This file is intentionally lean: it holds **only** facts defined by the SSTV
protocol itself, not implementation details like which PySSTV class encodes
the mode (that mapping lives in ``core/encoder.py``). Keeping the table free
of third-party imports lets us read mode metadata in tests and the UI without
pulling in heavyweight dependencies.

Numbers cross-checked against PySSTV's source (``pysstv/color.py``) and the
well-known on-air timings for each mode (Martin M1 ≈ 114 s, Scottie S1 ≈ 110 s,
Robot 36 = 36 s exact).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class Mode(StrEnum):
    """Supported SSTV modes.

    v1 ships the three most-on-air modes worldwide. Adding a new mode requires
    (a) a new enum value here, (b) a ``MODE_TABLE`` entry, (c) a class mapping
    in ``core/encoder.py``, and — for RX — (d) a per-mode decode function in
    ``core/decoder.py``.
    """

    ROBOT_36 = "robot_36"
    MARTIN_M1 = "martin_m1"
    SCOTTIE_S1 = "scottie_s1"


@unique
class SyncPosition(StrEnum):
    """Where the 1200 Hz horizontal sync pulse falls within each scan line.

    The decoder needs this to slice incoming audio correctly: Martin and Robot
    put the sync pulse at the start of the line, but Scottie family modes put
    it mid-line, between the blue and red components. The Scottie quirk is
    the single most common source of off-by-one bugs in SSTV decoders.
    """

    LINE_START = "line_start"  # Martin, Robot, PD ...
    BEFORE_RED = "before_red"  # Scottie family — sync separates blue from red


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """Protocol-level parameters for one SSTV mode."""

    name: Mode
    vis_code: int
    width: int
    height: int
    sync_pulse_ms: float
    """1200 Hz horizontal sync pulse duration."""
    sync_porch_ms: float
    """1500 Hz porch length (between sync and pixel data)."""
    line_time_ms: float
    """Total per-line duration including sync, porches, and all channels."""
    color_layout: tuple[str, ...]
    """Channel labels in transmission order. ``("G","B","R")`` for Martin /
    Scottie, ``("Y","C")`` for Robot 36 (Y every line, chroma alternating
    B-Y on even lines / R-Y on odd lines)."""
    sync_position: SyncPosition

    @property
    def total_duration_s(self) -> float:
        """Approximate end-to-end transmission length, in seconds.

        Body-only — ignores the ~600 ms VIS leader, which is small relative
        to multi-second image bodies. Tests bound on this with a ±5% slop.
        """
        return (self.line_time_ms * self.height) / 1000.0


# === Per-mode protocol constants (from PySSTV ``pysstv/color.py``) ===

# Martin M1 — most common European mode, ~114 s for a 320×256 image.
_MARTIN_M1_SCAN_MS = 146.432   # per-channel scan time
_MARTIN_M1_PORCH_MS = 0.572    # 1500 Hz inter-channel gap
_MARTIN_M1_SYNC_MS = 4.862     # 1200 Hz horizontal sync pulse

# Scottie S1 — most common US mode, ~110 s. Sync pulse separates B from R
# within each line, not between lines (the defining oddity of Scottie modes).
_SCOTTIE_S1_SCAN_MS = 138.24 - 1.5
_SCOTTIE_S1_PORCH_MS = 1.5
_SCOTTIE_S1_SYNC_MS = 9.0

# Robot 36 — most common HF / ISS SSTV mode, 36 s exact for 320×240. YUV not
# RGB: Y on every line, chroma channel alternates B-Y on even / R-Y on odd.
_ROBOT_36_Y_SCAN_MS = 88.0
_ROBOT_36_C_SCAN_MS = 44.0
_ROBOT_36_INTER_CH_GAP_MS = 4.5
_ROBOT_36_INTER_CH_PORCH_MS = 1.5
_ROBOT_36_SYNC_MS = 9.0
_ROBOT_36_SYNC_PORCH_MS = 3.0


MODE_TABLE: dict[Mode, ModeSpec] = {
    Mode.MARTIN_M1: ModeSpec(
        name=Mode.MARTIN_M1,
        vis_code=0x2C,  # 44
        width=320,
        height=256,
        sync_pulse_ms=_MARTIN_M1_SYNC_MS,
        sync_porch_ms=_MARTIN_M1_PORCH_MS,
        # 1 sync + 4 porches (before-G, after-G, after-B, after-R) + 3 channel scans
        line_time_ms=(
            _MARTIN_M1_SYNC_MS
            + 4 * _MARTIN_M1_PORCH_MS
            + 3 * _MARTIN_M1_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.SCOTTIE_S1: ModeSpec(
        name=Mode.SCOTTIE_S1,
        vis_code=0x3C,  # 60
        width=320,
        height=256,
        sync_pulse_ms=_SCOTTIE_S1_SYNC_MS,
        sync_porch_ms=_SCOTTIE_S1_PORCH_MS,
        # 1 sync (mid-line, before R) + 6 porches (before/after each of G, B, R)
        # + 3 channel scans. Note: NO sync at start of line for Scottie.
        line_time_ms=(
            _SCOTTIE_S1_SYNC_MS
            + 6 * _SCOTTIE_S1_PORCH_MS
            + 3 * _SCOTTIE_S1_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.BEFORE_RED,
    ),
    Mode.ROBOT_36: ModeSpec(
        name=Mode.ROBOT_36,
        vis_code=0x08,  # 8
        width=320,
        height=240,
        sync_pulse_ms=_ROBOT_36_SYNC_MS,
        sync_porch_ms=_ROBOT_36_SYNC_PORCH_MS,
        # SYNC + SYNC_PORCH + Y_SCAN + INTER_CH_GAP + PORCH + C_SCAN = 150 ms
        line_time_ms=(
            _ROBOT_36_SYNC_MS
            + _ROBOT_36_SYNC_PORCH_MS
            + _ROBOT_36_Y_SCAN_MS
            + _ROBOT_36_INTER_CH_GAP_MS
            + _ROBOT_36_INTER_CH_PORCH_MS
            + _ROBOT_36_C_SCAN_MS
        ),
        color_layout=("Y", "C"),
        sync_position=SyncPosition.LINE_START,
    ),
}


def mode_from_vis(code: int) -> Mode | None:
    """Look up an SSTV ``Mode`` by VIS code, or ``None`` if unknown.

    The decoder calls this after detecting a VIS header to dispatch into the
    right per-mode pixel layout. Returns ``None`` for unsupported modes so
    callers can choose to log/skip rather than crash.
    """
    for spec in MODE_TABLE.values():
        if spec.vis_code == code:
            return spec.name
    return None


__all__ = [
    "MODE_TABLE",
    "Mode",
    "ModeSpec",
    "SyncPosition",
    "mode_from_vis",
]
