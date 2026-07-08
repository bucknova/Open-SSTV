# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent thumbnail cache for the gallery.

Produces small PNG thumbnails on disk under
``user_cache_dir("open_sstv")/thumbnails/`` so reopening the gallery is
instant.  Deliberately **Qt-free** (Pillow only) — it stores thumbnail
*files*; the UI layer loads them into ``QPixmap`` and keeps its own
in-memory LRU for the visible viewport.  This keeps the cache unit-
testable headless.

Cache key is a hash of ``(absolute path, mtime, size, thumb size)``, so
editing or replacing a file (which changes mtime/size) transparently
produces a new thumbnail and the stale one is pruned by ``prune``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

import platformdirs
from PIL import Image

_log = logging.getLogger(__name__)

_APP_NAME = "open_sstv"
#: 160×120 matches the RX-panel strip and keeps 4:3 Robot 36 proportions.
DEFAULT_THUMB_SIZE: tuple[int, int] = (160, 120)
#: Prune keeps at most this many thumbnails (newest by mtime).  ~15k ×
#: a few KB each is a handful of MB — comfortably past the ≤10k image
#: target with headroom for re-rendered (mtime-changed) duplicates.
DEFAULT_MAX_FILES: int = 15_000


def default_thumbnail_cache_dir() -> Path:
    """``user_cache_dir/open_sstv/thumbnails`` (may not exist yet)."""
    return Path(platformdirs.user_cache_dir(_APP_NAME)) / "thumbnails"


class ThumbnailCache:
    """On-disk thumbnail cache.  One instance per gallery window."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        size: tuple[int, int] = DEFAULT_THUMB_SIZE,
    ) -> None:
        self._dir = cache_dir if cache_dir is not None else default_thumbnail_cache_dir()
        self._size = size
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # A read-only / locked cache dir degrades to "regenerate
            # every time" rather than crashing the gallery.
            _log.warning("thumbnail cache dir unavailable (%s): %s", self._dir, exc)

    @property
    def cache_dir(self) -> Path:
        return self._dir

    def _key(self, path: Path, mtime: float, size_bytes: int) -> str:
        raw = f"{path.resolve().as_posix()}|{mtime}|{size_bytes}|{self._size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get_or_create(self, image_path: Path) -> Path | None:
        """Return a cached thumbnail PNG path for *image_path*.

        Generates the thumbnail on first request and on any change to
        the source (mtime/size).  Returns ``None`` if the source can't
        be read or decoded (corrupt/partial image) or the cache dir is
        unwritable — the UI shows a placeholder in that case.
        """
        try:
            st = image_path.stat()
        except OSError:
            return None
        thumb = self._dir / f"{self._key(image_path, st.st_mtime, st.st_size)}.png"
        if thumb.exists():
            return thumb
        try:
            with Image.open(image_path) as im:
                im.draft("RGB", self._size)  # fast pre-scale for big JPEGs
                im = im.convert("RGB")
                im.thumbnail(self._size)  # in place, preserves aspect ratio
                # Write to a UNIQUE temp sibling then atomically rename, so a
                # concurrent reader never sees a half-written PNG AND two
                # threads generating the same thumbnail at once (e.g. a
                # phone + desktop both loading the gallery, now that the
                # remote server serves multiple clients) don't clobber each
                # other's temp file.  A per-thumb fixed name would collide.
                fd, tmp_name = tempfile.mkstemp(
                    suffix=".png.tmp", prefix=thumb.stem + "-", dir=self._dir
                )
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    im.save(tmp, "PNG")
                    tmp.replace(thumb)  # atomic on the same filesystem
                except Exception:
                    tmp.unlink(missing_ok=True)  # don't leave an orphan temp
                    raise
        except Exception as exc:  # noqa: BLE001 — any decode/IO failure → placeholder
            _log.debug("thumbnail generation failed for %s: %s", image_path, exc)
            return None
        return thumb

    def prune(self, max_files: int = DEFAULT_MAX_FILES) -> int:
        """Delete oldest thumbnails beyond *max_files*.  Returns count removed.

        Cheap best-effort housekeeping — the gallery calls this on close.
        Orphans (source deleted, or re-rendered under a new key) age out
        by mtime.
        """
        try:
            thumbs = [p for p in self._dir.glob("*.png") if p.is_file()]
        except OSError:
            return 0
        if len(thumbs) <= max_files:
            return 0
        thumbs.sort(key=lambda p: p.stat().st_mtime)  # oldest first
        removed = 0
        for p in thumbs[: len(thumbs) - max_files]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        return removed


__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_THUMB_SIZE",
    "ThumbnailCache",
    "default_thumbnail_cache_dir",
]
