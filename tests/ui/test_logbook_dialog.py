# SPDX-License-Identifier: GPL-3.0-or-later
"""LogbookDialog — table model, filters, detail panel, delete flow.

Runs against a real ``LogbookStore`` in a tmp dir via a real
``LogbookCoordinator`` — the dialog has no seams to mock and the store
is fast enough that there's no reason to fake it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.logbook.coordinator import LogbookCoordinator
from open_sstv.logbook.model import QSO
from open_sstv.ui.logbook_dialog import LogbookDialog, QsoTableModel

pytestmark = pytest.mark.gui


@pytest.fixture
def coordinator(tmp_path: Path) -> LogbookCoordinator:
    cfg = AppConfig()
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    return LogbookCoordinator(lambda: cfg)


def _insert(coord: LogbookCoordinator, **kw: object) -> QSO:
    defaults: dict[str, object] = {
        "direction": "TX",
        "callsign": "K1ABC",
        "mode": "Martin M1",
        "time_utc": datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
        "frequency_hz": 14_230_000,
    }
    defaults.update(kw)
    return coord.store.insert(QSO(**defaults))  # type: ignore[arg-type]


class TestQsoTableModel:
    def test_columns_and_rows(self, qtbot, coordinator: LogbookCoordinator) -> None:
        model = QsoTableModel()
        model.set_qsos([_insert(coordinator), _insert(coordinator, callsign="N0XYZ")])
        assert model.rowCount() == 2
        assert model.columnCount() == 6

    def test_display_formats(self, qtbot, coordinator: LogbookCoordinator) -> None:
        model = QsoTableModel()
        model.set_qsos([_insert(coordinator, rsv_sent="595", rsv_received="575")])
        idx = model.index
        assert model.data(idx(0, 0)) == "2026-06-10 12:00"
        assert model.data(idx(0, 1)) == "TX"
        assert model.data(idx(0, 2)) == "K1ABC"
        assert model.data(idx(0, 3)) == "Martin M1"
        assert model.data(idx(0, 4)) == "14.230 MHz"
        assert model.data(idx(0, 5)) == "595 / 575"

    def test_draft_placeholder(self, qtbot, coordinator: LogbookCoordinator) -> None:
        model = QsoTableModel()
        model.set_qsos([_insert(coordinator, callsign="", direction="RX")])
        assert model.data(model.index(0, 2)) == "(draft)"

    def test_empty_rsv_is_dash(self, qtbot, coordinator: LogbookCoordinator) -> None:
        model = QsoTableModel()
        model.set_qsos([_insert(coordinator, rsv_sent="", rsv_received="")])
        assert model.data(model.index(0, 5)) == "—"


class TestDialogRefresh:
    def test_loads_rows_newest_first(self, qtbot, coordinator: LogbookCoordinator) -> None:
        _insert(coordinator, time_utc=datetime(2026, 6, 1, 8, 0, tzinfo=UTC))
        newer = _insert(
            coordinator,
            callsign="N0XYZ",
            time_utc=datetime(2026, 6, 11, 8, 0, tzinfo=UTC),
        )
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        assert dlg._model.rowCount() == 2
        assert dlg._model.qso_at(0).id == newer.id
        assert dlg._count_label.text() == "2 QSOs"

    def test_draft_count_in_label(self, qtbot, coordinator: LogbookCoordinator) -> None:
        _insert(coordinator)
        _insert(coordinator, callsign="", direction="RX")
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        assert dlg._count_label.text() == "2 QSOs (1 draft)"

    def test_callsign_filter(self, qtbot, coordinator: LogbookCoordinator) -> None:
        _insert(coordinator, callsign="K1ABC")
        _insert(coordinator, callsign="N0XYZ")
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._f_callsign.setText("k1")
        assert dlg._model.rowCount() == 1
        assert dlg._model.qso_at(0).callsign == "K1ABC"

    def test_direction_filter(self, qtbot, coordinator: LogbookCoordinator) -> None:
        _insert(coordinator, direction="TX")
        _insert(coordinator, callsign="", direction="RX")
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._f_direction.setCurrentText("RX")
        assert dlg._model.rowCount() == 1
        assert dlg._model.qso_at(0).direction == "RX"

    def test_date_range_filter_inclusive_until(
        self, qtbot, coordinator: LogbookCoordinator
    ) -> None:
        from PySide6.QtCore import QDate

        _insert(coordinator, time_utc=datetime(2026, 6, 5, 12, 0, tzinfo=UTC))
        _insert(
            coordinator,
            callsign="N0XYZ",
            time_utc=datetime(2026, 6, 10, 23, 59, tzinfo=UTC),
        )
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._f_from.setDate(QDate(2026, 6, 6))
        dlg._f_from_on.setChecked(True)
        dlg._f_until.setDate(QDate(2026, 6, 10))
        dlg._f_until_on.setChecked(True)
        # 6/5 excluded by since; 6/10 23:59 included because until is
        # inclusive of the whole picked day.
        assert dlg._model.rowCount() == 1
        assert dlg._model.qso_at(0).callsign == "N0XYZ"


class TestDetailPanel:
    def test_selection_populates_detail(
        self, qtbot, coordinator: LogbookCoordinator, tmp_path: Path
    ) -> None:
        img = tmp_path / "qso.png"
        Image.new("RGB", (320, 256), color=(40, 80, 120)).save(img)
        _insert(
            coordinator,
            name="Sam",
            qth="Boston",
            grid="FN42",
            comment="great colour",
            image_path=img,
        )
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._table.selectRow(0)
        assert dlg._d_callsign.text() == "K1ABC"
        assert "Martin M1" in dlg._d_mode_freq.text()
        assert "14.230 MHz" in dlg._d_mode_freq.text()
        assert dlg._d_op.text() == "Sam, Boston, FN42"
        assert dlg._d_comment.text() == "great colour"
        assert dlg._preview.pixmap() is not None
        assert not dlg._preview.pixmap().isNull()

    def test_missing_image_indicator(
        self, qtbot, coordinator: LogbookCoordinator, tmp_path: Path
    ) -> None:
        _insert(coordinator, image_path=tmp_path / "gone.png")
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._table.selectRow(0)
        assert "Missing image" in dlg._preview.text()

    def test_no_selection_disables_actions(
        self, qtbot, coordinator: LogbookCoordinator
    ) -> None:
        _insert(coordinator)
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        assert not dlg._edit_btn.isEnabled()
        assert not dlg._delete_btn.isEnabled()
        dlg._table.selectRow(0)
        assert dlg._edit_btn.isEnabled()
        assert dlg._delete_btn.isEnabled()


class TestDeleteFlow:
    def test_delete_removes_row_keeps_file(
        self,
        qtbot,
        coordinator: LogbookCoordinator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        img = tmp_path / "keepme.png"
        Image.new("RGB", (32, 32)).save(img)
        _insert(coordinator, image_path=img)
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._table.selectRow(0)
        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )
        dlg._on_delete()
        assert dlg._model.rowCount() == 0
        assert coordinator.store.count() == 0
        assert img.exists(), "deleting a QSO must never touch the image file"

    def test_delete_declined_keeps_row(
        self,
        qtbot,
        coordinator: LogbookCoordinator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        _insert(coordinator)
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._table.selectRow(0)
        monkeypatch.setattr(
            QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
        )
        dlg._on_delete()
        assert coordinator.store.count() == 1


class TestEditFlow:
    def test_edit_updates_row(
        self,
        qtbot,
        coordinator: LogbookCoordinator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from PySide6.QtWidgets import QDialog

        from open_sstv.ui import logbook_dialog as mod

        _insert(coordinator, comment="before")

        def fake_exec(self: object) -> int:
            # Simulate the operator changing the notes field then Save.
            self._comment.setText("after")  # type: ignore[attr-defined]
            return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr(mod.LogQsoDialog, "exec", fake_exec)
        dlg = LogbookDialog(coordinator)
        qtbot.addWidget(dlg)
        dlg._table.selectRow(0)
        dlg._on_edit()
        assert coordinator.store.list_qsos()[0].comment == "after"
