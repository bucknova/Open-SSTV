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
import io
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from open_sstv.gallery.index import build_qso_index, enrich
from open_sstv.gallery.scanner import scan_dirs
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.logbook.store import LogbookStore, default_db_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image as PILImage

    from open_sstv.config.schema import AppConfig
    from open_sstv.gallery.model import GalleryItem

_log = logging.getLogger(__name__)

#: Minimum seconds between filesystem rescans triggered by an unknown-id
#: lookup.  Bounds the work a burst of stale/probing ids can force.
_RESCAN_DEBOUNCE_S = 1.0


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
        # (config_getter exposed below for the compose service to share.)
        self._lock = threading.Lock()
        #: ``time.monotonic()`` of the last full scan, for rescan debounce.
        self._last_scan_t = 0.0
        #: Current in-progress RX frame.  The GUI thread stores the raw PIL
        #: image (cheap); the PNG is encoded lazily on the request thread in
        #: ``live_frame`` (and cached), so the GUI never pays the encode and
        #: nothing is encoded when no browser is watching.  Own lock so the
        #: server thread never contends on the registry lock.
        self._live_img: PILImage | None = None
        self._live_png: bytes | None = None
        self._live_lock = threading.Lock()

    # -- config-derived paths ------------------------------------------

    @property
    def config_getter(self) -> Callable[[], AppConfig]:
        """The live-config getter, shared with the compose service."""
        return self._config_getter

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
            self._last_scan_t = time.monotonic()
        return items

    def payload(self) -> list[dict[str, object]]:
        """JSON-serialisable gallery items, **newest first** (scan_dirs returns
        oldest-first by filename; the remote gallery shows the latest on top)."""
        out: list[dict[str, object]] = []
        items = sorted(
            self.list_items(),
            key=lambda it: (it.timestamp_utc, it.path.name),
            reverse=True,
        )
        for item in items:
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

    # -- logbook (read-only) -------------------------------------------

    def logbook_payload(self, limit: int = 500) -> list[dict[str, object]]:
        """JSON-serialisable QSO list (newest first), or ``[]`` if no logbook.

        Opens its own read connection per call (thread-safe) and never
        creates a ``logbook.db`` — a station with no logbook simply
        returns an empty list.  ``image_id`` is the same opaque id the
        gallery uses, so a logged image can be fetched via
        ``/api/image/<id>`` when it's one of the scanned gallery files.
        """
        db_path = self._db_path()
        if not db_path.exists():
            return []
        store: LogbookStore | None = None
        try:
            store = LogbookStore(db_path)
            rows = store.list_qsos(order="time_desc", limit=limit)
        except Exception as exc:  # noqa: BLE001 — a bad DB must not 500 the view
            _log.warning("remote logbook: unavailable: %s", exc)
            return []
        finally:
            if store is not None:
                store.close()
        out: list[dict[str, object]] = []
        for q in rows:
            out.append(
                {
                    "id": q.id,
                    "callsign": q.callsign,
                    "direction": q.direction,
                    "time": q.time_utc.isoformat(),
                    "mode": q.mode,
                    "frequency_hz": q.frequency_hz,
                    "rst_sent": q.rsv_sent,
                    "rst_received": q.rsv_received,
                    "name": q.name,
                    "image_id": _image_id(q.image_path) if q.image_path else None,
                }
            )
        return out

    # -- id → file resolution (the path-traversal fence) ---------------

    def resolve(self, image_id: str) -> Path | None:
        """Return the path for a known id, or ``None`` if the id was never issued.

        Re-scans once on a miss so a freshly-arrived image (id learned
        by the browser from a newer ``/api/gallery`` than the server has
        cached) still resolves, without trusting the id itself.  The
        rescan is debounced so a burst of unknown/stale ids can't force a
        filesystem walk + DB open per request.
        """
        with self._lock:
            hit = self._registry.get(image_id)
            last = self._last_scan_t
        if hit is not None:
            return hit
        if time.monotonic() - last < _RESCAN_DEBOUNCE_S:
            return None
        self.list_items()  # refresh registry (and _last_scan_t), then retry once
        with self._lock:
            return self._registry.get(image_id)

    def _within_source_dirs(self, path: Path) -> bool:
        """True if *path*'s real location is inside a configured gallery dir.

        The id registry is the fence against client-supplied paths, but it
        is built by walking the gallery directories, and ``Path.is_file()``
        follows symlinks — so a symlink dropped into a watched folder
        (``gallery_extra_dirs`` explicitly invites pointing at a shared
        one) became a legitimate gallery id whose bytes this server would
        then serve.  Resolve both sides and require containment.
        """
        try:
            real = path.resolve(strict=True)
        except OSError:
            return False
        for d in self._source_dirs():
            try:
                if real.is_relative_to(d.resolve(strict=True)):
                    return True
            except OSError:
                continue
        _log.warning(
            "remote: refusing %s — resolves to %s, outside every gallery "
            "directory (symlink?)", path, real,
        )
        return False

    def image_path(self, image_id: str) -> Path | None:
        """Resolve *image_id* to a still-present source file, or ``None``."""
        path = self.resolve(image_id)
        if path is None or not path.is_file():
            return None
        if not self._within_source_dirs(path):
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

    def set_live_image(self, image: PILImage | None) -> None:
        """Store the current in-progress RX frame (a PIL image), or clear it.

        Called from the GUI thread — keeps only a reference and drops the
        cached PNG.  Encoding is deferred to :meth:`live_frame` on the
        request thread, so the GUI thread never pays the PNG cost and
        nothing is encoded when no browser is watching.  The caller passes
        a *private copy*: the decoder keeps mutating the original frame.
        """
        with self._live_lock:
            self._live_img = image
            self._live_png = None

    def live_frame(self) -> bytes | None:
        """PNG bytes of the current in-progress frame, or ``None``.

        Encodes on first request and caches until the frame changes, so
        repeated polls (and multiple viewers) encode each frame once.
        """
        with self._live_lock:
            if self._live_png is not None:
                return self._live_png
            img = self._live_img
        if img is None:
            return None
        try:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            png = buf.getvalue()
        except Exception as exc:  # noqa: BLE001 — a bad frame must not 500 the view
            _log.debug("remote live-frame encode failed: %s", exc)
            return None
        with self._live_lock:
            if self._live_img is img:  # still the current frame → cache it
                self._live_png = png
        return png


__all__ = ["GalleryService"]
