# SPDX-License-Identifier: GPL-3.0-or-later
"""Diagnostics export — package logs + system info + redacted config into a zip.

Backs the **Settings → Diagnostics → Export Diagnostics…** button so users
can attach a single file to bug reports instead of being told to navigate
to ``%LOCALAPPDATA%\\open_sstv\\open_sstv\\Logs\\`` or wherever
``platformdirs.user_log_dir`` happens to land on their platform.

The zip contains three members:

* ``open-sstv.log`` — concatenation of the current rotating log file
  plus its ``.1`` / ``.2`` backups, in chronological order (oldest
  first).  Captures whatever ``app.py``'s ``_setup_logging`` has been
  writing since the app started — see ``open_sstv.app._setup_logging``.

* ``system-info.txt`` — plain-text report with Open-SSTV version,
  install path, Python + key dependency versions, OS info, platformdirs
  paths, bundled-asset presence check, and user templates / fonts dir
  contents.  Everything you'd want pasted into a "doesn't work on my
  machine" issue without having to ask follow-up questions.

* ``config-redacted.toml`` — the user's saved config, with any field
  whose name matches a sensitive pattern (``password``, ``token``,
  ``secret``, etc.) replaced with ``***REDACTED***``.  Today no
  AppConfig field hits the pattern (callsigns are public for hams),
  but the redaction logic is here for forward-compat when a future
  schema adds an API key or service token.

Design notes
------------
* Pure function ``export_diagnostics(zip_path)`` — no Qt dependencies
  in this module so it's unit-testable without ``pytest-qt``.  The
  Settings dialog's button handler wires the user's chosen path
  through.
* Every section is wrapped in ``try/except`` and writes a placeholder
  on failure — a corrupt config or missing log file should never
  prevent the user from sharing whatever else IS available.
* Sizes capped: log file rotation already bounds the log to ~6 MB
  total; system-info is text and tiny; config is text and tiny.
  Final zip is well under 10 MB even on a long-running session.
"""
from __future__ import annotations

import io
import logging
import os
import platform
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

#: Fields whose value gets replaced with the redaction marker when
#: written into ``config-redacted.toml``.  Match is case-insensitive
#: substring against the field name.  Callsigns are intentionally NOT
#: redacted — ham callsigns are public FCC database entries.
_SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
)

_REDACTION_MARKER = "***REDACTED***"


