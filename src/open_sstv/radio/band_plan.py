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


def mode_family(mode: str) -> str:
    """Return the sideband family of a rig-reported mode string.

    Used by band-plan tuning so a user's data-variant mode (IC-7300
    ``USB-D``, Yaesu ``USB-DATA``, Kenwood / Hamlib ``PKTUSB``, …) is
    preserved when the band-plan target's sideband family matches what
    they're already on.  Without this, every band-plan pick would clobber
    the rig's data-IN routing and re-enable the speech processor — which
    breaks SSTV TX immediately on Icom rigs.

    Family classification:

    * ``"USB"`` — anything containing ``"USB"`` (case-insensitive) or the
      single-character ``"U"`` some rigs report.
      Covers: USB, USB-D, USB-D1/2/3, USB-DATA, PKTUSB.
    * ``"LSB"`` — symmetric; covers LSB, LSB-D, PKTLSB, etc.
    * ``"FM"`` — anything containing ``"FM"``: FM, FM-N, PKTFM.
    * Anything else is returned uppercased + stripped (CW, AM, RTTY,
      empty string, …).  The band-plan tune treats it as a distinct
      family, so picking a USB / LSB / FM band from CW correctly
      switches modes.

    The substring match is deliberate so unfamiliar variant names
    (e.g. a future ``"USB-D4"``) classify correctly without a code
    change.  Rigs reporting truly opaque mode names (e.g. K3's
    ``"DATA-A"`` over direct serial CAT, which is upper-sideband data
    but doesn't contain ``"USB"``) will fall through to a mode change;
    report any such case and we'll extend the matcher.
    """
    m = mode.upper().strip()
    if not m:
        return ""
    if "USB" in m or m == "U":
        return "USB"
    if "LSB" in m or m == "L":
        return "LSB"
    if "FM" in m:
        return "FM"
    return m


__all__ = ["BandEntry", "SSTV_BAND_PLAN", "mode_family", "primary_entry"]
