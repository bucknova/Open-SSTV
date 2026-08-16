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

    * ``"USB"`` — anything containing ``"USB"`` (USB, USB-D, USB-D1/2/3,
      USB-DATA, PKTUSB), explicit upper-data variants from Yaesu / K3
      (``"DATA-U"``, ``"DATA-A"``, ``"PSK-U"``, ``"FT8-U"``), or the
      single-character ``"U"`` some rigs report.
    * ``"LSB"`` — symmetric: LSB / LSB-D / PKTLSB, plus ``"DATA-L"``,
      ``"DATA-B"`` (K3 lower-data), ``"PSK-L"``, ``"FT8-L"``, ``"L"``.
    * ``"FM"`` — anything containing ``"FM"``: FM, FM-N, PKTFM,
      DATA-FM.
    * Anything else is returned uppercased + stripped (CW, AM, RTTY,
      empty string, …).  The band-plan tune treats it as a distinct
      family, so picking a USB / LSB / FM band from CW correctly
      switches modes.

    M6: extended the matcher to recognise explicit ``DATA-U`` /
    ``DATA-A`` style mode strings reported by Elecraft K3 over direct
    serial CAT (PKTUSB via Hamlib was already covered) and the
    ``PSK-*`` / ``FT8-*`` strings some rigs emit for those operating
    modes.  Substring match for ``"USB"`` / ``"LSB"`` / ``"FM"`` stays
    in place so unfamiliar variant names still classify if they happen
    to contain the sideband substring; the explicit alias map covers
    the strings that don't.
    """
    m = mode.upper().strip()
    if not m:
        return ""
    # Explicit aliases for mode strings that don't contain a sideband
    # substring (K3 DATA-A/DATA-B, PSK-U/L, FT8-U/L on some firmware).
    _USB_ALIASES = {"DATA-U", "DATA-A", "PSK-U", "FT8-U", "U"}
    _LSB_ALIASES = {"DATA-L", "DATA-B", "PSK-L", "FT8-L", "L"}
    if m in _USB_ALIASES or "USB" in m:
        return "USB"
    if m in _LSB_ALIASES or "LSB" in m:
        return "LSB"
    if "FM" in m:
        return "FM"
    return m


#: Per-protocol CAT mode strings for the "data" tune policy, keyed by the
#: ``rig_serial_protocol`` name (see ``serial_rig.SERIAL_RIG_PROTOCOLS``)
#: and then by sideband family (``mode_family()`` output).  Only protocols
#: with a verified single-command data-mode selector are listed here —
#: ``YaesuRig.set_mode()`` already accepts ``"DATA-U"``/``"DATA-L"``
#: directly.  Icom's data mode is not a mode-select byte at all (base
#: sideband plus a separate CI-V ``0x1A 0x06`` "DATA ON" sub-command) and
#: Kenwood/Elecraft's is model-specific (e.g. the K3 uses a separate ``DT``
#: sub-command) — both are intentionally left out and fall back to
#: ``"voice"`` in ``resolve_tune_mode`` rather than guess a wrong CAT
#: string untested against real hardware.
DATA_MODE_BY_PROTOCOL: dict[str, dict[str, str]] = {
    "Yaesu CAT": {"USB": "DATA-U", "LSB": "DATA-L"},
}


def resolve_tune_mode(rig_mode: str, protocol: str, policy: str) -> str:
    """Resolve the actual ``Rig.set_mode()`` target for a band-plan tune.

    Mirrors WSJT-X's rig "Mode" setting (None / USB / Data-Pkt) so users
    don't have to know or type vendor-specific CAT mode strings themselves.

    * ``"none"`` — return ``""``; ``_RigPollWorker.tune()`` treats an empty
      mode as "don't touch the rig's mode at all", for operators who
      already have their own data mode dialed in.
    * ``"voice"`` — return *rig_mode* unchanged (today's behavior: the
      band-plan entry's plain ``"USB"``/``"LSB"``/``"FM"`` literal).
    * ``"data"`` — look up *protocol*'s data-mode variant for *rig_mode*'s
      sideband family in ``DATA_MODE_BY_PROTOCOL``.  ``"FM"`` has no data
      variant and always passes through unchanged.  A protocol/family
      combination with no known mapping (Kenwood, Icom, PTT-only, or any
      unrecognised *protocol* string) falls back to the voice literal —
      the same value used today — so an unsupported "data" request never
      picks a worse mode than before, it just doesn't improve on it.
    """
    if policy == "none":
        return ""
    if policy == "data":
        variant = DATA_MODE_BY_PROTOCOL.get(protocol, {}).get(mode_family(rig_mode))
        if variant:
            return variant
    return rig_mode


__all__ = [
    "BandEntry",
    "DATA_MODE_BY_PROTOCOL",
    "SSTV_BAND_PLAN",
    "mode_family",
    "primary_entry",
    "resolve_tune_mode",
]
