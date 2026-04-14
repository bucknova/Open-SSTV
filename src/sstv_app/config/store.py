# SPDX-License-Identifier: GPL-3.0-or-later
"""Load and save the user config from a TOML file in the platformdirs path.

Reads with stdlib ``tomllib``, writes with ``tomli_w``. Missing keys fall
back to ``AppConfig`` dataclass defaults; unknown keys are silently ignored
(forwards-compatible with future config additions).

The config file lives at ``platformdirs.user_config_dir("sstv_app") / "config.toml"``.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, fields
from pathlib import Path

_log = logging.getLogger(__name__)

import platformdirs
import tomli_w

from sstv_app.config.schema import AppConfig

_APP_NAME = "sstv_app"
_CONFIG_FILENAME = "config.toml"


def config_path() -> Path:
    """Absolute path to the TOML config file (may not exist yet)."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / _CONFIG_FILENAME


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from *path* (default: ``config_path()``).

    Returns a fresh ``AppConfig()`` with defaults if the file doesn't
    exist or is empty. Unknown keys are ignored; missing keys keep
    their dataclass defaults.
    """
    if path is None:
        path = config_path()
    if not path.is_file():
        return AppConfig()

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        # Only pass keys that AppConfig actually defines, so a TOML file
        # from a newer version with extra keys doesn't blow up construction.
        known = {f.name for f in fields(AppConfig)}
        filtered = {k: v for k, v in raw.items() if k in known}
        return AppConfig(**filtered)
    except Exception:  # noqa: BLE001 — corrupt file must never crash startup
        _log.warning("Config file %s is corrupt or unreadable — using defaults", path)
        return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    """Write *cfg* to *path* (default: ``config_path()``).

    Creates parent directories if needed.

    Raises
    ------
    OSError
        If the config directory cannot be created or the file cannot be
        written (permission denied, disk full, etc.). The caller is
        expected to catch this and surface it to the user rather than
        letting it propagate as an unhandled exception.
    """
    if path is None:
        path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(cfg).items() if v is not None}
        with path.open("wb") as f:
            tomli_w.dump(data, f)
    except OSError as exc:
        _log.error("Could not save config to %s: %s", path, exc)
        raise


__all__ = ["config_path", "load_config", "save_config"]
