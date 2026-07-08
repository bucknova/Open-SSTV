# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless, Qt-free read model for the remote gallery.

:class:`GalleryService` is the whole data layer the Phase 1 server needs.
It reuses the ``gallery`` package (scanner + logbook index, both Qt-free)
and adds two things the web surface requires:

- **Opaque image ids.**  The browser never sends a filesystem path; it
  receives an id per image and asks for ``/api/thumb/<id>`` /
  ``/api/image/<id>``.  The service keeps an id→path registry from the
  most recent scan and refuses any id it did not itself hand out.  This
  is the path-traversal fence: an attacker cannot name an arbitrary file
  because ids only ever map back to files inside the configured gallery
  directories.
- **Thread-safe access.**  The server serves each request on its own
  thread.  The service therefore holds *no* long-lived SQLite handle:
  each :meth:`list_items` opens a fresh read connection, reads, and
  closes it — and only if a ``logbook.db`` already exists, so a
  view-only operator never causes one to be created.  The id registry is
  guarded by a lock.

Nothing here imports Qt or the main-thread logbook connection, so the
service can be unit-tested headless and called from any thread.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from open_sstv.gallery.index import build_qso_index, enrich
from open_sstv.gallery.scanner import scan_dirs
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.logbook.store import LogbookStore, default_db_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from open_sstv.config.schema import AppConfig
    from open_sstv.gallery.model import GalleryItem

_log = logging.getLogger(__name__)


def _image_id(path: Path) -> str:
    """Stable opaque id for an image path (16 hex chars of SHA-1).

    Deterministic so the same file keeps its id across scans, and
    non-reversible so the id carries no filesystem information.
    """
    return hashlib.sha1(path.as_posix().encode("utf-8")).hexdigest()[:16]


class GalleryService:
    """Read-only gallery data for the remote server.

    Constructed with a ``config_getter`` (not a config value) so it
    always reads the live config — ``MainWindow`` replaces its config
    object on settings save, exactly as the logbook coordinator does.
    """

    def __init__(
        self,
        config_getter: Callable[[], AppConfig],
        thumbnail_cache: ThumbnailCache | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._thumbs = thumbnail_cache if thumbnail_cache is not None else ThumbnailCache()
        self._registry: dict[str, Path] = {}
        self._lock = threading.Lock()
        #: Latest in-progress RX preview (PNG bytes), served by
        #: ``/api/rx/preview`` and refreshed by the RX bridge on the GUI
        #: thread.  ``None`` when no decode is in flight.  Guarded by its
        #: own lock so the server thread reads it without contending on
        #: the registry lock.
        self._live_png: bytes | None = None
        self._live_lock = threading.Lock()

    # -- config-derived paths ------------------------------------------

    def _source_dirs(self) -> list[Path]:
        """``images_save_dir`` + any ``gallery_extra_dirs`` (same as the GUI gallery)."""
        cfg = self._config_getter()
        dirs: list[Path] = []
        if cfg.images_save_dir:
            dirs.append(Path(cfg.images_save_dir))
        dirs.extend(Path(d) for d in cfg.gallery_extra_dirs)
        return dirs

    def _db_path(self) -> Path:
        """Logbook DB path: config override or platform default (mirrors coordinator)."""
        raw = (getattr(self._config_getter(), "logbook_db_path", "") or "").strip()
        return Path(raw).expanduser() if raw else default_db_path()

    # -- scan + join ---------------------------------------------------

    def list_items(self) -> list[GalleryItem]:
        """Scan the source dirs, best-effort logbook-enrich, refresh the id registry.

        The logbook join is best-effort and **never creates** a
        ``logbook.db``: if the file does not exist yet (view-only
        operator) the items are returned unenriched.  A broken or
        locked DB likewise degrades to unenriched rather than raising —
        the gallery must still show files.  Opens and closes its own
        SQLite connection so this is safe on any thread.
        """
        items = scan_dirs(self._source_dirs())
        db_path = self._db_path()
        if db_path.exists():
            store: LogbookStore | None = None
            try:
                store = LogbookStore(db_path)
                enrich(items, build_qso_index(store))
            except Exception as exc:  # noqa: BLE001 — join is best-effort
                _log.warning("remote gallery: logbook join unavailable: %s", exc)
            finally:
                if store is not None:
                    store.close()
        with self._lock:
            self._registry = {_image_id(item.path): item.path for item in items}
        return items

    def payload(self) -> list[dict[str, object]]:
        """JSON-serialisable list of gallery items (id + resolved display fields)."""
        out: list[dict[str, object]] = []
        for item in self.list_items():
            out.append(
                {
                    "id": _image_id(item.path),
                    "name": item.path.name,
                    "mode": item.display_mode,
                    "callsign": item.callsign,
                    "direction": item.direction,
                    "logged": item.is_logged,
                    "timestamp": item.timestamp_utc.isoformat(),
                    "size_bytes": item.size_bytes,
                }
            )
        return out

    # -- id → file resolution (the path-traversal fence) ---------------

    def resolve(self, image_id: str) -> Path | None:
        """Return the path for a known id, or ``None`` if the id was never issued.

        Re-scans once on a miss so a freshly-arrived image (id learned
        by the browser from a newer ``/api/gallery`` than the server has
        cached) still resolves, without trusting the id itself.
        """
        with self._lock:
            hit = self._registry.get(image_id)
        if hit is not None:
            return hit
        self.list_items()  # refresh registry, then retry once
        with self._lock:
            return self._registry.get(image_id)

    def image_path(self, image_id: str) -> Path | None:
        """Resolve *image_id* to a still-present source file, or ``None``."""
        path = self.resolve(image_id)
        if path is None or not path.is_file():
            return None
        return path

    def thumbnail_path(self, image_id: str) -> Path | None:
        """Cached thumbnail PNG for *image_id*, generating it on first request.

        ``None`` if the id is unknown or the source can't be decoded.
        """
        path = self.image_path(image_id)
        if path is None:
            return None
        return self._thumbs.get_or_create(path)

    # -- live RX preview (set by the app, served over HTTP) ------------

    def set_live_frame(self, png: bytes | None) -> None:
        """Store the current in-progress RX preview (PNG bytes), or clear it."""
        with self._live_lock:
            self._live_png = png

    def live_frame(self) -> bytes | None:
        """Return the latest in-progress RX preview PNG, or ``None``."""
        with self._live_lock:
            return self._live_png


__all__ = ["GalleryService"]
