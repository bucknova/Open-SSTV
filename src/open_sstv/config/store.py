# SPDX-License-Identifier: GPL-3.0-or-later
"""Load and save the user config from a TOML file in the platformdirs path.

Reads with stdlib ``tomllib``, writes with ``tomli_w``. Missing keys fall
back to ``AppConfig`` dataclass defaults; unknown keys are silently ignored
(forwards-compatible with future config additions).

The config file lives at ``platformdirs.user_config_dir("open_sstv") / "config.toml"``.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict, fields
from pathlib import Path

_log = logging.getLogger(__name__)

import platformdirs
import tomli_w

from open_sstv.config.schema import AppConfig

_APP_NAME = "open_sstv"
_CONFIG_FILENAME = "config.toml"


def config_path() -> Path:
    """Absolute path to the TOML config file (may not exist yet)."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / _CONFIG_FILENAME


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from *path* (default: ``config_path()``).

    Returns a fresh ``AppConfig()`` with defaults if the file doesn't
    exist or is empty. Unknown keys are ignored; missing keys keep
    their dataclass defaults.

    OP2-06: OSError (permission denied, directory instead of file) propagates
    rather than being silently swallowed — only TOML decode errors fall back
    to defaults, since a corrupt file is genuinely unrecoverable.
    """
    if path is None:
        path = config_path()
    if not path.is_file():
        return AppConfig()

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
        # v0.1.24: renamed experimental_incremental_decode -> incremental_decode.
        # Migrate old config files that still carry the previous key so users
        # who explicitly set it to False keep that preference.
        if "experimental_incremental_decode" in raw and "incremental_decode" not in raw:
            raw["incremental_decode"] = raw["experimental_incremental_decode"]
        # v0.2.7: the first-launch callsign dialog is gated on
        # ``first_launch_seen``.  The mere presence of a config file
        # means the user has opened the app before (≤ v0.2.6 didn't
        # emit this key), so grandfather them in — don't show the
        # welcome prompt to an existing user just because they upgraded.
        if "first_launch_seen" not in raw:
            raw["first_launch_seen"] = True

        # Only pass keys that AppConfig actually defines, so a TOML file
        # from a newer version with extra keys doesn't blow up construction.
        known = {f.name for f in fields(AppConfig)}
        filtered = {k: v for k, v in raw.items() if k in known}
        return AppConfig(**filtered)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        _log.warning("Config file %s is corrupt — using defaults", path)
        return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    """Write *cfg* to *path* (default: ``config_path()``).

    Creates parent directories if needed.  Writes atomically via a
    sibling ``.tmp`` file + ``os.replace`` so a SIGKILL mid-write never
    leaves a truncated config (OP2-07).

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
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(cfg).items() if v is not None}
        with tmp.open("wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        _log.error("Could not save config to %s: %s", path, exc)
        tmp.unlink(missing_ok=True)
        raise


__all__ = ["config_path", "load_config", "save_config"]
