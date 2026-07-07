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


class TestOperations:
    def test_delete_removes_file_keeps_qso(
        self, qtbot, coordinator, images_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        coord, _cfg = coordinator
        img = _img(images_dir / "2026-06-10_120000_martin_m1.png")
        saved = _log(coord, img, callsign="K1ABC")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )
        dlg._on_delete()
        assert not img.exists()                          # file gone
        assert dlg._model.rowCount() == 0                # dropped from grid
        row = coord.store.get(saved.id)
        assert row is not None                           # QSO row survives
        assert row.image_path is None                    # link cleared

    def test_delete_declined_keeps_everything(
        self, qtbot, coordinator, images_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        img = _img(images_dir / "keep.png")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
        )
        dlg._on_delete()
        assert img.exists()
        assert dlg._model.rowCount() == 1

    def test_export_copies_original_untouched(
        self, qtbot, coordinator, images_dir: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QFileDialog

        img = _img(images_dir / "orig.png")
        dest = tmp_path / "exported.png"
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName",
            staticmethod(lambda *a, **k: (str(dest), "")),
        )
        dlg._on_export()
        assert dest.is_file()
        assert img.is_file()  # original never moved

    def test_resend_emits_path(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        img = _img(images_dir / "resend.png")
        dlg = _dialog(qtbot, coordinator)
        dlg._view.setCurrentIndex(dlg._model.index(0, 0))
        with qtbot.waitSignal(dlg.resend_requested, timeout=1000) as blocker:
            dlg._resend_btn.click()
        assert Path(blocker.args[0]) == img

    def test_buttons_disabled_without_selection(self, qtbot, coordinator) -> None:
        dlg = _dialog(qtbot, coordinator)
        assert not dlg._resend_btn.isEnabled()
        assert not dlg._export_btn.isEnabled()
        assert not dlg._delete_btn.isEnabled()


class TestFocusOnPath:
    def test_focus_selects_matching_item(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        _img(images_dir / "a.png")
        target = _img(images_dir / "b.png")
        dlg = _dialog(qtbot, coordinator)
        dlg.focus_on_path(target)
        sel = dlg._selected_item()
        assert sel is not None and sel.path == target

    def test_focus_missing_path_clears_selection(
        self, qtbot, coordinator, images_dir: Path
    ) -> None:
        _img(images_dir / "a.png")
        dlg = _dialog(qtbot, coordinator)
        dlg.focus_on_path(images_dir / "not-here.png")
        assert dlg._selected_item() is None


class TestMainWindowCrossLinks:
    """v0.5: the three Logbook↔Gallery↔TX cross-links wired in MainWindow."""

    @pytest.fixture
    def window(self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
        images = tmp_path / "images"
        images.mkdir()
        cfg.images_save_dir = str(images)
        monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
        w = MainWindow(rig=ManualRig())
        qtbot.addWidget(w)
        return w, images

    def test_gallery_open_qso_focuses_logbook(self, qtbot, window) -> None:
        w, images = window
        img = _img(images / "x.png")
        saved = w._logbook_coordinator.store.insert(
            QSO(direction="RX", callsign="W0AEZ", mode="Martin M1",
                time_utc=datetime(2026, 6, 1, tzinfo=UTC), image_path=img)
        )
        w._open_gallery()
        w._gallery_dialog.open_qso_requested.emit(saved)
        assert w._logbook_dialog is not None
        assert w._logbook_dialog.isVisible()
        assert w._logbook_dialog._selected_qso().id == saved.id

    def test_gallery_resend_loads_tx_panel(self, qtbot, window) -> None:
        w, images = window
        img = _img(images / "resend.png")
        w._open_gallery()
        w._gallery_dialog.resend_requested.emit(img)
        assert w._tx_panel._current_path == img

    def test_logbook_show_in_gallery_focuses(self, qtbot, window) -> None:
        w, images = window
        img = _img(images / "shown.png")
        w._logbook_coordinator.store.insert(
            QSO(direction="RX", callsign="K1ABC", mode="PD 120",
                time_utc=datetime(2026, 6, 1, tzinfo=UTC), image_path=img)
        )
        w._open_logbook()
        w._logbook_dialog.show_in_gallery_requested.emit(img)
        assert w._gallery_dialog is not None
        assert w._gallery_dialog.isVisible()
        sel = w._gallery_dialog._selected_item()
        assert sel is not None and sel.path == img
