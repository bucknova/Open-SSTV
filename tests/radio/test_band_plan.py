# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.radio.band_plan``.

Hardware-free and display-free — no Qt, no WebSocket, no serial port.
All tests exercise the pure data layer only.
"""
from __future__ import annotations

import pytest

from open_sstv.radio.band_plan import BandEntry, SSTV_BAND_PLAN, primary_entry


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
