# SPDX-License-Identifier: GPL-3.0-or-later
"""SSTV calling-frequency band plan.

Contains the internationally recognised SSTV calling frequencies so the
UI can offer a one-click "go to SSTV frequency" helper without embedding
magic numbers in widget code.

References
----------
- IARU Region 1 Band Plan (2023)
- ARRL Band Plan
- OH2AQ SSTV frequency list (http://www.kolumbus.fi/oh2aq/sstv/)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BandEntry:
    """A single SSTV calling frequency entry.

    Attributes
    ----------
    label:
        Human-readable description shown in the band-plan menu, e.g.
        ``"20m — 14.230 MHz (USB)"``.
    freq_hz:
        Carrier frequency in Hz.
    rig_mode:
        Mode string accepted by the ``Rig.set_mode`` protocol: ``"USB"``,
        ``"LSB"``, or ``"FM"``.
    passband_hz:
        Recommended IF passband in Hz (0 = leave the rig's current
        passband unchanged).
    primary:
        ``True`` for the single most-active calling frequency (20m USB).
        Used by the UI to mark it with a star and keep it at the top of
        the menu.
    region:
        Informal region tag — ``"HF"``, ``"VHF"``, or ``"UHF"``.  Used
        to insert separators between groups in the menu.
    """

    label: str
    freq_hz: int
    rig_mode: str
    passband_hz: int
    primary: bool = False
    region: str = "HF"


# ---------------------------------------------------------------------------
# SSTV calling frequencies
# ---------------------------------------------------------------------------
# Frequencies marked ★ are the primary calling / activity centres.
# LSB is conventional below 10 MHz on HF; USB above.

SSTV_BAND_PLAN: list[BandEntry] = [
    # ---- HF ----------------------------------------------------------------
    BandEntry(
        label="20m — 14.230 MHz ★",
        freq_hz=14_230_000,
        rig_mode="USB",
        passband_hz=2_700,
        primary=True,
        region="HF",
    ),
    BandEntry(
        label="20m — 14.227 MHz (EU alt)",
        freq_hz=14_227_000,
        rig_mode="USB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="15m — 21.340 MHz",
        freq_hz=21_340_000,
        rig_mode="USB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="17m — 18.160 MHz",
        freq_hz=18_160_000,
        rig_mode="USB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="10m — 28.680 MHz",
        freq_hz=28_680_000,
        rig_mode="USB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="40m — 7.171 MHz",
        freq_hz=7_171_000,
        rig_mode="LSB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="40m — 7.165 MHz (EU)",
        freq_hz=7_165_000,
        rig_mode="LSB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="80m — 3.733 MHz",
        freq_hz=3_733_000,
        rig_mode="LSB",
        passband_hz=2_700,
        region="HF",
    ),
    BandEntry(
        label="80m — 3.740 MHz (EU)",
        freq_hz=3_740_000,
        rig_mode="LSB",
        passband_hz=2_700,
        region="HF",
    ),
    # ---- VHF ---------------------------------------------------------------
    BandEntry(
        label="2m — 144.500 MHz",
        freq_hz=144_500_000,
        rig_mode="FM",
        passband_hz=0,
        region="VHF",
    ),
    BandEntry(
        label="2m — 145.500 MHz (EU)",
        freq_hz=145_500_000,
        rig_mode="FM",
        passband_hz=0,
        region="VHF",
    ),
    # ---- UHF ---------------------------------------------------------------
    BandEntry(
        label="70cm — 430.100 MHz",
        freq_hz=430_100_000,
        rig_mode="FM",
        passband_hz=0,
        region="UHF",
    ),
]


def primary_entry() -> BandEntry:
    """Return the primary SSTV calling frequency (20m 14.230 MHz USB)."""
    for entry in SSTV_BAND_PLAN:
        if entry.primary:
            return entry
    # Fallback — should never happen unless the data table is emptied.
    return SSTV_BAND_PLAN[0]


__all__ = ["BandEntry", "SSTV_BAND_PLAN", "primary_entry"]
