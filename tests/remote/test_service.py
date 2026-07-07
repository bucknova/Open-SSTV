# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.service — headless read model (scan, enrich, id resolution)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import LogbookStore
from open_sstv.remote.service import GalleryService


def _img(path: Path, size: tuple[int, int] = (32, 24)) -> Path:
    Image.new("RGB", size, (20, 40, 60)).save(path)
    return path


def _service(tmp_path: Path, **overrides: object) -> GalleryService:
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    cfg = AppConfig(
        images_save_dir=str(images),
        logbook_db_path=str(tmp_path / "logbook.db"),
        **overrides,  # type: ignore[arg-type]
    )
    # Cache in a tmp dir so tests never touch the real user cache.
    thumbs = ThumbnailCache(cache_dir=tmp_path / "thumbs")
    return GalleryService(lambda: cfg, thumbnail_cache=thumbs)


class TestScan:
    def test_empty_gallery_is_empty_payload(self, tmp_path: Path) -> None:
        assert _service(tmp_path).payload() == []

    def test_lists_images_with_ids_and_parsed_mode(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        images = tmp_path / "images"
        _img(images / "2026-04-17_213512_scottie_s1.png")
        _img(images / "holiday.png")
        payload = svc.payload()
        assert len(payload) == 2
        by_name = {p["name"]: p for p in payload}
        assert by_name["2026-04-17_213512_scottie_s1.png"]["mode"] == "scottie_s1"
        assert by_name["holiday.png"]["mode"] == "Unknown"
        # Every item carries a non-empty, unique id.
        ids = [p["id"] for p in payload]
        assert all(ids) and len(set(ids)) == 2

    def test_non_images_ignored(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        images = tmp_path / "images"
        _img(images / "a.png")
        (images / "notes.txt").write_text("nope")
        assert len(svc.payload()) == 1


class TestResolve:
    def test_known_id_resolves_to_file(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        p = _img(tmp_path / "images" / "a.png")
        item_id = svc.payload()[0]["id"]
        assert svc.image_path(str(item_id)) == p

    def test_unknown_id_returns_none(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        _img(tmp_path / "images" / "a.png")
        svc.payload()
        # A never-issued id (path-traversal attempt substitute) must not resolve.
        assert svc.image_path("deadbeefdeadbeef") is None
        assert svc.thumbnail_path("deadbeefdeadbeef") is None

    def test_thumbnail_is_a_png(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        _img(tmp_path / "images" / "a.png")
        item_id = str(svc.payload()[0]["id"])
        thumb = svc.thumbnail_path(item_id)
        assert thumb is not None and thumb.suffix == ".png"
        with Image.open(thumb) as im:
            assert im.format == "PNG"

    def test_deleted_source_stops_resolving(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        p = _img(tmp_path / "images" / "a.png")
        item_id = str(svc.payload()[0]["id"])
        p.unlink()
        assert svc.image_path(item_id) is None


class TestEnrichment:
    def test_logbook_join_fills_callsign(self, tmp_path: Path) -> None:
        svc = _service(tmp_path)
        img = _img(tmp_path / "images" / "a.png")
        store = LogbookStore(tmp_path / "logbook.db")
        store.insert(QSO(direction="RX", callsign="K1ABC", mode="Scottie 1", image_path=img))
        store.close()
        item = next(p for p in svc.payload() if p["name"] == "a.png")
        assert item["logged"] is True
        assert item["callsign"] == "K1ABC"
        assert item["direction"] == "RX"

    def test_no_db_serves_unenriched(self, tmp_path: Path) -> None:
        # A view-only operator with no logbook.db: items still list, and
        # the service must NOT create the DB file.
        svc = _service(tmp_path)
        _img(tmp_path / "images" / "a.png")
        item = svc.payload()[0]
        assert item["logged"] is False
        assert item["callsign"] == ""
        assert not (tmp_path / "logbook.db").exists()
