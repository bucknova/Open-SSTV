# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.6.14 audit C-H2: the config is loaded once at startup.

The first load (for the log level) moved a corrupt file to ``.corrupt``;
MainWindow then loaded again, found nothing, skipped the warning, showed
the Welcome dialog and saved defaults.  ``_load_startup_config`` is now
the single load, and its result (config + backup path) goes to MainWindow.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from open_sstv import app
from open_sstv.config import store


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.toml"
    monkeypatch.setattr(store, "config_path", lambda: p)
    return p


def test_corrupt_config_is_loaded_once_with_backup(cfg_path: Path) -> None:
    cfg_path.write_bytes(b"[[[ not toml")
    cfg, backup, records = app._load_startup_config()
    assert backup == cfg_path.with_suffix(".toml.corrupt")
    assert backup.exists()
    # Not a fresh install: no Welcome dialog, so no save of defaults.
    assert cfg.first_launch_seen is True
    # The warning was captured for replay once logging is configured.
    assert any("corrupt" in r.getMessage() for r in records)


def test_clean_config_loads_values(cfg_path: Path) -> None:
    cfg_path.write_text('callsign = "W0AEZ"\nlog_level = "DEBUG"\n')
    cfg, backup, _records = app._load_startup_config()
    assert backup is None
    assert cfg.callsign == "W0AEZ"
    assert app._config_log_level(cfg) == logging.DEBUG
    assert cfg_path.exists()


def test_bad_field_warning_is_captured(cfg_path: Path) -> None:
    cfg_path.write_text("log_level = 5\n")
    cfg, backup, records = app._load_startup_config()
    assert backup is None
    assert cfg.log_level == "INFO"
    assert any("log_level" in r.getMessage() for r in records)
