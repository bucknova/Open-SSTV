# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.radio.band_plan``.

Hardware-free and display-free — no Qt, no WebSocket, no serial port.
All tests exercise the pure data layer only.
"""
from __future__ import annotations

import pytest

from open_sstv.radio.band_plan import (
    DATA_MODE_BY_PROTOCOL,
    SSTV_BAND_PLAN,
    BandEntry,
    mode_family,
    primary_entry,
    resolve_tune_mode,
)

# ---------------------------------------------------------------------------
# Data integrity
# ---------------------------------------------------------------------------

class TestBandPlanData:
    def test_plan_is_non_empty(self) -> None:
        assert len(SSTV_BAND_PLAN) >= 1

    def test_all_entries_have_positive_freq(self) -> None:
        for entry in SSTV_BAND_PLAN:
            assert entry.freq_hz > 0, f"{entry.label!r} has non-positive freq"

    def test_all_entries_have_valid_mode(self) -> None:
        valid = {"USB", "LSB", "FM"}
        for entry in SSTV_BAND_PLAN:
            assert entry.rig_mode in valid, (
                f"{entry.label!r} has unknown mode {entry.rig_mode!r}"
            )

    def test_all_entries_have_non_empty_label(self) -> None:
        for entry in SSTV_BAND_PLAN:
            assert entry.label.strip(), "Found an entry with a blank label"

    def test_all_passband_hz_are_non_negative(self) -> None:
        for entry in SSTV_BAND_PLAN:
            assert entry.passband_hz >= 0, (
                f"{entry.label!r} has negative passband"
            )

    def test_exactly_one_primary_entry(self) -> None:
        primaries = [e for e in SSTV_BAND_PLAN if e.primary]
        assert len(primaries) == 1, (
            f"Expected exactly one primary entry, got {len(primaries)}"
        )

    def test_no_duplicate_frequencies(self) -> None:
        freqs = [e.freq_hz for e in SSTV_BAND_PLAN]
        assert len(freqs) == len(set(freqs)), "Duplicate frequencies in band plan"


# ---------------------------------------------------------------------------
# Known frequencies — regression guards
# ---------------------------------------------------------------------------

class TestKnownFrequencies:
    """Guard that well-known calling frequencies stay in the plan and have
    the correct mode.  These are stable IARU/ARRL data points."""

    def _entry(self, freq_hz: int) -> BandEntry:
        for e in SSTV_BAND_PLAN:
            if e.freq_hz == freq_hz:
                return e
        raise AssertionError(f"{freq_hz} Hz not found in SSTV_BAND_PLAN")

    def test_20m_primary_is_14230_usb(self) -> None:
        e = self._entry(14_230_000)
        assert e.rig_mode == "USB"
        assert e.primary is True

    def test_20m_primary_is_hf(self) -> None:
        e = self._entry(14_230_000)
        assert e.region == "HF"

    def test_40m_is_lsb(self) -> None:
        # 40m is below 10 MHz — convention is LSB.
        e = self._entry(7_171_000)
        assert e.rig_mode == "LSB"

    def test_80m_is_lsb(self) -> None:
        e = self._entry(3_733_000)
        assert e.rig_mode == "LSB"

    def test_2m_is_fm(self) -> None:
        e = self._entry(144_500_000)
        assert e.rig_mode == "FM"
        assert e.region == "VHF"

    def test_10m_is_usb(self) -> None:
        e = self._entry(28_680_000)
        assert e.rig_mode == "USB"

    def test_15m_is_usb(self) -> None:
        e = self._entry(21_340_000)
        assert e.rig_mode == "USB"


# ---------------------------------------------------------------------------
# primary_entry() helper
# ---------------------------------------------------------------------------

class TestPrimaryEntry:
    def test_returns_a_band_entry(self) -> None:
        assert isinstance(primary_entry(), BandEntry)

    def test_primary_flag_is_set(self) -> None:
        assert primary_entry().primary is True

    def test_is_20m_usb(self) -> None:
        e = primary_entry()
        assert e.freq_hz == 14_230_000
        assert e.rig_mode == "USB"


# ---------------------------------------------------------------------------
# BandEntry immutability
# ---------------------------------------------------------------------------

class TestBandEntryFrozen:
    def test_cannot_mutate_freq(self) -> None:
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            primary_entry().freq_hz = 0  # type: ignore[misc]

    def test_cannot_mutate_mode(self) -> None:
        with pytest.raises(Exception):
            primary_entry().rig_mode = "AM"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# mode_family() — sideband-family classifier for band-plan tuning
# ---------------------------------------------------------------------------

class TestModeFamily:
    """The family check lets band-plan tuning preserve a user's data-variant
    mode (USB-D, USB-DATA, PKTUSB, …) when the target band's sideband matches
    what they're already on, while still switching at band-edge crossings.
    """

    # --- USB family ---------------------------------------------------------

    def test_plain_usb_is_usb_family(self) -> None:
        assert mode_family("USB") == "USB"

    def test_icom_usb_d_is_usb_family(self) -> None:
        """IC-7300 USB-D (USB Data) — the canonical SSTV-on-Icom mode."""
        assert mode_family("USB-D") == "USB"

    def test_icom_usb_d_variants_are_usb_family(self) -> None:
        """IC-7610 / IC-9700 sometimes report USB-D1 / USB-D2 / USB-D3."""
        for variant in ("USB-D1", "USB-D2", "USB-D3"):
            assert mode_family(variant) == "USB", variant

    def test_yaesu_usb_data_is_usb_family(self) -> None:
        """FTDX10 / FTDX101 report USB-DATA."""
        assert mode_family("USB-DATA") == "USB"

    def test_hamlib_pktusb_is_usb_family(self) -> None:
        """Hamlib's RIG_MODE_PKTUSB — covers Kenwood, Yaesu PKT modes."""
        assert mode_family("PKTUSB") == "USB"

    def test_single_char_u_is_usb_family(self) -> None:
        """Some rigs report just 'U' for USB."""
        assert mode_family("U") == "USB"

    def test_lowercase_usb_is_usb_family(self) -> None:
        assert mode_family("usb") == "USB"

    def test_usb_with_whitespace_is_usb_family(self) -> None:
        assert mode_family("  USB  ") == "USB"

    # --- LSB family ---------------------------------------------------------

    def test_plain_lsb_is_lsb_family(self) -> None:
        assert mode_family("LSB") == "LSB"

    def test_icom_lsb_d_is_lsb_family(self) -> None:
        assert mode_family("LSB-D") == "LSB"

    def test_hamlib_pktlsb_is_lsb_family(self) -> None:
        assert mode_family("PKTLSB") == "LSB"

    def test_single_char_l_is_lsb_family(self) -> None:
        assert mode_family("L") == "LSB"

    # --- FM family ----------------------------------------------------------

    def test_plain_fm_is_fm_family(self) -> None:
        assert mode_family("FM") == "FM"

    def test_fm_narrow_is_fm_family(self) -> None:
        assert mode_family("FM-N") == "FM"

    def test_hamlib_pktfm_is_fm_family(self) -> None:
        assert mode_family("PKTFM") == "FM"

    # --- Distinct families pass through ------------------------------------

    # --- M6: explicit DATA / PSK / FT8 aliases without a sideband substring ---

    def test_k3_data_a_classifies_as_usb(self) -> None:
        """Elecraft K3 reports `DATA-A` (upper-sideband data) over direct
        serial CAT — doesn't contain "USB" but is functionally USB-family.
        Without the alias, every band-plan pick on a K3 would clobber
        the DATA mode."""
        assert mode_family("DATA-A") == "USB"

    def test_k3_data_b_classifies_as_lsb(self) -> None:
        """K3 `DATA-B` is lower-sideband data."""
        assert mode_family("DATA-B") == "LSB"

    def test_yaesu_data_u_classifies_as_usb(self) -> None:
        """Yaesu FTDX10/FTDX101 report `DATA-U` (alongside the older
        `USB-DATA`); covered by the alias as well as a fallback."""
        assert mode_family("DATA-U") == "USB"

    def test_yaesu_data_l_classifies_as_lsb(self) -> None:
        assert mode_family("DATA-L") == "LSB"

    def test_psk_u_classifies_as_usb(self) -> None:
        assert mode_family("PSK-U") == "USB"

    def test_ft8_l_classifies_as_lsb(self) -> None:
        assert mode_family("FT8-L") == "LSB"

    # --- distinct families pass through ---

    def test_cw_is_distinct_family(self) -> None:
        """CW must NOT classify as USB / LSB / FM — picking a USB band from
        CW should trigger a mode change."""
        assert mode_family("CW") == "CW"
        assert mode_family("CW") != "USB"

    def test_am_is_distinct_family(self) -> None:
        assert mode_family("AM") == "AM"

    def test_rtty_is_distinct_family(self) -> None:
        assert mode_family("RTTY") == "RTTY"

    def test_empty_string_returns_empty(self) -> None:
        """An empty current-mode (rig returned nothing / get_mode failed)
        must not collide with any real family, so the tune triggers
        set_mode as a safe default."""
        assert mode_family("") == ""
        assert mode_family("") != "USB"

    # --- Band-plan tune use case (the whole reason this exists) -------------

    def test_usb_d_to_usb_target_same_family_preserves_data_variant(self) -> None:
        """The IC-7300 USB-D scenario: user is on USB-D, picks 20 m
        (target USB).  Families match → tune must NOT call set_mode."""
        current = "USB-D"
        target = "USB"
        assert mode_family(current) == mode_family(target)

    def test_usb_to_lsb_target_different_family_switches(self) -> None:
        """User on 20 m USB picks 40 m LSB.  Families differ → tune DOES
        call set_mode (sideband flip is expected at the band-edge)."""
        current = "USB"
        target = "LSB"
        assert mode_family(current) != mode_family(target)

    def test_pktusb_to_usb_target_same_family_preserves_pkt(self) -> None:
        """Hamlib-mediated PKTUSB → picking a USB band must preserve PKTUSB."""
        assert mode_family("PKTUSB") == mode_family("USB")


