# SPDX-License-Identifier: GPL-3.0-or-later
"""ADIF / logbook regressions from the v0.6.14 audit (C-M8, S-L8)."""
from __future__ import annotations

import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_sstv.logbook.adif import (
    adif_mode_fields,
    export_adif,
    format_qso_record,
    import_adif,
    mhz_str_to_hz,
)
from open_sstv.logbook.coordinator import LogbookCoordinator
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import LogbookStore
from open_sstv.logbook.udp_log import UdpQsoLogger

_T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _q(**kw: object) -> QSO:
    base: dict[str, object] = {"direction": "TX", "callsign": "W0AEZ", "time_utc": _T}
    base.update(kw)
    return QSO(**base)  # type: ignore[arg-type]


# --- C-M8: MODE / SUBMODE ----------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("Martin M1", ("SSTV", "MartinM1")),   # unchanged vs earlier exports
        ("Scottie 1", ("SSTV", "Scottie1")),
        ("PD 120", ("SSTV", "PD120")),
        ("Robot 72", ("SSTV", "Robot72")),     # SSTV mode we don't transmit
        ("SSTV", ("SSTV", "")),
        ("", ("SSTV", "")),
        ("FT8", ("FT8", "")),
        ("ft4", ("MFSK", "FT4")),
        ("USB", ("SSB", "USB")),
        ("CW", ("CW", "")),
    ],
)
def test_adif_mode_fields(mode: str, expected: tuple[str, str]) -> None:
    assert adif_mode_fields(mode) == expected


def test_non_sstv_mode_is_not_written_as_sstv_submode() -> None:
    rec = format_qso_record(_q(mode="FT8"))
    assert "<MODE:3>FT8" in rec
    assert "SUBMODE" not in rec
    assert "SSTV" not in rec


def test_sstv_mode_round_trips() -> None:
    rec = format_qso_record(_q(mode="Martin M1", frequency_hz=14_230_000))
    assert "<MODE:4>SSTV" in rec and "<SUBMODE:8>MartinM1" in rec
    back = import_adif(rec, mode_table=LogbookCoordinator(lambda: None).mode_table())
    assert back[0].mode == "Martin M1"


# --- C-M8: BAND round-trip ----------------------------------------------------


def test_band_without_freq_survives_import_and_export(tmp_path: Path) -> None:
    text = "<CALL:5>W0AEZ<QSO_DATE:8>20261013<TIME_ON:4>1200<MODE:4>SSTV<BAND:3>20M<EOR>"
    (qso,) = import_adif(text)
    assert qso.frequency_hz is None
    assert qso.band == "20m"
    with LogbookStore(tmp_path / "log.db") as store:
        stored = store.insert(qso)
        assert store.get(stored.id).band == "20m"
    assert "<BAND:3>20m" in export_adif([qso])


def test_freq_derived_band_wins_over_bad_band() -> None:
    text = (
        "<CALL:5>W0AEZ<QSO_DATE:8>20261013<TIME_ON:4>1200<MODE:4>SSTV"
        "<FREQ:6>14.230<BAND:3>40m<EOR>"
    )
    (qso,) = import_adif(text)
    assert qso.band == "20m"


def test_unknown_band_is_ignored() -> None:
    text = "<CALL:5>W0AEZ<QSO_DATE:8>20261013<TIME_ON:4>1200<BAND:5>bogus<EOR>"
    (qso,) = import_adif(text)
    assert qso.band == ""


def test_existing_v1_db_gains_band_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with LogbookStore(db) as store:
        store.insert(_q(mode="Martin M1"))
    # Simulate a DB created before the column existed.
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE qsos DROP COLUMN band")
    conn.commit()
    conn.close()
    with LogbookStore(db) as store:
        assert store.schema_version == 1  # additive: no downgrade lockout
        (row,) = store.list_qsos()
        assert row.band == ""
        store.insert(_q(band="6m"))


# --- S-L8: FREQ parsing ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "1e300", "-14.2", "0", "300001"])
def test_bad_freq_parses_as_none(bad: str) -> None:
    assert mhz_str_to_hz(bad) is None


def test_one_bad_record_does_not_abort_import(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "<CALL:5>AAAAA<QSO_DATE:8>20261013<TIME_ON:4>1200<FREQ:3>inf<EOR>"
        "<CALL:5>BBBBB<QSO_DATE:8>20261013<TIME_ON:4>1201<FREQ:5>1e300<EOR>"
        "<CALL:5>CCCCC<QSO_DATE:8>20261013<TIME_ON:4>1202<FREQ:6>14.230<EOR>"
    )
    qsos = import_adif(text)
    assert [q.callsign for q in qsos] == ["AAAAA", "BBBBB", "CCCCC"]
    assert qsos[0].frequency_hz is None and qsos[2].frequency_hz == 14_230_000

    # Any unexpected per-record failure skips only that record.
    import open_sstv.logbook.adif as adif_mod

    real = adif_mod._build_qso

    def flaky(record, **kw):  # type: ignore[no-untyped-def]
        if record.get("CALL") == "BBBBB":
            raise RuntimeError("boom")
        return real(record, **kw)

    monkeypatch.setattr(adif_mod, "_build_qso", flaky)
    assert [q.callsign for q in import_adif(text)] == ["AAAAA", "CCCCC"]


# --- UDP: getaddrinfo + SO_BROADCAST -------------------------------------------


def test_udp_logger_reaches_ipv6_listener() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        rx = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        rx.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback unavailable")
    rx.settimeout(2.0)
    try:
        UdpQsoLogger("::1", rx.getsockname()[1], format="adif").log_qso(_q(mode="Martin M1"))
        data, _ = rx.recvfrom(65535)
    finally:
        rx.close()
    assert b"<CALL:5>W0AEZ" in data


def test_udp_logger_enables_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    opts: list[tuple[int, int, int]] = []
    real_setsockopt = socket.socket.setsockopt

    def spy(self, level, opt, value):  # type: ignore[no-untyped-def]
        opts.append((level, opt, value))
        return real_setsockopt(self, level, opt, value)

    monkeypatch.setattr(socket.socket, "setsockopt", spy)
    monkeypatch.setattr(socket.socket, "sendto", lambda self, data, addr: len(data))
    UdpQsoLogger("127.0.0.1", 2237, format="adif").log_qso(_q())
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in opts
