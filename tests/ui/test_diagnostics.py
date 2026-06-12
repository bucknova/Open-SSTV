# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics export — logbook member opt-in (v0.4).

``export_diagnostics`` is a pure function (no Qt), so these run
headless.  Only the logbook behaviour is covered here; the
log/system-info/config sections predate this file and are exercised
implicitly by every assertion that the zip opens cleanly.

The logbook member goes through sqlite3's backup API (audit #9), so
these tests work against a real ``LogbookStore`` and verify the
zipped snapshot is a *valid* database, not just copied bytes.
"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import LogbookStore
from open_sstv.ui.diagnostics import export_diagnostics


@pytest.fixture
def fake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """Point the diagnostics config lookup at a config we control."""
    cfg = AppConfig()
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    monkeypatch.setattr(
        "open_sstv.config.store.load_config", lambda path=None: cfg
    )
    return cfg


def _make_real_logbook(tmp_path: Path) -> None:
    with LogbookStore(tmp_path / "logbook.db") as store:
        store.insert(QSO(direction="RX", callsign="K1ABC", mode="Robot 36"))


def _members(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


class TestLogbookMember:
    def test_default_excludes_logbook(self, tmp_path: Path, fake_config: AppConfig) -> None:
        _make_real_logbook(tmp_path)
        out = export_diagnostics(tmp_path / "diag.zip")
        names = _members(out)
        assert "logbook.db" not in names
        assert "logbook-missing.txt" not in names
        # The three standard members are always present.
        assert {"open-sstv.log", "system-info.txt", "config-redacted.toml"} <= names

    def test_opt_in_includes_valid_snapshot(
        self, tmp_path: Path, fake_config: AppConfig
    ) -> None:
        _make_real_logbook(tmp_path)
        out = export_diagnostics(tmp_path / "diag.zip", include_logbook=True)
        extracted = tmp_path / "extracted.db"
        with zipfile.ZipFile(out) as zf:
            extracted.write_bytes(zf.read("logbook.db"))
        # The snapshot is a real, openable database with the row intact —
        # the property a raw byte copy couldn't guarantee (audit #9).
        conn = sqlite3.connect(extracted)
        try:
            rows = conn.execute("SELECT callsign FROM qsos").fetchall()
        finally:
            conn.close()
        assert rows == [("K1ABC",)]

    def test_opt_in_with_no_db_writes_placeholder(
        self, tmp_path: Path, fake_config: AppConfig
    ) -> None:
        out = export_diagnostics(tmp_path / "diag.zip", include_logbook=True)
        names = _members(out)
        assert "logbook.db" not in names
        assert "logbook-missing.txt" in names

    def test_corrupt_db_degrades_to_placeholder(
        self, tmp_path: Path, fake_config: AppConfig
    ) -> None:
        # Not-actually-sqlite bytes must not abort the whole export —
        # the other diagnostics members still matter.
        (tmp_path / "logbook.db").write_bytes(b"this is not a database")
        out = export_diagnostics(tmp_path / "diag.zip", include_logbook=True)
        names = _members(out)
        assert "logbook.db" not in names
        assert "logbook-missing.txt" in names
        with zipfile.ZipFile(out) as zf:
            assert b"export failed" in zf.read("logbook-missing.txt")
