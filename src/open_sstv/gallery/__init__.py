# SPDX-License-Identifier: GPL-3.0-or-later
"""Image gallery (v0.5) — data layer.

A first-class in-app browser for received (and opt-in transmitted)
SSTV images.  The gallery is a **filesystem × logbook left-join**: a
``GalleryItem`` is a file on disk, optionally enriched by the matching
logbook ``QSO`` record.  See ``docs/v0.5-plan.md``.

Package layout
──────────────
- ``model``           — ``GalleryItem`` (file facts + resolved views).
- ``scanner``         — scan image dirs; parse mode/date from filenames.
- ``index``           — build the ``{path: QSO}`` join and enrich items.
- ``thumbnail_cache`` — persistent on-disk thumbnail cache (Qt-free).

The UI (``ui/gallery_dialog.py``) lands in PR #2; this package is the
Qt-free data layer and is import-safe with nothing wired into the
running app yet.
"""
from __future__ import annotations

from open_sstv.gallery.index import build_qso_index, enrich
from open_sstv.gallery.model import GalleryItem
from open_sstv.gallery.scanner import (
    IMAGE_EXTENSIONS,
    parse_date,
    parse_mode,
    scan_dir,
    scan_dirs,
)
from open_sstv.gallery.thumbnail_cache import (
    ThumbnailCache,
    default_thumbnail_cache_dir,
)

__all__ = [
    "IMAGE_EXTENSIONS",
    "GalleryItem",
    "ThumbnailCache",
    "build_qso_index",
    "default_thumbnail_cache_dir",
    "enrich",
    "parse_date",
    "parse_mode",
    "scan_dir",
    "scan_dirs",
]
