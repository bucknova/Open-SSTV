# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable file writes.

``atomic_write_bytes`` is the one way Open-SSTV replaces a file that must
not be lost: the config, the templates, and auto-saved images.

The writers it replaces each did "write ``<name>.tmp``, then
``os.replace``".  That's atomic, but not durable.  Without an ``fsync``, a
power cut shortly after the rename can leave the replaced file empty on
APFS, XFS, btrfs and others: the rename reached the disk and the data
didn't.  An empty config loads as defaults, so the callsign, audio devices
and rig setup are gone.  The fixed ``.tmp`` name also let two concurrent
saves of the same file interleave.  (2026-10 stability audit.)
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

_log = logging.getLogger(__name__)

#: A temp file younger than this may belong to a save still in flight
#: (another thread or process); never sweep it.
STALE_TEMP_MIN_AGE_S: float = 60.0


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace *path* with *data*, atomically and durably.

    Writes a uniquely named temporary file beside *path*, flushes and
    ``fsync``\\ s it, renames it over *path*, then ``fsync``\\ s the
    directory so the rename itself survives a crash (POSIX).  On any error
    the temporary file is removed and the exception propagates, leaving
    *path* untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make a rename in *directory* durable.  Best effort.

    Windows can't open a directory for fsync, and NTFS journals the rename
    anyway.  Some network filesystems refuse it too, so a failure here is
    ignored.  The data itself was already fsync'd.
    """
    if sys.platform == "win32":
        return
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def temp_glob(name: str) -> str:
    """Glob pattern matching ``atomic_write_bytes`` temps for file *name*
    (``.<name>.<random>.tmp``)."""
    return f".{name}.*.tmp"


def sweep_stale_temps(
    directory: Path,
    pattern: str,
    *,
    min_age_s: float = STALE_TEMP_MIN_AGE_S,
) -> int:
    """Delete files in *directory* matching *pattern* older than *min_age_s*.

    v0.6.14 audit: the config and template stale-tmp sweeps still looked
    for the old fixed ``<name>.tmp`` names after the writers moved to
    ``atomic_write_bytes``'s ``.<name>.<random>.tmp``, so a temp orphaned
    by a hard kill was never cleaned up.  Callers pass ``temp_glob(...)``.
    The age floor keeps a sweep from deleting a temp another save is
    still writing.  Best effort: errors are logged and swallowed.
    Returns the number of files removed.
    """
    removed = 0
    now = time.time()
    try:
        candidates = list(Path(directory).glob(pattern))
    except OSError as exc:
        _log.debug("Stale-temp sweep of %s failed: %s", directory, exc)
        return 0
    for tmp in candidates:
        try:
            if now - tmp.stat().st_mtime < min_age_s:
                continue
            tmp.unlink()
            removed += 1
            _log.info("Removed stale temp file: %s", tmp)
        except OSError as exc:
            _log.debug("Could not remove stale temp %s: %s", tmp, exc)
    return removed


__all__ = ["atomic_write_bytes", "sweep_stale_temps", "temp_glob"]
