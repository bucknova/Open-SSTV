# SPDX-License-Identifier: GPL-3.0-or-later
"""Token / band / glyph regressions from the v0.6.14 audit (section 4 Lows)."""
from __future__ import annotations

import datetime

import pytest

from open_sstv.bands import hz_to_band
from open_sstv.config.schema import AppConfig
from open_sstv.logbook import adif
from open_sstv.templates.fonts import resolve_font_path
from open_sstv.templates.model import QSOState, TXContext
from open_sstv.templates.renderer import (
    _font_has_glyph,
    _load_font,
    _substitute_missing_glyphs,
)
from open_sstv.templates.tokens import (
    TokenContext,
    _hz_to_band,
    resolve_text,
    resolve_tokens,
)

_NOW = datetime.datetime(2026, 10, 13, 12, 34, 56, tzinfo=datetime.UTC)


def _rt(text: str, **qso: object) -> str:
    return resolve_text(
        text, QSOState(**qso), AppConfig(callsign="W0AEZ"), TXContext(),
        now_utc=_NOW, slashed_zero=False,
    )


# --- single-pass substitution -------------------------------------------------


def test_substituted_value_is_not_expanded_again() -> None:
    assert _rt("%note", note="{callsign}") == "{callsign}"
    assert _rt("{note}", note="%c") == "%c"


def test_filename_tokens_single_pass() -> None:
    ctx = TokenContext(callsign="{mode}", mode="Scottie-S1")
    assert resolve_tokens("%c_%m", ctx) == "{mode}_Scottie-S1"


def test_percent_escape_still_works() -> None:
    assert _rt("100%% %c") == "100% W0AEZ"
    assert resolve_tokens("%%c", TokenContext(callsign="X")) == "%c"


# --- \n escape ------------------------------------------------------------------


def test_backslash_n_is_a_line_break() -> None:
    assert _rt(r"CQ\nde %c") == "CQ\nde W0AEZ"


def test_backslash_n_inside_a_value_is_literal() -> None:
    assert _rt("%note", note=r"a\nb") == r"a\nb"


# --- %ts / %rx_tx / %q -------------------------------------------------------------


def test_ts_and_direction_tokens() -> None:
    epoch = str(int(_NOW.timestamp()))
    assert _rt("%ts {timestamp}") == f"{epoch} {epoch}"
    assert _rt("%rx_tx {direction}") == "TX TX"


def test_qso_serial_token() -> None:
    assert _rt("%q {qso_serial}", serial=7) == "7 7"


# --- one band table --------------------------------------------------------------


@pytest.mark.parametrize(
    ("hz", "band"),
    [
        (14_230_000, "20m"),
        (7_171_000, "40m"),
        (7_500_000, None),     # above 40 m — used to say "40m"
        (450_500_000, None),   # above 70 cm
        (3_000_000_000, None),
        (0, None),
        (float("nan"), None),
    ],
)
def test_shared_band_table(hz: float, band: str | None) -> None:
    assert hz_to_band(hz) == band
    assert _hz_to_band(hz) == (band or "")
    if hz and hz == hz:
        assert adif.hz_to_band(int(hz)) == band


# --- Ø glyph fallback -----------------------------------------------------------


def test_font_without_slashed_zero_falls_back_to_zero() -> None:
    try:
        resolve_font_path("Orbitron Bold")
    except Exception:  # noqa: BLE001
        pytest.skip("Orbitron not bundled")
    orbitron = _load_font("Orbitron Bold", 40)
    dejavu = _load_font("DejaVu Sans Bold", 40)
    assert _font_has_glyph(dejavu, "Ø")
    if _font_has_glyph(orbitron, "Ø"):
        pytest.skip("this Orbitron build has Ø")
    assert _substitute_missing_glyphs(orbitron, "WØAEZ") == "W0AEZ"
    assert _substitute_missing_glyphs(dejavu, "WØAEZ") == "WØAEZ"
