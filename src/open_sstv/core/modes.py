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

    Adding a new mode requires (a) a new enum value here, (b) a ``MODE_TABLE``
    entry, (c) a class mapping in ``core/encoder.py``, and — for RX — (d) a
    per-mode decode function registered in ``core/decoder.py``.

    Modes backed by PySSTV (directly or via thin subclass): Robot 36,
    Martin M1/M2/M3/M4, Scottie S1/S2/DX/S3/S4, PD 50/90/120/160/180/240/290,
    Wraase SC2-120/180, Pasokon P3/P5/P7.

    Modes not yet supported (need custom encoders — Robot family YCbCr):
    Robot 8, Robot 12, Robot 24, Robot 72.
    """

    ROBOT_36 = "robot_36"
    MARTIN_M1 = "martin_m1"
    MARTIN_M2 = "martin_m2"
    MARTIN_M3 = "martin_m3"
    MARTIN_M4 = "martin_m4"
    SCOTTIE_S1 = "scottie_s1"
    SCOTTIE_S2 = "scottie_s2"
    SCOTTIE_DX = "scottie_dx"
    SCOTTIE_S3 = "scottie_s3"
    SCOTTIE_S4 = "scottie_s4"
    PD_50 = "pd_50"
    PD_90 = "pd_90"
    PD_120 = "pd_120"
    PD_160 = "pd_160"
    PD_180 = "pd_180"
    PD_240 = "pd_240"
    PD_290 = "pd_290"
    WRAASE_SC2_120 = "wraase_sc2_120"
    WRAASE_SC2_180 = "wraase_sc2_180"
    PASOKON_P3 = "pasokon_p3"
    PASOKON_P5 = "pasokon_p5"
    PASOKON_P7 = "pasokon_p7"


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
# Martin M2 — half horizontal resolution variant, ~57 s for 160×256.
# Both share the same sync/porch timing; only the channel scan time differs.
_MARTIN_M1_SCAN_MS = 146.432   # per-channel scan time
_MARTIN_M2_SCAN_MS = 73.216
_MARTIN_M1_PORCH_MS = 0.572    # 1500 Hz inter-channel gap (same for M1 and M2)
_MARTIN_M1_SYNC_MS = 4.862     # 1200 Hz horizontal sync pulse (same for M1 and M2)

# Scottie S1 — most common US mode, ~110 s. Sync pulse separates B from R
# within each line, not between lines (the defining oddity of Scottie modes).
# S2 (half-res, ~71 s) and DX (high-quality, ~269 s) share the same sync/porch.
_SCOTTIE_S1_SCAN_MS = 138.24 - 1.5  # = 136.74 ms (PySSTV: SCAN = TOTAL - INTER_CH_GAP)
_SCOTTIE_S2_SCAN_MS = 86.564
_SCOTTIE_DX_SCAN_MS = 344.1
_SCOTTIE_S1_PORCH_MS = 1.5           # same for S1, S2, DX
_SCOTTIE_S1_SYNC_MS = 9.0            # same for S1, S2, DX

# Robot 36 — most common HF / ISS SSTV mode, 36 s exact for 320×240. YUV not
# RGB: Y on every line, chroma channel alternates B-Y on even / R-Y on odd.
_ROBOT_36_Y_SCAN_MS = 88.0
_ROBOT_36_C_SCAN_MS = 44.0
_ROBOT_36_INTER_CH_GAP_MS = 4.5
_ROBOT_36_INTER_CH_PORCH_MS = 1.5
_ROBOT_36_SYNC_MS = 9.0
_ROBOT_36_SYNC_PORCH_MS = 3.0

# PD family — YCbCr line-pair format (one sync per two image rows).
# Per super-line: SYNC(20 ms) + PORCH(2.08 ms) + Y0 + Cr + Cb + Y1,
# where each of the four channel scans is WIDTH × PIXEL ms long.
# MODE_TABLE stores height = actual_height // 2 (number of sync pulses /
# super-lines) so the standard sync-grid walk in the decoder finds the right
# count, and line_time_ms is the full super-line period. total_duration_s
# therefore equals (line_time_ms × (height÷2×2)) / 1000 = correct duration.
_PD_SYNC_MS: float = 20.0
_PD_PORCH_MS: float = 2.08
_PD_50_CHANNEL_SCAN_MS: float = 320 * 0.286    # 91.52 ms
_PD_90_CHANNEL_SCAN_MS: float = 320 * 0.532    # 170.24 ms
_PD_120_CHANNEL_SCAN_MS: float = 640 * 0.190   # 121.60 ms
_PD_160_CHANNEL_SCAN_MS: float = 512 * 0.382   # 195.584 ms
_PD_180_CHANNEL_SCAN_MS: float = 640 * 0.286   # 183.04 ms
_PD_240_CHANNEL_SCAN_MS: float = 640 * 0.382   # 244.48 ms
_PD_290_CHANNEL_SCAN_MS: float = 800 * 0.286   # 228.80 ms

# Wraase SC2 family — RGB, one 0.5 ms porch before the red channel only
# (no gaps between green and blue). Two widths share the same sync timing.
_WRAASE_SC2_SYNC_MS: float = 5.5225
_WRAASE_SC2_PORCH_MS: float = 0.5
_WRAASE_SC2_180_SCAN_MS: float = 235.0
_WRAASE_SC2_120_SCAN_MS: float = 156.0

# Pasokon family — RGB with equal inter-channel gaps before and after every
# channel (4 gaps per line). Three time-unit multiples give P3/P5/P7.
# TIMEUNIT for each: 1000/4800, 1000/3200, 1000/2400 ms.
# sync_porch_ms is set to the INTER_CH_GAP so that the generic decoder can
# derive scan_ms = (line_time − sync − 4×gap) / 3, matching Martin's formula.
_PASOKON_P3_SYNC_MS: float = 1000.0 / 4800.0 * 25    # ≈ 5.2083 ms
_PASOKON_P3_SCAN_MS: float = 1000.0 / 4800.0 * 640   # ≈ 133.333 ms
_PASOKON_P3_GAP_MS: float = 1000.0 / 4800.0 * 5      # ≈ 1.0417 ms

_PASOKON_P5_SYNC_MS: float = 1000.0 / 3200.0 * 25    # = 7.8125 ms
_PASOKON_P5_SCAN_MS: float = 1000.0 / 3200.0 * 640   # = 200.0 ms
_PASOKON_P5_GAP_MS: float = 1000.0 / 3200.0 * 5      # = 1.5625 ms

_PASOKON_P7_SYNC_MS: float = 1000.0 / 2400.0 * 25    # ≈ 10.4167 ms
_PASOKON_P7_SCAN_MS: float = 1000.0 / 2400.0 * 640   # ≈ 266.667 ms
_PASOKON_P7_GAP_MS: float = 1000.0 / 2400.0 * 5      # ≈ 2.0833 ms


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

    # ------------------------------------------------------------------ #
    # Martin M2 — half horizontal resolution of M1, ~57 s for 160×256.   #
    # ------------------------------------------------------------------ #
    Mode.MARTIN_M2: ModeSpec(
        name=Mode.MARTIN_M2,
        vis_code=0x28,  # 40
        width=160,
        height=256,
        sync_pulse_ms=_MARTIN_M1_SYNC_MS,
        sync_porch_ms=_MARTIN_M1_PORCH_MS,
        # SYNC + 4×PORCH + 3×SCAN (identical structure to M1)
        line_time_ms=(
            _MARTIN_M1_SYNC_MS
            + 4 * _MARTIN_M1_PORCH_MS
            + 3 * _MARTIN_M2_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.LINE_START,
    ),

    # ------------------------------------------------------------------ #
    # Martin M3 / M4 — 128-line variants of M1/M2. Identical timing; only  #
    # height differs. VIS 36 (M3) / 32 (M4). ~57 s / ~29 s.              #
    # ------------------------------------------------------------------ #
    Mode.MARTIN_M3: ModeSpec(
        name=Mode.MARTIN_M3,
        vis_code=0x24,  # 36
        width=320,
        height=128,
        sync_pulse_ms=_MARTIN_M1_SYNC_MS,
        sync_porch_ms=_MARTIN_M1_PORCH_MS,
        line_time_ms=(
            _MARTIN_M1_SYNC_MS
            + 4 * _MARTIN_M1_PORCH_MS
            + 3 * _MARTIN_M1_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.MARTIN_M4: ModeSpec(
        name=Mode.MARTIN_M4,
        vis_code=0x20,  # 32
        width=160,
        height=128,
        sync_pulse_ms=_MARTIN_M1_SYNC_MS,
        sync_porch_ms=_MARTIN_M1_PORCH_MS,
        line_time_ms=(
            _MARTIN_M1_SYNC_MS
            + 4 * _MARTIN_M1_PORCH_MS
            + 3 * _MARTIN_M2_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.LINE_START,
    ),

    # ------------------------------------------------------------------ #
    # Scottie S2 — half horizontal resolution of S1, ~71 s for 160×256.  #
    # Scottie DX — wide-scan high-quality, ~269 s for 320×256.           #
    # ------------------------------------------------------------------ #
    Mode.SCOTTIE_S2: ModeSpec(
        name=Mode.SCOTTIE_S2,
        vis_code=0x38,  # 56
        width=160,
        height=256,
        sync_pulse_ms=_SCOTTIE_S1_SYNC_MS,
        sync_porch_ms=_SCOTTIE_S1_PORCH_MS,
        # SYNC (mid-line, before R) + 6×PORCH + 3×SCAN
        line_time_ms=(
            _SCOTTIE_S1_SYNC_MS
            + 6 * _SCOTTIE_S1_PORCH_MS
            + 3 * _SCOTTIE_S2_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.BEFORE_RED,
    ),
    Mode.SCOTTIE_DX: ModeSpec(
        name=Mode.SCOTTIE_DX,
        vis_code=0x4C,  # 76
        width=320,
        height=256,
        sync_pulse_ms=_SCOTTIE_S1_SYNC_MS,
        sync_porch_ms=_SCOTTIE_S1_PORCH_MS,
        line_time_ms=(
            _SCOTTIE_S1_SYNC_MS
            + 6 * _SCOTTIE_S1_PORCH_MS
            + 3 * _SCOTTIE_DX_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.BEFORE_RED,
    ),

    # ------------------------------------------------------------------ #
    # Scottie S3 / S4 — 128-line variants of S1/S2. Identical timing;    #
    # only height differs. VIS 52 (S3) / 48 (S4). ~55 s / ~36 s.        #
    # ------------------------------------------------------------------ #
    Mode.SCOTTIE_S3: ModeSpec(
        name=Mode.SCOTTIE_S3,
        vis_code=0x34,  # 52
        width=320,
        height=128,
        sync_pulse_ms=_SCOTTIE_S1_SYNC_MS,
        sync_porch_ms=_SCOTTIE_S1_PORCH_MS,
        line_time_ms=(
            _SCOTTIE_S1_SYNC_MS
            + 6 * _SCOTTIE_S1_PORCH_MS
            + 3 * _SCOTTIE_S1_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.BEFORE_RED,
    ),
    Mode.SCOTTIE_S4: ModeSpec(
        name=Mode.SCOTTIE_S4,
        vis_code=0x30,  # 48
        width=160,
        height=128,
        sync_pulse_ms=_SCOTTIE_S1_SYNC_MS,
        sync_porch_ms=_SCOTTIE_S1_PORCH_MS,
        line_time_ms=(
            _SCOTTIE_S1_SYNC_MS
            + 6 * _SCOTTIE_S1_PORCH_MS
            + 3 * _SCOTTIE_S2_SCAN_MS
        ),
        color_layout=("G", "B", "R"),
        sync_position=SyncPosition.BEFORE_RED,
    ),

    # ------------------------------------------------------------------ #
    # PD family — YCbCr line-pair (one sync covers two image rows).       #
    # height = actual_image_height // 2 (number of sync pulses).          #
    # line_time_ms = full super-line period (SYNC + PORCH + 4×channel).   #
    # Decoders output width × (height×2) images.                          #
    # VIS codes: 7 LSBs of PySSTV VIS_CODE constant.                      #
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # PD-50 — 320×256 image, ~50 s. Same layout as PD-90; pixel time    #
    # 0.286 ms instead of 0.532 ms. VIS 93 (0x5D).                      #
    # ------------------------------------------------------------------ #
    Mode.PD_50: ModeSpec(
        name=Mode.PD_50,
        vis_code=0x5D,  # 93  — 320×256 image, ~50 s
        width=320,
        height=128,     # 256 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_50_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_90: ModeSpec(
        name=Mode.PD_90,
        vis_code=0x63,  # 99  — 320×256 image, ~90 s
        width=320,
        height=128,     # 256 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_90_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_120: ModeSpec(
        name=Mode.PD_120,
        vis_code=0x5F,  # 95  — 640×496 image, ~126 s
        width=640,
        height=248,     # 496 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_120_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_160: ModeSpec(
        name=Mode.PD_160,
        vis_code=0x62,  # 98  — 512×400 image, ~161 s
        width=512,
        height=200,     # 400 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_160_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_180: ModeSpec(
        name=Mode.PD_180,
        vis_code=0x60,  # 96  — 640×496 image, ~188 s
        width=640,
        height=248,     # 496 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_180_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_240: ModeSpec(
        name=Mode.PD_240,
        vis_code=0x61,  # 97  — 640×496 image, ~248 s
        width=640,
        height=248,     # 496 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_240_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PD_290: ModeSpec(
        name=Mode.PD_290,
        vis_code=0x5E,  # 94  — 800×616 image, ~289 s
        width=800,
        height=308,     # 616 image rows / 2
        sync_pulse_ms=_PD_SYNC_MS,
        sync_porch_ms=_PD_PORCH_MS,
        line_time_ms=_PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_290_CHANNEL_SCAN_MS,
        color_layout=("Y0", "Cr", "Cb", "Y1"),
        sync_position=SyncPosition.LINE_START,
    ),

    # ------------------------------------------------------------------ #
    # Wraase SC2 family — RGB, single 0.5 ms porch before the first      #
    # channel only; no gaps between G and B.                              #
    # ------------------------------------------------------------------ #
    Mode.WRAASE_SC2_120: ModeSpec(
        name=Mode.WRAASE_SC2_120,
        vis_code=0x3F,  # 63  — 320×256, ~122 s
        width=320,
        height=256,
        sync_pulse_ms=_WRAASE_SC2_SYNC_MS,
        sync_porch_ms=_WRAASE_SC2_PORCH_MS,
        # SYNC + PORCH (before R only) + R + G + B
        line_time_ms=(
            _WRAASE_SC2_SYNC_MS
            + _WRAASE_SC2_PORCH_MS
            + 3 * _WRAASE_SC2_120_SCAN_MS
        ),
        color_layout=("R", "G", "B"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.WRAASE_SC2_180: ModeSpec(
        name=Mode.WRAASE_SC2_180,
        vis_code=0x37,  # 55  — 320×256, ~183 s
        width=320,
        height=256,
        sync_pulse_ms=_WRAASE_SC2_SYNC_MS,
        sync_porch_ms=_WRAASE_SC2_PORCH_MS,
        line_time_ms=(
            _WRAASE_SC2_SYNC_MS
            + _WRAASE_SC2_PORCH_MS
            + 3 * _WRAASE_SC2_180_SCAN_MS
        ),
        color_layout=("R", "G", "B"),
        sync_position=SyncPosition.LINE_START,
    ),

    # ------------------------------------------------------------------ #
    # Pasokon P3 / P5 / P7 — RGB with equal inter-channel gaps.          #
    # sync_porch_ms holds the INTER_CH_GAP so that the generic decoder    #
    # can derive scan_ms as (line_time − sync − 4×gap) / 3.              #
    # VIS codes: 0x71/0x72/0x73 (PySSTV stores 0xF3 for P7 but only the  #
    # 7 LSBs = 0x73 = 115 are transmitted and detected by detect_vis).   #
    # ------------------------------------------------------------------ #
    Mode.PASOKON_P3: ModeSpec(
        name=Mode.PASOKON_P3,
        vis_code=0x71,  # 113  — 640×496, ~203 s
        width=640,
        height=496,
        sync_pulse_ms=_PASOKON_P3_SYNC_MS,
        sync_porch_ms=_PASOKON_P3_GAP_MS,   # INTER_CH_GAP (see note above)
        # SYNC + 4×GAP + 3×SCAN
        line_time_ms=(
            _PASOKON_P3_SYNC_MS
            + 4 * _PASOKON_P3_GAP_MS
            + 3 * _PASOKON_P3_SCAN_MS
        ),
        color_layout=("R", "G", "B"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PASOKON_P5: ModeSpec(
        name=Mode.PASOKON_P5,
        vis_code=0x72,  # 114  — 640×496, ~304 s
        width=640,
        height=496,
        sync_pulse_ms=_PASOKON_P5_SYNC_MS,
        sync_porch_ms=_PASOKON_P5_GAP_MS,
        line_time_ms=(
            _PASOKON_P5_SYNC_MS
            + 4 * _PASOKON_P5_GAP_MS
            + 3 * _PASOKON_P5_SCAN_MS
        ),
        color_layout=("R", "G", "B"),
        sync_position=SyncPosition.LINE_START,
    ),
    Mode.PASOKON_P7: ModeSpec(
        name=Mode.PASOKON_P7,
        vis_code=0x73,  # 115 (7 LSBs of PySSTV's 243 / 0xF3)  — 640×496, ~406 s
        width=640,
        height=496,
        sync_pulse_ms=_PASOKON_P7_SYNC_MS,
        sync_porch_ms=_PASOKON_P7_GAP_MS,
        line_time_ms=(
            _PASOKON_P7_SYNC_MS
            + 4 * _PASOKON_P7_GAP_MS
            + 3 * _PASOKON_P7_SCAN_MS
        ),
        color_layout=("R", "G", "B"),
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
