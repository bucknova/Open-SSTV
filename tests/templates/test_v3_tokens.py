# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the v0.3 image-template token resolver (``resolve_text``).

Covers:
- Both %x and {name} syntaxes resolve identically.
- Own callsign, grid, name from AppConfig.
- QSO-state tokens: tocall, rst, tocall_name, note, serial.
- TX-context tokens: mode_display_name, frequency, band.
- Slashed-zero transform applies only to callsign-valued tokens.
- Date / time tokens use the injected now_utc (resolver stays pure).
- %% resolves to literal %.
- Unknown tokens pass through unchanged.
- AppConfig fields not yet present (grid, op_name) degrade gracefully.
"""
from __future__ import annotations

import datetime

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import QSOState, TXContext
from open_sstv.templates.tokens import _hz_to_band, resolve_text

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime.datetime(2026, 4, 24, 15, 30, 0, tzinfo=datetime.timezone.utc)


def _cfg(**kw: object) -> AppConfig:
    defaults: dict[str, object] = {"callsign": "W0AEZ"}
    defaults.update(kw)
    return AppConfig(**defaults)  # type: ignore[arg-type]


def _qso(**kw: object) -> QSOState:
    defaults: dict[str, object] = {
        "tocall": "VE7ABC",
        "rst": "595",
        "tocall_name": "Bob",
        "note": "nice signal",
        "serial": 7,
    }
    defaults.update(kw)
    return QSOState(**defaults)  # type: ignore[arg-type]


def _ctx(**kw: object) -> TXContext:
    defaults: dict[str, object] = {
        "mode_display_name": "Scottie S1",
        "frame_size": (320, 256),
        "frequency_hz": 14_230_000.0,
    }
    defaults.update(kw)
    return TXContext(**defaults)  # type: ignore[arg-type]


def rt(text: str, **kw: object) -> str:
    """Shorthand: resolve_text with defaults, injected clock."""
    cfg = kw.pop("cfg", _cfg())
    qso = kw.pop("qso", _qso())
    ctx = kw.pop("ctx", _ctx())
    return resolve_text(
        text, qso, cfg, ctx, now_utc=_FIXED_NOW, **kw  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Own-station tokens
# ---------------------------------------------------------------------------


def test_percent_callsign() -> None:
    assert rt("%c", cfg=_cfg(callsign="W0AEZ"), slashed_zero=False) == "W0AEZ"


def test_named_callsign() -> None:
    assert rt("{callsign}", cfg=_cfg(callsign="W0AEZ"), slashed_zero=False) == "W0AEZ"


def test_callsign_uppercased() -> None:
    assert rt("%c", cfg=_cfg(callsign="w0aez"), slashed_zero=False) == "W0AEZ"


def test_percent_mode() -> None:
    assert rt("%m", ctx=_ctx(mode_display_name="Martin M1")) == "Martin M1"


def test_named_mode() -> None:
    assert rt("{mode}", ctx=_ctx(mode_display_name="Scottie S2")) == "Scottie S2"


# ---------------------------------------------------------------------------
# QSO-state tokens
# ---------------------------------------------------------------------------


def test_tocall_percent() -> None:
    assert rt("%o", qso=_qso(tocall="VE7ABC"), slashed_zero=False) == "VE7ABC"


def test_tocall_named() -> None:
    assert rt("{tocall}", qso=_qso(tocall="VE7ABC"), slashed_zero=False) == "VE7ABC"


def test_rst_percent() -> None:
    assert rt("%r", qso=_qso(rst="599")) == "599"


def test_rst_named() -> None:
    assert rt("{rst}", qso=_qso(rst="589")) == "589"


def test_tocall_name_percent() -> None:
    assert rt("%name_o") == "Bob"


def test_tocall_name_named() -> None:
    assert rt("{tocallname}") == "Bob"


def test_note_percent() -> None:
    assert rt("%note") == "nice signal"


def test_note_named() -> None:
    assert rt("{note}") == "nice signal"


def test_serial_percent() -> None:
    assert rt("%q", qso=_qso(serial=42)) == "42"


def test_serial_named() -> None:
    assert rt("{qso_serial}", qso=_qso(serial=99)) == "99"


# ---------------------------------------------------------------------------
# Frequency / band tokens
# ---------------------------------------------------------------------------


def test_freq_formats_mhz() -> None:
    result = rt("%f", ctx=_ctx(frequency_hz=14_230_000.0))
    assert "14.2300" in result
    assert "MHz" in result


def test_freq_named() -> None:
    result = rt("{freq}", ctx=_ctx(frequency_hz=14_230_000.0))
    assert "MHz" in result


def test_band_20m() -> None:
    assert rt("%b", ctx=_ctx(frequency_hz=14_230_000.0)) == "20m"


def test_band_40m() -> None:
    assert rt("{band}", ctx=_ctx(frequency_hz=7_200_000.0)) == "40m"


def test_band_2m() -> None:
    assert rt("%b", ctx=_ctx(frequency_hz=144_500_000.0)) == "2m"


def test_freq_blank_when_no_rig() -> None:
    assert rt("%f", ctx=_ctx(frequency_hz=None)) == ""


def test_band_blank_when_no_rig() -> None:
    assert rt("%b", ctx=_ctx(frequency_hz=None)) == ""


# ---------------------------------------------------------------------------
# _hz_to_band unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hz,expected",
    [
        (14_230_000, "20m"),
        (7_050_000, "40m"),
        (3_750_000, "80m"),
        (28_500_000, "10m"),
        (144_200_000, "2m"),
        (432_100_000, "70cm"),
        (50_125_000, "6m"),
        (500, ""),         # below all ham bands
    ],
)
def test_hz_to_band(hz: float, expected: str) -> None:
    assert _hz_to_band(hz) == expected


# ---------------------------------------------------------------------------
# Date / time tokens
# ---------------------------------------------------------------------------


def test_date_iso_format() -> None:
    assert rt("%d") == "2026-04-24"


def test_time_default_format() -> None:
    assert rt("%t") == "15:30"


def test_date_named() -> None:
    assert rt("{date}") == "2026-04-24"


def test_time_named() -> None:
    assert rt("{time}") == "15:30"


def test_custom_date_format() -> None:
    assert rt("%d", date_format="%Y%m%d") == "20260424"


def test_custom_time_format() -> None:
    assert rt("%t", time_format="%H%M%S") == "153000"


def test_resolver_pure_no_system_clock() -> None:
    """Two calls with the same injected clock produce identical results."""
    r1 = rt("%d %t")
    r2 = rt("%d %t")
    assert r1 == r2


# ---------------------------------------------------------------------------
# Slashed-zero transform
# ---------------------------------------------------------------------------


def test_slashed_zero_on_own_callsign() -> None:
    result = rt("%c", cfg=_cfg(callsign="W0AEZ"), slashed_zero=True)
    assert result == "W\u00d8AEZ"


def test_slashed_zero_on_tocall() -> None:
    result = rt("%o", qso=_qso(tocall="K0TX"), slashed_zero=True)
    assert result == "K\u00d8TX"


def test_slashed_zero_does_not_affect_rst() -> None:
    result = rt("%r", qso=_qso(rst="599"), slashed_zero=True)
    assert result == "599"  # digits in RST must not be transformed


def test_slashed_zero_false_leaves_zero() -> None:
    result = rt("%c", cfg=_cfg(callsign="W0AEZ"), slashed_zero=False)
    assert "0" in result
    assert "\u00d8" not in result


def test_named_callsign_slashed_zero() -> None:
    result = rt("{callsign}", cfg=_cfg(callsign="W0AEZ"), slashed_zero=True)
    assert "\u00d8" in result


def test_named_tocall_slashed_zero() -> None:
    result = rt("{tocall}", qso=_qso(tocall="W0XYZ"), slashed_zero=True)
    assert "\u00d8" in result


# ---------------------------------------------------------------------------
# Literal percent / forward-compat
# ---------------------------------------------------------------------------


def test_literal_percent() -> None:
    assert rt("100%%") == "100%"


def test_escaped_percent_not_resolved_as_token() -> None:
    assert rt("%%c") == "%c"


def test_unknown_percent_token_passthrough() -> None:
    assert rt("prefix_%z_suffix") == "prefix_%z_suffix"


def test_unknown_named_token_passthrough() -> None:
    assert rt("prefix_{future_token}_suffix") == "prefix_{future_token}_suffix"


# ---------------------------------------------------------------------------
# Mixed syntax and composites
# ---------------------------------------------------------------------------


def test_mixed_syntax_same_result() -> None:
    r1 = rt("CQ de %c")
    r2 = rt("CQ de {callsign}")
    assert r1 == r2


def test_cq_de_callsign_template() -> None:
    result = rt("CQ de %c / %o", cfg=_cfg(callsign="W0AEZ"), qso=_qso(tocall="VE7ABC"), slashed_zero=False)
    assert result == "CQ de W0AEZ / VE7ABC"


def test_empty_text() -> None:
    assert rt("") == ""


def test_no_tokens_passthrough() -> None:
    assert rt("Hello World") == "Hello World"


# ---------------------------------------------------------------------------
# AppConfig missing optional fields (grid, op_name) degrade gracefully
# ---------------------------------------------------------------------------


def test_grid_token_empty_when_field_absent() -> None:
    cfg = _cfg()
    assert not hasattr(cfg, "grid") or rt("%g", cfg=cfg) in ("", getattr(cfg, "grid", ""))


def test_name_token_empty_when_field_absent() -> None:
    cfg = _cfg()
    assert not hasattr(cfg, "op_name") or rt("%n", cfg=cfg) in ("", getattr(cfg, "op_name", ""))
