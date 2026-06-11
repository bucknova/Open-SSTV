# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics export — logbook member opt-in (v0.4).

``export_diagnostics`` is a pure function (no Qt), so these run
headless.  Only the new logbook behaviour is covered here; the
log/system-info/config sections predate this file and are exercised
implicitly by every assertion that the zip opens cleanly.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from open_sstv.config.schema import AppConfig
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


def _members(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


class TestLogbookMember:
    def test_default_excludes_logbook(self, tmp_path: Path, fake_config: AppConfig) -> None:
        (tmp_path / "logbook.db").write_bytes(b"sqlite-ish bytes")
        out = export_diagnostics(tmp_path / "diag.zip")
        names = _members(out)
        assert "logbook.db" not in names
        assert "logbook-missing.txt" not in names
        # The three standard members are always present.
        assert {"open-sstv.log", "system-info.txt", "config-redacted.toml"} <= names

    def test_opt_in_includes_db_bytes(self, tmp_path: Path, fake_config: AppConfig) -> None:
        payload = b"sqlite-ish bytes"
        (tmp_path / "logbook.db").write_bytes(payload)
        out = export_diagnostics(tmp_path / "diag.zip", include_logbook=True)
        with zipfile.ZipFile(out) as zf:
            assert zf.read("logbook.db") == payload

    def test_opt_in_with_no_db_writes_placeholder(
        self, tmp_path: Path, fake_config: AppConfig
    ) -> None:
        out = export_diagnostics(tmp_path / "diag.zip", include_logbook=True)
        names = _members(out)
        assert "logbook.db" not in names
        assert "logbook-missing.txt" in names