def export_diagnostics(zip_path: Path) -> Path:
    """Write a diagnostics zip to *zip_path* and return the path.

    Raises ``OSError`` only if the destination is fundamentally unwriteable;
    individual sections that fail are logged and their failure is recorded
    in the zip itself rather than aborting the whole export.

    The output zip is overwritten if it exists — the caller's ``QFileDialog``
    typically handles the "overwrite this file?" prompt before invoking
    us, so re-writing without warning is the expected behaviour here.
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("open-sstv.log", _collect_logs())
        zf.writestr("system-info.txt", _collect_system_info())
        zf.writestr("config-redacted.toml", _collect_redacted_config())
    return zip_path


# ---------------------------------------------------------------------------
# Section: open-sstv.log
# ---------------------------------------------------------------------------


def _collect_logs() -> str:
    """Concatenate current + rotated log files into one chronological dump.

    ``RotatingFileHandler`` names the active file ``open-sstv.log`` and
    rotates older ones to ``open-sstv.log.1`` (most recent backup) and
    ``open-sstv.log.2`` (older backup).  We assemble them oldest-first so
    reading top-to-bottom matches event order — most useful for the
    issue-triager reading the zip.
    """
    try:
        import platformdirs  # noqa: PLC0415
        log_dir = Path(platformdirs.user_log_dir("open_sstv"))
    except Exception as exc:  # noqa: BLE001
        return f"(could not resolve log dir: {exc})\n"

    base = log_dir / "open-sstv.log"
    # RotatingFileHandler: .2 (oldest) → .1 → (current).  Iterate in
    # reverse order so the most recent events are at the bottom.
    paths_oldest_first = [base.with_suffix(".log.2"),
                          base.with_suffix(".log.1"),
                          base]
    buf = io.StringIO()
    found_any = False
    for p in paths_oldest_first:
        if not p.is_file():
            continue
        found_any = True
        buf.write(f"=== {p.name} ({p.stat().st_size} bytes) ===\n")
        try:
            buf.write(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            buf.write(f"(read failed: {exc})\n")
        buf.write("\n")
    if not found_any:
        buf.write(
            f"(no log files found at {log_dir} — file logging may have "
            "failed to initialise, or the app has not generated any output "
            "yet this session)\n"
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Section: system-info.txt
# ---------------------------------------------------------------------------


def _collect_system_info() -> str:
    """Return a multi-line plain-text system-info report."""
    lines: list[str] = []
    lines.append("Open-SSTV diagnostics")
    lines.append(f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}")
    lines.append("")

    # Open-SSTV version + install path (the exact same line app.py prints
    # at startup, so we know which build the user is actually running).
    try:
        import open_sstv  # noqa: PLC0415
        lines.append(f"Version: {open_sstv.__version__}")
        lines.append(f"Install path: {open_sstv.__file__}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Version: (lookup failed: {exc})")
    lines.append(f"Frozen / PyInstaller bundle: {getattr(sys, 'frozen', False)}")
    lines.append(f"sys.executable: {sys.executable}")
    lines.append("")

    # Python + key deps versions.  importlib.metadata works for everything
    # except Python itself, which is sys.version_info.
    lines.append(f"Python: {sys.version.split()[0]} ({platform.python_implementation()})")
    for dist in ("PySide6", "numpy", "scipy", "Pillow", "sounddevice",
                 "pyserial", "platformdirs", "tomli-w", "websocket-client"):
        try:
            from importlib.metadata import version  # noqa: PLC0415
            lines.append(f"{dist}: {version(dist)}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{dist}: (lookup failed: {exc})")
    lines.append("")

    # OS info
    lines.append(f"OS: {platform.system()} {platform.release()} ({platform.machine()})")
    lines.append(f"Platform: {platform.platform()}")
    lines.append("")

    # Platformdirs paths — the values the app actually uses for
    # config / log / cache.  If any of these don't match what the user
    # expected (e.g. they're looking at AppData\Local but the app uses
    # AppData\Roaming), this section makes it visible.
    try:
        import platformdirs  # noqa: PLC0415
        lines.append("User directories (per platformdirs):")
        lines.append(f"  config: {platformdirs.user_config_dir('open_sstv')}")
        lines.append(f"  data:   {platformdirs.user_data_dir('open_sstv')}")
        lines.append(f"  log:    {platformdirs.user_log_dir('open_sstv')}")
        lines.append(f"  cache:  {platformdirs.user_cache_dir('open_sstv')}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"User directories: (lookup failed: {exc})")
    lines.append("")

    # Bundled-asset presence check — same check app.py runs at startup,
    # repeated here so a diagnostics zip carries the answer.
    try:
        from open_sstv.app import _verify_bundled_assets  # noqa: PLC0415
        missing = _verify_bundled_assets()
        if missing:
            lines.append("Bundled-asset presence check: MISSING")
            for m in missing:
                lines.append(f"  - {m}")
        else:
            lines.append("Bundled-asset presence check: OK (all expected assets present)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Bundled-asset presence check: (failed: {exc})")
    lines.append("")

    # User templates dir — count and sample names.  The v0.3.20 bug
    # surfaced as "zero templates" so this section is the smoking gun
    # for that class of bug if it recurs.
    try:
        from open_sstv.templates.manager import default_templates_dir  # noqa: PLC0415
        utdir = default_templates_dir()
        lines.append(f"User templates dir: {utdir}")
        if utdir.is_dir():
            tomls = sorted(utdir.glob("*.toml"))
            lines.append(f"  {len(tomls)} *.toml file(s):")
            for p in tomls[:20]:
                lines.append(f"    {p.name}")
            if len(tomls) > 20:
                lines.append(f"    ... and {len(tomls) - 20} more")
        else:
            lines.append("  (does not exist — install_starter_pack may have failed)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"User templates dir: (lookup failed: {exc})")
    lines.append("")

    # User fonts dir — same idea
    try:
        import platformdirs  # noqa: PLC0415
        ufdir = Path(platformdirs.user_config_dir('open_sstv')) / "fonts"
        lines.append(f"User fonts dir: {ufdir}")
        if ufdir.is_dir():
            ttfs = sorted(list(ufdir.glob("*.ttf")) + list(ufdir.glob("*.otf")))
            lines.append(f"  {len(ttfs)} font file(s)")
        else:
            lines.append("  (does not exist — no user-imported fonts)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"User fonts dir: (lookup failed: {exc})")
    lines.append("")

    # Audio devices visible to sounddevice — count only, not the full
    # device list (that can include personally-identifying device names
    # like "Kevin's AirPods").  Operators can run our existing CLI tools
    # if they need the full list for debugging.
    try:
        import sounddevice as sd  # noqa: PLC0415
        devs = sd.query_devices()
        lines.append(f"sounddevice: {len(devs)} device(s) visible")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"sounddevice: (query failed: {exc})")
    lines.append("")

    # Environment variables that affect Open-SSTV behaviour.
    lines.append("Relevant environment variables:")
    for var in ("OPEN_SSTV_DEBUG", "OPEN_SSTV_LOGFILE",
                "QT_QPA_PLATFORM", "QT_DEBUG_PLUGINS"):
        val = os.environ.get(var)
        if val is not None:
            lines.append(f"  {var}={val!r}")
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Section: config-redacted.toml
# ---------------------------------------------------------------------------


def _collect_redacted_config() -> str:
    """Read the user's config.toml and return a redacted text dump.

    If the config file doesn't exist (first launch, user never saved
    Settings), return a placeholder rather than an empty member — the
    file's presence in the zip is itself a useful signal to the
    triager.
    """
    try:
        from open_sstv.config.store import config_path  # noqa: PLC0415
        path = config_path()
    except Exception as exc:  # noqa: BLE001
        return f"# Could not resolve config path: {exc}\n"

    if not path.is_file():
        return f"# Config file does not exist at {path}\n# (user has not opened Settings yet, or first launch)\n"

    try:
        import tomllib  # noqa: PLC0415
        with path.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001
        return f"# Could not read config at {path}: {exc}\n"

    redacted = _redact_dict(data)

    try:
        import tomli_w  # noqa: PLC0415
        # tomli_w.dumps takes a dict and returns a TOML string
        body = tomli_w.dumps(redacted)
    except Exception as exc:  # noqa: BLE001
        body = f"# Could not serialise redacted config: {exc}\n# Raw repr follows:\n{redacted!r}\n"

    header = (
        f"# Source: {path}\n"
        f"# Redacted fields are replaced with {_REDACTION_MARKER!r}.\n"
        f"# Field-name patterns triggering redaction: {_SENSITIVE_NAME_PATTERNS}\n"
        f"\n"
    )
    return header + body


def _redact_dict(data: dict) -> dict:
    """Return *data* with values replaced for sensitive-named fields."""
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif _is_sensitive_field(k) and v not in ("", None):
            out[k] = _REDACTION_MARKER
        else:
            out[k] = v
    return out


def _is_sensitive_field(name: str) -> bool:
    """Case-insensitive substring match against the sensitive-name patterns."""
    n = name.lower()
    return any(p in n for p in _SENSITIVE_NAME_PATTERNS)


__all__ = ["export_diagnostics"]
