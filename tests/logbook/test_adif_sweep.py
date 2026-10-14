"""v0.6.14 sweep: ADIF MODE/SUBMODE round-trip regressions and UTF-8 policy."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from open_sstv.logbook.adif import (
    ADIF_MODES,
    adif_mode_fields,
    format_qso_record,
    import_adif,
    mode_to_submode,
)
from open_sstv.logbook.coordinator import LogbookCoordinator
from open_sstv.logbook.model import QSO

_T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _q(**kw: object) -> QSO:
    base: dict[str, object] = {"direction": "TX", "callsign": "W0AEZ", "time_utc": _T}
    base.update(kw)
    return QSO(**base)  # type: ignore[arg-type]


def _rec(mode: str, submode: str = "") -> str:
    parts = [
        "<CALL:5>W0AEZ", "<QSO_DATE:8>20261013", "<TIME_ON:4>1200",
        f"<MODE:{len(mode)}>{mode}",
    ]
    if submode:
        parts.append(f"<SUBMODE:{len(submode)}>{submode}")
    return " ".join(parts) + " <EOR>"


def _roundtrip(mode: str, submode: str = "") -> tuple[str, str]:
    table = LogbookCoordinator(lambda: None).mode_table()
    (qso,) = import_adif(_rec(mode, submode), mode_table=table)
    return adif_mode_fields(qso.mode)


@pytest.mark.parametrize(
    "submode",
    ["SC2-180", "P3", "MP73-N", "Martin1", "Martin 1", "M1", "Robot 36C", "AVT 24"],
)
def test_foreign_sstv_submodes_round_trip_as_sstv(submode: str) -> None:
    mode, sub = _roundtrip("SSTV", submode)
    assert mode == "SSTV"
    assert sub == mode_to_submode(submode)


@pytest.mark.parametrize(
    ("mode", "submode", "expected"),
    [
        ("FT8", "", ("FT8", "")),
        ("PSK", "PSK250", ("PSK", "PSK250")),
        ("MFSK", "FT4", ("MFSK", "FT4")),
        ("SSB", "USB", ("SSB", "USB")),
        ("SSTV", "", ("SSTV", "")),
        ("SSTV", "MartinM1", ("SSTV", "MartinM1")),
        # Non-SSTV parent with a submode we don't know: keep the parent
        # rather than mis-filing it as an SSTV submode.
        ("PSK", "PSK9999X", ("PSK", "")),
    ],
)
def test_other_modes_round_trip(mode: str, submode: str, expected: tuple[str, str]) -> None:
    assert _roundtrip(mode, submode) == expected


@pytest.mark.parametrize("mode", ["Martin1", "P3", "Robot 36C", "something odd"])
def test_unknown_names_export_as_sstv_submode(mode: str) -> None:
    assert adif_mode_fields(mode) == ("SSTV", mode_to_submode(mode))


def test_exported_mode_is_always_in_adif_enumeration() -> None:
    for mode in ("SC2-180", "M1", "FT8", "PSK250", "FT4", "USB", "Martin M1", "zzz"):
        assert adif_mode_fields(mode)[0] in ADIF_MODES


def test_adif_mode_enumeration_core_members() -> None:
    for m in ("SSTV", "FT8", "SSB", "CW", "PSK", "MFSK", "RTTY", "FM", "AM"):
        assert m in ADIF_MODES
    # Submodes are not modes.
    for m in ("FT4", "USB", "PSK31", "JS8"):
        assert m not in ADIF_MODES


# --- Non-ASCII policy: UTF-8 with byte-count lengths ---------------------------


def test_utf8_name_uses_byte_length_and_round_trips() -> None:
    name = "Jörg Ñoño"  # 9 chars, 12 UTF-8 bytes
    rec = format_qso_record(_q(name=name, qth="Zürich"))
    assert f"<NAME:{len(name.encode('utf-8'))}>{name}" in rec
    assert "<NAME:12>" in rec
    assert "<QTH:7>Zürich" in rec
    assert "_INTL" not in rec  # ADI files may not carry *_INTL fields
    (back,) = import_adif(rec.encode("utf-8"))
    assert back.name == name and back.qth == "Zürich"
