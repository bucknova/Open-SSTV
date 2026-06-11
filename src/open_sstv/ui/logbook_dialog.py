# SPDX-License-Identifier: GPL-3.0-or-later
"""Logbook window — browse, filter, edit, and ADIF-exchange the QSO log.

A separate non-modal ``QDialog`` opened via **Tools → Logbook…**
(Cmd/Ctrl+L), per the v0.4 plan: the main window's TX/RX splitter is
the live operating surface and stays untouched; detached logbooks are
the established convention (MMSSTV, fldigi, WSJT-X, JS8Call).

Layout: filter bar across the top; QSO table (newest first) on the
left; detail panel (image preview + full fields, read-only) on the
right; action buttons along the bottom.  All edits flow through
``LogQsoDialog`` — one editing surface, one save path — with the
detail panel's *Edit…* button, the toolbar *Edit*, and double-click
all opening the same form.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Slot,
)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from open_sstv import __version__
from open_sstv.logbook.adif import read_adif_file, write_adif_file
from open_sstv.logbook.model import QSO
from open_sstv.ui.log_qso_dialog import LogQsoDialog, format_frequency

if TYPE_CHECKING:
    from pathlib import Path

    from open_sstv.logbook.coordinator import LogbookCoordinator

_PREVIEW_W, _PREVIEW_H = 320, 240

#: Shared invalid-index default for the model overrides (B008: no call
#: in argument defaults).  Read-only, so one instance is safe.
_EMPTY_INDEX = QModelIndex()


class QsoTableModel(QAbstractTableModel):
    """Read-only table model over the current filtered QSO list."""

    _COLUMNS = ("Time (UTC)", "Dir", "Callsign", "Mode", "Frequency", "RSV S/R")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._qsos: list[QSO] = []

    # -- population ------------------------------------------------------

    def set_qsos(self, qsos: list[QSO]) -> None:
        self.beginResetModel()
        self._qsos = qsos
        self.endResetModel()

    def qso_at(self, row: int) -> QSO | None:
        if 0 <= row < len(self._qsos):
            return self._qsos[row]
        return None

    # -- QAbstractTableModel ----------------------------------------------

    def rowCount(  # noqa: N802 — Qt naming
        self, parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._qsos)

    def columnCount(  # noqa: N802 — Qt naming
        self, parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._COLUMNS)

    def headerData(  # noqa: N802 — Qt naming
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return self._COLUMNS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        qso = self._qsos[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return qso.time_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
            if col == 1:
                return qso.direction
            if col == 2:
                return qso.callsign or "(draft)"
            if col == 3:
                return qso.mode
            if col == 4:
                return format_frequency(qso.frequency_hz)
            if col == 5:
                if not qso.rsv_sent and not qso.rsv_received:
                    return "—"
                return f"{qso.rsv_sent or '—'} / {qso.rsv_received or '—'}"
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and col == 2
            and not qso.callsign
        ):
            return Qt.GlobalColor.gray
        return None


class LogbookDialog(QDialog):
    """The logbook window.  Owns no data — everything flows through the
    coordinator's store so the capture flow and this window always see
    the same rows."""

    def __init__(
        self, coordinator: LogbookCoordinator, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self.setWindowTitle("Logbook — Open-SSTV")
        self.resize(1000, 600)
        self.setMinimumSize(820, 480)
        # Non-modal tool window: the operator keeps working the radio
        # while the log is open.
        self.setModal(False)

        root = QVBoxLayout(self)

        # --- Filter bar --------------------------------------------------
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Callsign:"))
        self._f_callsign = QLineEdit()
        self._f_callsign.setPlaceholderText("substring")
        self._f_callsign.setMaximumWidth(110)
        self._f_callsign.textChanged.connect(self.refresh)
        filter_row.addWidget(self._f_callsign)

        filter_row.addWidget(QLabel("Mode:"))
        self._f_mode = QLineEdit()
        self._f_mode.setPlaceholderText("e.g. Martin")
        self._f_mode.setMaximumWidth(110)
        self._f_mode.textChanged.connect(self.refresh)
        filter_row.addWidget(self._f_mode)

        filter_row.addWidget(QLabel("Direction:"))
        self._f_direction = QComboBox()
        self._f_direction.addItems(["All", "RX", "TX"])
        self._f_direction.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self._f_direction)

        self._f_from_on = QCheckBox("From:")
        self._f_from_on.toggled.connect(self.refresh)
        filter_row.addWidget(self._f_from_on)
        self._f_from = QDateEdit(QDate.currentDate().addDays(-30))
        self._f_from.setCalendarPopup(True)
        self._f_from.dateChanged.connect(self.refresh)
        filter_row.addWidget(self._f_from)

        self._f_until_on = QCheckBox("Until:")
        self._f_until_on.toggled.connect(self.refresh)
        filter_row.addWidget(self._f_until_on)
        self._f_until = QDateEdit(QDate.currentDate())
        self._f_until.setCalendarPopup(True)
        self._f_until.dateChanged.connect(self.refresh)
        filter_row.addWidget(self._f_until)

        filter_row.addStretch(1)
        root.addLayout(filter_row)

        # --- Table + detail splitter --------------------------------------
        self._model = QsoTableModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._on_edit)
        self._table.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self._preview = QLabel()
        self._preview.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("border: 1px solid palette(mid);")
        detail_layout.addWidget(self._preview)

        detail_form = QFormLayout()
        detail_form.setHorizontalSpacing(8)
        self._d_callsign = QLabel()
        self._d_when = QLabel()
        self._d_mode_freq = QLabel()
        self._d_rsv = QLabel()
        self._d_op = QLabel()
        self._d_comment = QLabel()
        self._d_comment.setWordWrap(True)
        detail_form.addRow("Callsign:", self._d_callsign)
        detail_form.addRow("When:", self._d_when)
        detail_form.addRow("Mode / freq:", self._d_mode_freq)
        detail_form.addRow("RSV S/R:", self._d_rsv)
        detail_form.addRow("Operator:", self._d_op)
        detail_form.addRow("Notes:", self._d_comment)
        detail_layout.addLayout(detail_form)

        self._edit_btn_detail = QPushButton("Edit…")
        self._edit_btn_detail.clicked.connect(self._on_edit)
        detail_layout.addWidget(self._edit_btn_detail)
        detail_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._table)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter, stretch=1)

        # --- Action row ----------------------------------------------------
        actions = QHBoxLayout()
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self._on_new)
        actions.addWidget(new_btn)
        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._on_edit)
        actions.addWidget(self._edit_btn)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self._delete_btn)
        actions.addSpacing(24)
        import_btn = QPushButton("Import ADIF…")
        import_btn.clicked.connect(self._on_import_adif)
        actions.addWidget(import_btn)
        export_btn = QPushButton("Export ADIF…")
        export_btn.clicked.connect(self._on_export_adif)
        actions.addWidget(export_btn)
        actions.addSpacing(24)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(refresh_btn)
        actions.addStretch(1)
        self._count_label = QLabel("0 QSOs")
        actions.addWidget(self._count_label)
        root.addLayout(actions)

        self.refresh()

    # === Querying ===

    def _query_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if self._f_callsign.text().strip():
            kw["callsign"] = self._f_callsign.text().strip()
        if self._f_mode.text().strip():
            kw["mode"] = self._f_mode.text().strip()
        if self._f_direction.currentText() in ("RX", "TX"):
            kw["direction"] = self._f_direction.currentText()
        if self._f_from_on.isChecked():
            d = self._f_from.date()
            kw["since"] = datetime(d.year(), d.month(), d.day(), tzinfo=UTC)
        if self._f_until_on.isChecked():
            # The store's `until` is exclusive; a date picked in the UI
            # should be *inclusive* of that whole day.
            d = self._f_until.date().addDays(1)
            kw["until"] = datetime(d.year(), d.month(), d.day(), tzinfo=UTC)
        return kw

    @Slot()
    def refresh(self) -> None:
        """Re-query the store with the current filters."""
        try:
            qsos = self._coordinator.store.list_qsos(**self._query_kwargs())
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the window
            QMessageBox.warning(self, "Logbook unavailable", str(exc))
            return
        self._model.set_qsos(qsos)
        drafts = sum(1 for q in qsos if not q.callsign)
        total = f"{len(qsos)} QSO{'s' if len(qsos) != 1 else ''}"
        if drafts:
            total += f" ({drafts} draft{'s' if drafts != 1 else ''})"
        self._count_label.setText(total)
        self._show_detail(None)
        self._update_action_state()

    def _selected_qso(self) -> QSO | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self._model.qso_at(indexes[0].row())

    def _update_action_state(self) -> None:
        has_sel = self._selected_qso() is not None
        self._edit_btn.setEnabled(has_sel)
        self._edit_btn_detail.setEnabled(has_sel)
        self._delete_btn.setEnabled(has_sel)

    # === Detail panel ===

    @Slot()
    def _on_selection_changed(self) -> None:
        self._show_detail(self._selected_qso())
        self._update_action_state()

    def _show_detail(self, qso: QSO | None) -> None:
        if qso is None:
            self._preview.setText("No QSO selected")
            for label in (
                self._d_callsign,
                self._d_when,
                self._d_mode_freq,
                self._d_rsv,
                self._d_op,
                self._d_comment,
            ):
                label.setText("—")
            return
        self._set_preview(qso.image_path)
        self._d_callsign.setText(qso.callsign or "(draft — no callsign yet)")
        self._d_when.setText(
            qso.time_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        )
        self._d_mode_freq.setText(
            f"{qso.mode or '—'} · {format_frequency(qso.frequency_hz)} · {qso.direction}"
        )
        rsv = f"{qso.rsv_sent or '—'} / {qso.rsv_received or '—'}"
        self._d_rsv.setText(rsv)
        op_bits = [b for b in (qso.name, qso.qth, qso.grid) if b]
        self._d_op.setText(", ".join(op_bits) if op_bits else "—")
        self._d_comment.setText(qso.comment or "—")

    def _set_preview(self, image_path: Path | None) -> None:
        if image_path is None:
            self._preview.setText("No image")
            return
        if not image_path.is_file():
            self._preview.setText("Missing image\n(file moved or deleted)")
            return
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self._preview.setText("Unreadable image")
            return
        self._preview.setPixmap(
            pixmap.scaled(
                _PREVIEW_W,
                _PREVIEW_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # === Actions ===

    @Slot()
    def _on_new(self) -> None:
        """Manual entry — *Save & New* loops for batch keying."""
        while True:
            draft = QSO(direction="TX", time_utc=datetime.now(UTC))
            dlg = LogQsoDialog(draft, allow_save_and_new=True, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                break
            try:
                self._coordinator.save_draft(dlg.result_qso())
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Save failed", str(exc))
                break
            self.refresh()
            if not dlg.save_and_new:
                break

    @Slot()
    def _on_edit(self) -> None:
        qso = self._selected_qso()
        if qso is None:
            return
        dlg = LogQsoDialog(qso, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._coordinator.store.update(dlg.result_qso())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(exc))
        self.refresh()

    @Slot()
    def _on_delete(self) -> None:
        qso = self._selected_qso()
        if qso is None or qso.id is None:
            return
        who = qso.callsign or f"draft {qso.direction} {qso.mode}".strip()
        answer = QMessageBox.question(
            self,
            "Delete QSO",
            f"Delete the QSO with {who}?\n\n"
            "The image and audio files on disk are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._coordinator.store.delete(qso.id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Delete failed", str(exc))
        self.refresh()

    @Slot()
    def _on_export_adif(self) -> None:
        """Export the *currently filtered* rows — what you see is what
        you ship.  Drafts (no callsign) are skipped per ADIF writer
        default; the status message says how many."""
        qsos = [
            q
            for row in range(self._model.rowCount())
            if (q := self._model.qso_at(row)) is not None
        ]
        if not qsos:
            QMessageBox.information(self, "Export ADIF", "Nothing to export.")
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export ADIF",
            "open-sstv-logbook.adi",
            "ADIF files (*.adi *.adif);;All files (*)",
        )
        if not path_str:
            return
        from pathlib import Path as _Path  # noqa: PLC0415

        drafts = sum(1 for q in qsos if not q.callsign.strip())
        try:
            write_adif_file(
                _Path(path_str),
                qsos,
                station=self._coordinator.station_info(),
                program_version=__version__,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        msg = f"Exported {len(qsos) - drafts} QSOs."
        if drafts:
            msg += f"  {drafts} draft(s) without a callsign were skipped."
        QMessageBox.information(self, "Export ADIF", msg)

    @Slot()
    def _on_import_adif(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import ADIF",
            "",
            "ADIF files (*.adi *.adif);;All files (*)",
        )
        if not path_str:
            return
        from pathlib import Path as _Path  # noqa: PLC0415

        try:
            qsos = read_adif_file(
                _Path(path_str), mode_table=self._coordinator.mode_table()
            )
            added, skipped = self._coordinator.import_qsos(qsos)
        except Exception as exc:  # noqa: BLE001 — parse or store failure
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        msg = f"Imported {added} QSO{'s' if added != 1 else ''}."
        if skipped:
            msg += f"  Skipped {skipped} duplicate{'s' if skipped != 1 else ''}."
        QMessageBox.information(self, "Import ADIF", msg)
        self.refresh()


__all__ = ["LogbookDialog", "QsoTableModel"]