# ---------------------------------------------------------------------------
# resolve_tune_mode() — WSJT-X-style None/Voice/Data tune-mode policy
# ---------------------------------------------------------------------------

class TestResolveTuneMode:
    """The band-plan entry's mode is always a plain "USB"/"LSB"/"FM" literal;
    ``resolve_tune_mode`` translates it per the user's SSTV-mode policy
    before it's ever sent to ``Rig.set_mode()``.
    """

    # --- "none" policy: never touch the rig's mode -------------------------

    def test_none_policy_returns_empty_string(self) -> None:
        assert resolve_tune_mode("USB", "Yaesu CAT", "none") == ""

    def test_none_policy_ignores_protocol(self) -> None:
        """"none" always skips set_mode, regardless of protocol."""
        assert resolve_tune_mode("LSB", "Icom CI-V", "none") == ""

    # --- "voice" policy: today's behavior, unchanged ------------------------

    def test_voice_policy_passes_through_usb(self) -> None:
        assert resolve_tune_mode("USB", "Yaesu CAT", "voice") == "USB"

    def test_voice_policy_passes_through_fm(self) -> None:
        assert resolve_tune_mode("FM", "Yaesu CAT", "voice") == "FM"

    # --- "data" policy: Yaesu is the one verified mapping -------------------

    def test_data_policy_resolves_yaesu_usb_to_data_u(self) -> None:
        assert resolve_tune_mode("USB", "Yaesu CAT", "data") == "DATA-U"

    def test_data_policy_resolves_yaesu_lsb_to_data_l(self) -> None:
        assert resolve_tune_mode("LSB", "Yaesu CAT", "data") == "DATA-L"

    def test_data_policy_fm_has_no_variant_and_passes_through(self) -> None:
        assert resolve_tune_mode("FM", "Yaesu CAT", "data") == "FM"

    def test_data_policy_falls_back_to_voice_for_unmapped_protocol(self) -> None:
        """Kenwood/Icom/PTT-only have no verified data-mode CAT string —
        "data" must not invent one, it falls back to the plain literal."""
        assert resolve_tune_mode("USB", "Kenwood / Elecraft", "data") == "USB"
        assert resolve_tune_mode("USB", "Icom CI-V", "data") == "USB"
        assert resolve_tune_mode("USB", "PTT Only (DTR/RTS)", "data") == "USB"

    def test_data_policy_falls_back_to_voice_for_unknown_protocol_string(self) -> None:
        assert resolve_tune_mode("USB", "Some Future Rig", "data") == "USB"

    def test_data_map_only_lists_verified_protocols(self) -> None:
        """Guard against silently "supporting" a protocol whose data-mode
        CAT command was never verified against real hardware."""
        assert set(DATA_MODE_BY_PROTOCOL) == {"Yaesu CAT"}
