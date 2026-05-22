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
import threading
import tomllib
from dataclasses import asdict, fields
from pathlib import Path

_log = logging.getLogger(__name__)

import platformdirs
import tomli_w

from open_sstv.config.schema import AppConfig

_APP_NAME = "open_sstv"
_CONFIG_FILENAME = "config.toml"

#: Serializes concurrent ``save_config()`` calls.  ``save_config`` is
#: invoked from the GUI thread (Settings dialog Save), but background
#: paths — Phase 3 auto-save toggles, future first-launch dialog flows,
#: and tests that exercise the writer from a worker — can race.  The
#: tmp-file + ``os.replace`` is atomic per call, but two interleaved
#: writes could still produce a final file from one call and a
#: half-written ``.tmp`` from the other (visible to the next ``load_config``
#: as a stray sibling).  A module-level lock keeps the section single-
#: writer without forcing every caller to know about the threading model.
_save_lock = threading.Lock()


def config_path() -> Path:
    """Absolute path to the TOML config file (may not exist yet)."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / _CONFIG_FILENAME


def _cleanup_stale_tmp(path: Path) -> None:
    """Delete ``<path>.tmp`` if it exists.

    Called by ``load_config`` to opportunistically clean up the tmp file
    left behind when a previous ``save_config`` was killed between
    ``tomli_w.dump`` and ``os.replace`` (SIGKILL, power loss, OOM).
    The tmp may be a complete config that just didn't get renamed, OR
    a partial write — we don't try to recover it either way because:

      * if it's complete, the next save will overwrite it anyway,
      * if it's partial, it's worse than the default config we'd fall
        back to.

    Any error from ``unlink`` is logged at debug and swallowed — a
    stale tmp blocking nothing is acceptable; the load must proceed.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    if not tmp.exists():
        return
    try:
        tmp.unlink()
        _log.info("Removed stale config tmp file: %s", tmp)
    except OSError as exc:
        _log.debug("Could not remove stale config tmp %s: %s", tmp, exc)


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from *path* (default: ``config_path()``).

    Returns a fresh ``AppConfig()`` with defaults if the file doesn't
    exist or is empty. Unknown keys are ignored; missing keys keep
    their dataclass defaults.

    OP2-06: OSError (permission denied, directory instead of file) propagates
    rather than being silently swallowed — only TOML decode errors fall back
    to defaults, since a corrupt file is genuinely unrecoverable.

    H-5 (audit 4.7/v0.2.9): opportunistically delete any
    ``<config>.toml.tmp`` sibling left behind by a SIGKILL between
    ``tomli_w.dump`` and ``os.replace`` in a prior ``save_config()``
    call.  ``save_config`` only unlinks its own tmp on caught
    ``OSError`` — a hard kill leaves the tmp orphaned forever, and
    over a long-running install they accumulate.  Cleanup here is a
    no-op when nothing is stale and runs once per app start (the
    common load_config caller).  Failure to unlink is logged but does
    not block the load — the load path is what users care about.
    """
    if path is None:
        path = config_path()
    _cleanup_stale_tmp(path)
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
    with _save_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # L5: ``None`` values are stripped from the dict before
            # writing because tomli-w doesn't have a representation for
            # them (TOML has no null).  Currently a no-op because no
            # AppConfig field is intentionally None at save time —
            # ``None`` only means "missing → use the dataclass default
            # on load".  WATCH FOR: if a future schema field uses
            # ``None`` as an "explicit reset" sentinel (vs. "field was
            # never set"), the load path will treat the two
            # indistinguishably and silently re-apply the default.
            # Solution at that point will be: write a sentinel string
            # (e.g. ``"__none__"``) for explicit-None fields and have
            # the load path translate it back.  No action needed today.
            data = {k: v for k, v in asdict(cfg).items() if v is not None}
            with tmp.open("wb") as f:
                tomli_w.dump(data, f)
            os.replace(tmp, path)
        except OSError as exc:
            _log.error("Could not save config to %s: %s", path, exc)
            tmp.unlink(missing_ok=True)
            raise


__all__ = ["config_path", "load_config", "save_config"]
