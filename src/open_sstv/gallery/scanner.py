# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan image directories into ``GalleryItem`` records.

Pure filesystem — no Qt, no logbook.  The logbook join happens later
in ``gallery.index``.

Filename metadata is parsed **position-independently**.  The auto-save
filename pattern is user-configurable (``autosave_filename_pattern``,
default ``%d_%t_%m``), so rather than assume a layout we:

- take the timestamp from the file's mtime (always present),
- pull a UTC date from any ``YYYY-MM-DD`` run in the stem (the default
  ``%d`` token), and
- pull the SSTV mode from any ``Mode`` enum *value* token in the stem.

Ground truth (verified 2026-07-07): ``%m`` renders as the raw enum
value — ``2026-04-17_213512_scottie_s1.png`` — not the ``Scottie-S1``
form an older schema comment suggested.  So the mode parser matches
against ``Mode`` values directly.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from open_sstv.core.modes import Mode
from open_sstv.gallery.model import GalleryItem

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = logging.getLogger(__name__)

#: Image extensions the gallery browses.  Matches the TX panel's
#: load-image filter so anything the app can open is browsable.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
)

#: Mode values, longest first, so ``wraase_sc2_180`` is tried before any
#: shorter value it might share a prefix with.  Anchored on token
#: boundaries (start / ``_`` / ``-`` / end) so a mode embedded anywhere
#: in the stem is found regardless of the surrounding pattern.
_MODE_VALUES: tuple[str, ...] = tuple(
    sorted((m.value for m in Mode), key=len, reverse=True)
)
_MODE_RE = re.compile(
    r"(?:^|[_-])(" + "|".join(re.escape(v) for v in _MODE_VALUES) + r")(?:[_-]|$)"
)
_DATE_RE = re.compile(r"(?:^|[_-])(\d{4})-(\d{2})-(\d{2})(?:[_-]|$)")


def parse_mode(stem: str) -> str | None:
    """Return the ``Mode`` value embedded in *stem*, or ``None``."""
    m = _MODE_RE.search(stem)
    return m.group(1) if m else None


def parse_date(stem: str) -> date | None:
    """Return the UTC date from a ``YYYY-MM-DD`` run in *stem*, or ``None``."""
    m = _DATE_RE.search(stem)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None  # e.g. 2026-13-40 — digits matched but not a real date


def _item_for(path: Path) -> GalleryItem | None:
    """Build a ``GalleryItem`` for one file, or ``None`` if unreadable."""
    try:
        st = path.stat()
    except OSError as exc:
        _log.debug("gallery scan: skipping unstatable %s: %s", path, exc)
        return None
    stem = path.stem
    return GalleryItem(
        path=path,
        mtime=st.st_mtime,
        size_bytes=st.st_size,
        parsed_mode=parse_mode(stem),
        parsed_date=parse_date(stem),
    )


def scan_dir(directory: Path) -> list[GalleryItem]:
    """Scan one directory (non-recursive) for browsable image files.

    Non-existent or unreadable directories yield an empty list rather
    than raising — a misconfigured ``images_save_dir`` must not crash
    the gallery.
    """
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        _log.warning("gallery scan: cannot read %s: %s", directory, exc)
        return []
    items: list[GalleryItem] = []
    for entry in entries:
        if entry.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if not entry.is_file():
            continue
        item = _item_for(entry)
        if item is not None:
            items.append(item)
    return items


def scan_dirs(directories: Iterable[Path]) -> list[GalleryItem]:
    """Scan several directories, de-duplicating by resolved path.

    The same file reachable via two configured dirs (e.g.
    ``images_save_dir`` plus a ``gallery_extra_dirs`` entry that
    overlaps) appears once.  Order follows first-seen.
    """
    seen: set[str] = set()
    out: list[GalleryItem] = []
    for directory in directories:
        for item in scan_dir(directory):
            key = item.path.as_posix()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


__all__ = [
    "IMAGE_EXTENSIONS",
    "parse_date",
    "parse_mode",
    "scan_dir",
    "scan_dirs",
]
