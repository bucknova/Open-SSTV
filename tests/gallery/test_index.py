# SPDX-License-Identifier: GPL-3.0-or-later
"""gallery.index — the filesystem × logbook join, all four cases."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.gallery.index import build_qso_index, enrich
from open_sstv.gallery.scanner import scan_dir
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import LogbookStore


@pytest.fixture
def store(tmp_path: Path):
    with LogbookStore(tmp_path / "logbook.db") as s:
        yield s


def _img(path: Path) -> Path:
    Image.new("RGB", (16, 16), (0, 0, 0)).save(path)
    return path


def _log(store: LogbookStore, image_path: Path | None, **kw: object) -> QSO:
    defaults: dict[str, object] = {
        "direction": "RX",
        "callsign": "K1ABC",
        "mode": "Martin M1",
        "time_utc": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        "image_path": image_path,
    }
    defaults.update(kw)
    return store.insert(QSO(**defaults))  # type: ignore[arg-type]


class TestBuildIndex:
    def test_indexes_only_image_linked_rows(self, store, tmp_path: Path) -> None:
        img = _img(tmp_path / "a.png")
        _log(store, img, callsign="K1ABC")
        _log(store, None, callsign="N0IMG")  # no image → not in index
        index = build_qso_index(store)
        assert list(index) == [img.as_posix()]
        assert index[img.as_posix()].callsign == "K1ABC"

    def test_shared_image_keeps_latest(self, store, tmp_path: Path) -> None:
        img = _img(tmp_path / "shared.png")
        _log(store, img, callsign="OLD", time_utc=datetime(2026, 6, 1, 8, 0, tzinfo=UTC))
        _log(store, img, callsign="NEW", time_utc=datetime(2026, 6, 2, 8, 0, tzinfo=UTC))
        index = build_qso_index(store)
        assert index[img.as_posix()].callsign == "NEW"


class TestEnrichFourCases:
    """docs/v0.5-plan.md §1: file+QSO, file-only, QSO-only(dangling), neither."""

    def test_file_with_qso_gets_enriched(self, store, tmp_path: Path) -> None:
        img = _img(tmp_path / "2026-06-01_120000_martin_m1.png")
        _log(store, img, callsign="K1ABC")
        items = scan_dir(tmp_path)
        enrich(items, build_qso_index(store))
        (item,) = items
        assert item.is_logged
        assert item.callsign == "K1ABC"
        assert item.display_mode == "Martin M1"  # QSO mode wins over parsed
        assert item.direction == "RX"

    def test_file_without_qso_stays_bare(self, store, tmp_path: Path) -> None:
        _img(tmp_path / "2026-06-01_120000_pd_120.png")  # never logged
        items = scan_dir(tmp_path)
        enrich(items, build_qso_index(store))
        (item,) = items
        assert not item.is_logged
        assert item.callsign == ""
        assert item.display_mode == "pd_120"  # falls back to parsed value
        assert item.direction is None

    def test_dangling_qso_produces_no_item(self, store, tmp_path: Path) -> None:
        # QSO links an image that isn't on disk → the gallery (which is
        # file-driven) simply has no item for it.
        _log(store, tmp_path / "deleted.png", callsign="K1ABC")
        items = scan_dir(tmp_path)  # empty dir
        enrich(items, build_qso_index(store))
        assert items == []

    def test_qso_mode_overrides_parsed_mode(self, store, tmp_path: Path) -> None:
        # Filename says scottie_s1 but the QSO was logged as PD 120
        # (e.g. re-saved image) — the authoritative QSO wins.
        img = _img(tmp_path / "2026-06-01_120000_scottie_s1.png")
        _log(store, img, mode="PD 120")
        items = scan_dir(tmp_path)
        enrich(items, build_qso_index(store))
        assert items[0].display_mode == "PD 120"


class TestStoreFindByImagePath:
    def test_finds_linked_row(self, store, tmp_path: Path) -> None:
        img = _img(tmp_path / "x.png")
        saved = _log(store, img, callsign="W0AEZ")
        found = store.find_by_image_path(img)
        assert found is not None and found.id == saved.id

    def test_accepts_str_and_path(self, store, tmp_path: Path) -> None:
        img = _img(tmp_path / "y.png")
        _log(store, img)
        assert store.find_by_image_path(str(img)) is not None
        assert store.find_by_image_path(img) is not None

    def test_unlinked_returns_none(self, store, tmp_path: Path) -> None:
        assert store.find_by_image_path(tmp_path / "nope.png") is None
