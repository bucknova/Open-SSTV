# SPDX-License-Identifier: GPL-3.0-or-later
"""GalleryDialog + GalleryListModel — populate, filter, sort, detail, signal."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.gallery import ThumbnailCache, scan_dir
from open_sstv.logbook.coordinator import LogbookCoordinator
from open_sstv.logbook.model import QSO
from open_sstv.ui.gallery_dialog import GalleryDialog, GalleryListModel

pytestmark = pytest.mark.gui


def _img(path: Path, color=(30, 60, 90)) -> Path:
    Image.new("RGB", (64, 48), color).save(path)
    return path


@pytest.fixture
def images_dir(tmp_path: Path) -> Path:
    d = tmp_path / "images"
    d.mkdir()
    return d


@pytest.fixture
def coordinator(tmp_path: Path, images_dir: Path):
    cfg = AppConfig()
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(images_dir)
    return LogbookCoordinator(lambda: cfg), cfg


def _log(coord: LogbookCoordinator, image_path: Path, **kw: object) -> QSO:
    defaults: dict[str, object] = {
        "direction": "RX",
        "callsign": "K1ABC",
        "mode": "Martin M1",
        "time_utc": datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        "image_path": image_path,
    }
    defaults.update(kw)
    return coord.store.insert(QSO(**defaults))  # type: ignore[arg-type]


def _dialog(qtbot, coordinator) -> GalleryDialog:
    coord, cfg = coordinator
    dlg = GalleryDialog(coord, config_getter=lambda: cfg)
    qtbot.addWidget(dlg)
    return dlg


# ---------------------------------------------------------------------------
# GalleryListModel
# ---------------------------------------------------------------------------


class TestGalleryListModel:
    def test_lazy_thumbnail_is_pixmap(self, qtbot, tmp_path: Path) -> None:
        from PySide6.QtCore import Qt

        _img(tmp_path / "2026-06-10_120000_martin_m1.png")
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        model = GalleryListModel(cache)
        (item,) = scan_dir(tmp_path)
        model.set_items([item], "date")
        assert model.rowCount() == 1
        pm = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
        assert pm is not None and not pm.isNull()

    def test_label_adapts_to_sort_key(self, qtbot, tmp_path: Path) -> None:
        from PySide6.QtCore import Qt

        _img(tmp_path / "2026-06-10_120000_martin_m1.png")
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        (item,) = scan_dir(tmp_path)
        item.qso = None
        model = GalleryListModel(cache)
        disp = Qt.ItemDataRole.DisplayRole

        model.set_items([item], "mode")
        assert model.data(model.index(0, 0), disp) == "martin_m1"
        model.set_items([item], "callsign")
        assert model.data(model.index(0, 0), disp) == "(unlogged)"
        model.set_items([item], "date")
        assert "2026-06-10" in model.data(model.index(0, 0), disp)

    def test_corrupt_image_gets_placeholder(self, qtbot, tmp_path: Path) -> None:
        from PySide6.QtCore import Qt

        bad = tmp_path / "2026-06-10_120000_pd_120.png"
        bad.write_bytes(b"not a png")
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        (item,) = scan_dir(tmp_path)
        model = GalleryListModel(cache)
        model.set_items([item], "date")
        pm = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
        assert pm is not None and not pm.isNull()  # gray placeholder, not crash


# ---------------------------------------------------------------------------
# GalleryDialog
# ---------------------------------------------------------------------------


class TestPopulate:
    def test_scans_and_counts(self, qtbot, coordinator, images_dir: Path) -> None:
        _img(images_dir / "2026-06-10_120000_martin_m1.png")
        _img(images_dir / "2026-06-11_090000_pd_120.png")
        dlg = _dialog(qtbot, coordinator)
        assert dlg._model.rowCount() == 2
        assert "2 images" in dlg._count_label.text()

    def test_join_enriches_logged_image(self, qtbot, coordinator, images_dir: Path) -> None:
        coord, _cfg = coordinator
        logged = _img(images_dir / "2026-06-10_120000_martin_m1.png")
        _img(images_dir / "unlogged.png")
        _log(coord, logged, callsign="W0AEZ")
        dlg = _dialog(qtbot, coordinator)
        assert "(1 logged)" in dlg._count_label.text()
        items = [dlg._model.item_at(i) for i in range(dlg._model.rowCount())]
        by_call = {i.callsign for i in items}
        assert "W0AEZ" in by_call and "" in by_call

    def test_empty_dir_is_empty_grid(self, qtbot, coordinator) -> None:
        dlg = _dialog(qtbot, coordinator)
        assert dlg._model.rowCount() == 0
        assert "0 images" in dlg._count_label.text()


class TestFilterSort:
    def test_callsign_filter(self, qtbot, coordinator, images_dir: Path) -> None:
        coord, _cfg = coordinator
        a = _img(images_dir / "a.png")
        b = _img(images_dir / "b.png")
        _log(coord, a, callsign="K1ABC")
        _log(coord, b, callsign="N0XYZ")
        dlg = _dialog(qtbot, coordinator)
        dlg._f_callsign.setText("k1")
        dlg._apply_filters_and_sort()  # bypass debounce timer in test
        assert dlg._model.rowCount() == 1
        assert dlg._model.item_at(0).callsign == "K1ABC"

    def test_mode_filter_uses_parsed_or_logged(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        _img(images_dir / "2026-06-10_120000_martin_m1.png")
        _img(images_dir / "2026-06-10_130000_pd_120.png")
        dlg = _dialog(qtbot, coordinator)
        dlg._f_mode.setText("martin")
        dlg._apply_filters_and_sort()
        assert dlg._model.rowCount() == 1
        assert "martin" in dlg._model.item_at(0).display_mode.lower()

    def test_sort_by_callsign_puts_unlogged_last(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        coord, _cfg = coordinator
        _img(images_dir / "z.png")  # unlogged
        a = _img(images_dir / "a.png")
        _log(coord, a, callsign="AA1AA")
        dlg = _dialog(qtbot, coordinator)
        dlg._sort_combo.setCurrentIndex(1)  # Callsign
        dlg._apply_filters_and_sort()
        first = dlg._model.item_at(0)
        last = dlg._model.item_at(dlg._model.rowCount() - 1)
        assert first.callsign == "AA1AA"
        assert last.callsign == ""  # unlogged sorts last

    def test_date_filter(self, qtbot, coordinator, images_dir: Path) -> None:
        from datetime import timedelta

        from PySide6.QtCore import QDate

        def local(y, m, d, h=12):
            return (datetime(y, m, d) + timedelta(hours=h)).astimezone(UTC)

        coord, _cfg = coordinator
        old = _img(images_dir / "old.png")
        new = _img(images_dir / "new.png")
        _log(coord, old, callsign="OLD", time_utc=local(2026, 6, 1))
        _log(coord, new, callsign="NEW", time_utc=local(2026, 6, 20))
        dlg = _dialog(qtbot, coordinator)
        dlg._f_from.setDate(QDate(2026, 6, 10))
        dlg._f_from_on.setChecked(True)
        assert dlg._model.rowCount() == 1
        assert dlg._model.item_at(0).callsign == "NEW"


class TestDetailAndSignal:
    def test_selection_populates_sidebar(self, qtbot, coordinator, images_dir: Path) -> None:
        coord, _cfg = coordinator
        img = _img(images_dir / "2026-06-10_120000_martin_m1.png")
        _log(coord, img, callsign="W0AEZ", frequency_hz=14_230_000,
             name="Kevin", comment="nice bars")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        assert dlg._d_callsign.text() == "W0AEZ"
        assert "Martin M1" in dlg._d_mode_freq.text()
        assert "14.230 MHz" in dlg._d_mode_freq.text()
        assert dlg._d_notes.text() == "nice bars"
        assert dlg._qso_btn.isEnabled()

    def test_unlogged_selection_disables_qso_button(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        _img(images_dir / "unlogged.png")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        assert dlg._d_callsign.text() == "(not logged)"
        assert not dlg._qso_btn.isEnabled()

    def test_open_qso_emits_signal(self, qtbot, coordinator, images_dir: Path) -> None:
        coord, _cfg = coordinator
        img = _img(images_dir / "x.png")
        saved = _log(coord, img, callsign="W0AEZ")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        with qtbot.waitSignal(dlg.open_qso_requested, timeout=1000) as blocker:
            dlg._qso_btn.click()
        assert blocker.args[0].id == saved.id


class TestExtraDirs:
    def test_scans_configured_extra_dir(self, qtbot, tmp_path: Path) -> None:
        main = tmp_path / "images"
        extra = tmp_path / "archive"
        main.mkdir()
        extra.mkdir()
        _img(main / "one.png")
        _img(extra / "two.png")
        cfg = AppConfig()
        cfg.logbook_db_path = str(tmp_path / "logbook.db")
        cfg.images_save_dir = str(main)
        cfg.gallery_extra_dirs = [str(extra)]
        coord = LogbookCoordinator(lambda: cfg)
        dlg = GalleryDialog(coord, config_getter=lambda: cfg)
        qtbot.addWidget(dlg)
        assert dlg._model.rowCount() == 2


class TestMainWindowIntegration:
    """Tools → Gallery… opens the detached window."""

    def test_open_gallery_creates_and_shows(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from unittest.mock import MagicMock

        import numpy as np

        from open_sstv.radio.base import ManualRig
        from open_sstv.ui.main_window import MainWindow

        monkeypatch.setattr(
            "open_sstv.ui.workers.encode",
            MagicMock(return_value=np.zeros(100, dtype=np.int16)),
        )
        monkeypatch.setattr(
            "open_sstv.ui.workers.output_stream.play_blocking", MagicMock()
        )
        monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
        cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
        cfg.logbook_db_path = str(tmp_path / "logbook.db")
        cfg.images_save_dir = str(tmp_path / "images")
        monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
        w = MainWindow(rig=ManualRig())
        qtbot.addWidget(w)
        assert w._gallery_dialog is None
        w._open_gallery()
        assert w._gallery_dialog is not None
        assert w._gallery_dialog.isVisible()
