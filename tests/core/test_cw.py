# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.core.cw`` Morse code generator."""
from __future__ import annotations

import numpy as np
import pytest

from open_sstv.core.cw import _MORSE_TABLE, make_cw

SR = 48_000
WPM = 20


# ---------------------------------------------------------------------------
# Morse table correctness
# ---------------------------------------------------------------------------


class TestMorseTable:
    def test_all_letters_present(self) -> None:
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            assert c in _MORSE_TABLE, f"Letter {c!r} missing from Morse table"

    def test_all_digits_present(self) -> None:
        for c in "0123456789":
            assert c in _MORSE_TABLE, f"Digit {c!r} missing from Morse table"

    def test_slash_present(self) -> None:
        assert "/" in _MORSE_TABLE

    def test_hyphen_present(self) -> None:
        assert "-" in _MORSE_TABLE

    def test_patterns_only_dots_and_dashes(self) -> None:
        for char, pattern in _MORSE_TABLE.items():
            assert all(s in ".-" for s in pattern), (
                f"Invalid symbol in pattern for {char!r}: {pattern!r}"
            )
            assert len(pattern) > 0, f"Empty pattern for {char!r}"

    # Spot-check a few well-known patterns (W1AW callsign components)
    def test_w_pattern(self) -> None:
        assert _MORSE_TABLE["W"] == ".--"

    def test_1_pattern(self) -> None:
        assert _MORSE_TABLE["1"] == ".----"

    def test_a_pattern(self) -> None:
        assert _MORSE_TABLE["A"] == ".-"

    def test_slash_pattern(self) -> None:
        # ITU fraction bar: dah-dit-dit-dah-dit
        assert _MORSE_TABLE["/"] == "-..-."

    def test_zero_is_five_dahs(self) -> None:
        assert _MORSE_TABLE["0"] == "-----"


# ---------------------------------------------------------------------------
# make_cw output properties
# ---------------------------------------------------------------------------


class TestMakeCW:
    def test_dtype_is_int16(self) -> None:
        out = make_cw("E", wpm=WPM, tone_hz=800, sample_rate=SR)
        assert out.dtype == np.dtype("int16")

    def test_empty_text_returns_empty_array(self) -> None:
        out = make_cw("", wpm=WPM, tone_hz=800, sample_rate=SR)
        assert out.dtype == np.dtype("int16")
        assert len(out) == 0

    def test_unknown_char_skipped_gracefully(self) -> None:
        """Characters not in the Morse table produce no output (no crash)."""
        out = make_cw("*", wpm=WPM, tone_hz=800, sample_rate=SR)
        assert out.dtype == np.dtype("int16")
        assert len(out) == 0

    def test_no_clipping_w1aw(self) -> None:
        out = make_cw("W1AW", wpm=WPM, tone_hz=800, sample_rate=SR)
        assert int(np.abs(out).max()) < 32768

    # --- Timing ---

    def test_single_dit_length(self) -> None:
        """'E' is a single dit; total length must equal exactly 1 dit."""
        out = make_cw("E", wpm=WPM, tone_hz=800, sample_rate=SR)
        expected_dit = int(round(1.2 / WPM * SR))  # 2880 at 20 WPM / 48 kHz
        assert len(out) == expected_dit

    def test_two_e_inter_char_gap(self) -> None:
        """'EE' = dit(1) + inter-char-gap(3) + dit(1) = 5 units."""
        out = make_cw("EE", wpm=WPM, tone_hz=800, sample_rate=SR)
        dit = int(round(1.2 / WPM * SR))
        assert len(out) == 5 * dit

    def test_w1aw_timing_20wpm(self) -> None:
        """'W1AW' timing is exactly correct at 20 WPM.

        Character unit counts (on + intra gaps, no trailing intra gap):
          W  = .--  → 1+1+3+1+3 = 9
          1  = .----→ 1+1+3+1+3+1+3+1+3 = 17
          A  = .-   → 1+1+3 = 5
          W  = .--  → 9
        Inter-character gaps (3 units between each pair): 3 × 3 = 9
        Total = 9+3 + 17+3 + 5+3 + 9 = 49 units
        """
        dit = int(round(1.2 / 20 * SR))
        expected = 49 * dit
        out = make_cw("W1AW", wpm=20, tone_hz=800, sample_rate=SR)
        assert len(out) == expected

    def test_inter_word_gap(self) -> None:
        """'E E' (two words) = dit + 7-unit-gap + dit = 9 units."""
        out = make_cw("E E", wpm=WPM, tone_hz=800, sample_rate=SR)
        dit = int(round(1.2 / WPM * SR))
        assert len(out) == 9 * dit

    # --- Amplitude ---

    def test_peak_within_01_db_of_minus1_dbfs(self) -> None:
        """Peak of a long tone sequence must sit within 0.1 dB of −1 dBFS.

        We use 'EEEEEEEEEE' (10 single dits) so there are enough samples
        for the discretised sine to reach its true peak.
        """
        out = make_cw("EEEEEEEEEE", wpm=WPM, tone_hz=800, sample_rate=SR)
        target = 10 ** (-1.0 / 20.0) * 32767.0  # ≈ 29204
        peak = int(np.abs(out).max())
        tol_factor = 10 ** (-0.1 / 20.0)  # 0.1 dB below target
        assert peak <= 32767, "Signal is clipping"
        assert peak >= target * tol_factor, (
            f"Peak {peak} is more than 0.1 dB below −1 dBFS target {target:.0f}"
        )

    def test_peak_dbfs_parameter_respected(self) -> None:
        """peak_dbfs=-3 gives a peak ~3 dB below full scale."""
        out = make_cw("EEEEEEEEEE", wpm=WPM, tone_hz=800, sample_rate=SR, peak_dbfs=-3.0)
        target = 10 ** (-3.0 / 20.0) * 32767.0
        peak = int(np.abs(out).max())
        tol_factor = 10 ** (-0.5 / 20.0)  # 0.5 dB tolerance
        assert peak <= 32767
        assert peak >= target * tol_factor

    def test_different_sample_rates(self) -> None:
        """Output length scales correctly with sample rate."""
        out_44 = make_cw("E", wpm=WPM, tone_hz=800, sample_rate=44_100)
        out_48 = make_cw("E", wpm=WPM, tone_hz=800, sample_rate=48_000)
        # Ratio of lengths should match ratio of sample rates (within rounding).
        assert abs(len(out_44) / len(out_48) - 44_100 / 48_000) < 0.01

    def test_slash_encodable(self) -> None:
        """Portable suffix calls like 'W0AEZ/P' must encode without error."""
        out = make_cw("W0AEZ/P", wpm=WPM, tone_hz=800, sample_rate=SR)
        assert out.dtype == np.dtype("int16")
        assert len(out) > 0
