# SPDX-License-Identifier: GPL-3.0-or-later
"""Logbook / gallery regressions from the v0.6.14 audit.

C-M7  Gallery Delete must not revert Logbook edits
C-M9  Logbook New… edits time/mode/frequency; a failed save keeps the form
Lows  scan_dirs dedupe, gallery timestamps, thumbnail prune race,
      QSO serial, grid length, shared uppercase helper
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.gallery.model import GalleryItem
from open_sstv.gallery.scanner import parse_time, scan_dirs
from open_sstv.gallery.thumbnail_cache import ThumbnailCache
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import LogbookStore

_T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


# --- C-M7 ----------------------------------------------------------------------


def test_clear_image_path_touches_only_that_column(tmp_path: Path) -> None:
    with LogbookStore(tmp_path / "log.db") as store:
        q = store.insert(QSO(direction="RX", callsign="W0AEZ", time_utc=_T,
                             image_path=tmp_path / "a.png", comment="old"))
        stale_snapshot = store.get(q.id)
        # Logbook edit after the gallery took its snapshot.
        edited = store.get(q.id)
        edited.comment = "edited in logbook"
        store.update(edited)
        store.clear_image_path(stale_snapshot.id)
        now = store.get(q.id)
        assert now.image_path is None
        assert now.comment == "edited in logbook"


@pytest.mark.gui
def test_gallery_delete_keeps_logbook_edit(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from open_sstv.config.schema import AppConfig
    from open_sstv.logbook.coordinator import LogbookCoordinator
    from open_sstv.ui.gallery_dialog import GalleryDialog

    img_dir = tmp_path / "img"
    img_dir.mkdir()
    img = img_dir / "2026-10-13_120000_martin_m1.png"
    Image.new("RGB", (32, 32)).save(img)
    cfg = AppConfig(images_save_dir=str(img_dir), logbook_db_path=str(tmp_path / "l.db"))
    coord = LogbookCoordinator(lambda: cfg)
    q = coord.store.insert(QSO(direction="RX", callsign="W0AEZ", time_utc=_T,
                               image_path=img, comment="old"))
    dlg = GalleryDialog(coord, lambda: cfg)
    qtbot.addWidget(dlg)
    dlg.focus_on_path(img)
    # Logbook edit lands after the gallery's snapshot.
    edited = coord.store.get(q.id)
    edited.comment = "edited"
    coord.store.update(edited)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dlg._on_delete()
    row = coord.store.get(q.id)
    assert not img.exists()
    assert row.image_path is None
    assert row.comment == "edited"
    coord.close()


# --- C-M9 ----------------------------------------------------------------------


@pytest.mark.gui
def test_new_entry_sets_time_mode_frequency(qtbot) -> None:
    from PySide6.QtCore import QDateTime, QTimeZone

    from open_sstv.ui.log_qso_dialog import LogQsoDialog

    saved: list[QSO] = []
    dlg = LogQsoDialog(
        QSO(direction="TX", time_utc=_T), editable_facts=True, on_save=saved.append
    )
    qtbot.addWidget(dlg)
    dlg._callsign.setText("k1abc")
    dlg._direction.setCurrentText("RX")
    dlg._time_edit.setDateTime(
        QDateTime.fromSecsSinceEpoch(
            int(datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()), QTimeZone.utc()
        )
    )
    dlg._mode.setCurrentText("Scottie 1")
    dlg._freq.setText("14.230")
    dlg.accept()
    assert dlg.result() == 1
    (q,) = saved
    assert q.callsign == "K1ABC"
    assert q.direction == "RX"
    assert q.time_utc == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert q.mode == "Scottie 1"
    assert q.frequency_hz == 14_230_000


@pytest.mark.gui
def test_failed_save_keeps_dialog_open(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from open_sstv.ui.log_qso_dialog import LogQsoDialog

    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[1]))
    )

    def boom(_q: QSO) -> None:
        raise RuntimeError("disk on fire")

    dlg = LogQsoDialog(QSO(direction="TX", time_utc=_T), editable_facts=True, on_save=boom)
    qtbot.addWidget(dlg)
    dlg.show()
    dlg._callsign.setText("K1ABC")
    dlg.accept()
    assert dlg.isVisible() and dlg.result() == 0
    assert dlg._callsign.text() == "K1ABC"
    assert warned == ["Save failed"]

    dlg._on_save = lambda q: None
    dlg._freq.setText("not-a-freq")
    dlg.accept()
    assert dlg.isVisible()
    assert warned[-1] == "Invalid frequency"


@pytest.mark.gui
def test_capture_dialog_keeps_facts_read_only(qtbot) -> None:
    from open_sstv.ui.log_qso_dialog import LogQsoDialog

    dlg = LogQsoDialog(QSO(direction="RX", time_utc=_T, mode="Martin M1"))
    qtbot.addWidget(dlg)
    assert not hasattr(dlg, "_time_edit")
    assert dlg.result_qso().mode == "Martin M1"


@pytest.mark.gui
def test_grid_max_length_and_uppercase(qtbot) -> None:
    from open_sstv.ui.log_qso_dialog import LogQsoDialog
    from open_sstv.ui.qso_state_widget import QSOStateWidget

    w = QSOStateWidget()
    qtbot.addWidget(w)
    w._grid.setText("en34ab12xyz")
    assert w._grid.text() == "EN34AB12"
    w._tocall.setText("w0aez")
    assert w._tocall.text() == "W0AEZ"
    dlg = LogQsoDialog(QSO(direction="RX", time_utc=_T))
    qtbot.addWidget(dlg)
    dlg._grid.setText("fn42aa99zz")
    assert dlg._grid.text() == "FN42AA99"


# --- QSO serial -------------------------------------------------------------------


@pytest.mark.gui
def test_qso_serial_populated_and_advances(qtbot) -> None:
    from open_sstv.ui.qso_state_widget import QSOStateWidget

    w = QSOStateWidget()
    qtbot.addWidget(w)
    assert w.get_state().serial == 1
    w.clear()  # nothing worked yet → stays 1
    assert w.get_state().serial == 1
    w._tocall.setText("K1ABC")
    w.clear()
    assert w.get_state().serial == 2
    w.set_serial(10)
    assert w.get_state().serial == 10


# --- gallery Lows -----------------------------------------------------------------


def test_scan_dirs_dedupes_by_resolved_path(tmp_path: Path) -> None:
    d = tmp_path / "img"
    d.mkdir()
    Image.new("RGB", (8, 8)).save(d / "a.png")
    link = tmp_path / "link"
    try:
        link.symlink_to(d, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    items = scan_dirs([d, link, tmp_path / "img" / ".." / "img"])
    assert len(items) == 1


def test_filename_time_is_used_for_timestamp() -> None:
    assert parse_time("2026-04-17_213512_scottie_s1") is not None
    assert parse_time("pd_120_2026-04-17") is None
    from open_sstv.gallery.scanner import parse_date

    stem = "2026-04-17_213512_scottie_s1"
    item = GalleryItem(
        path=Path(stem + ".png"), mtime=0.0, size_bytes=1,
        parsed_date=parse_date(stem), parsed_time=parse_time(stem),
    )
    assert item.timestamp_utc == datetime(2026, 4, 17, 21, 35, 12, tzinfo=UTC)


def test_date_only_uses_mtime_when_same_day() -> None:
    mtime = datetime(2026, 4, 17, 9, 30, tzinfo=UTC).timestamp()
    from datetime import date

    item = GalleryItem(path=Path("x.png"), mtime=mtime, size_bytes=1,
                       parsed_date=date(2026, 4, 17))
    assert item.timestamp_utc == datetime(2026, 4, 17, 9, 30, tzinfo=UTC)
    other = GalleryItem(path=Path("x.png"), mtime=0.0, size_bytes=1,
                        parsed_date=date(2026, 4, 17))
    assert other.timestamp_utc == datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


def test_gallery_filter_uses_utc_days() -> None:
    from PySide6.QtCore import QDate

    from open_sstv.ui.log_qso_dialog import qdate_day_start_utc

    assert qdate_day_start_utc(QDate(2026, 4, 17), local=False) == datetime(
        2026, 4, 17, tzinfo=UTC
    )


def test_thumbnail_prune_tolerates_vanishing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ThumbnailCache(tmp_path)
    for i in range(5):
        p = tmp_path / f"{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    real_stat = Path.stat

    def flaky_stat(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if self.name == "2.png":
            raise FileNotFoundError(self)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    removed = cache.prune(max_files=2)
    monkeypatch.undo()
    assert removed == 2
    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["2.png", "3.png", "4.png"]
