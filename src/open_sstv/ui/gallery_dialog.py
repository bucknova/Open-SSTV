# SPDX-License-Identifier: GPL-3.0-or-later
"""Gallery window — browse RX / opt-in TX images, enriched by the logbook.

A detached, non-modal ``QDialog`` opened via **Tools → Gallery…**
(Cmd/Ctrl+G), paired with the Logbook window (``docs/v0.5-plan.md``).
The grid is a `QListView` in `IconMode`; the model
(``GalleryListModel``) loads thumbnails **lazily** — ``data`` only
produces a pixmap for the cells the view actually paints, so a plain
`QListView` stays smooth to the ≤10k target with no virtualization.

The window owns no image data: it scans ``images_save_dir`` (+ any
``gallery_extra_dirs``) on refresh and left-joins the logbook, exactly
the model the data layer established.  Operations (delete / export /
re-send) and the Logbook↔Gallery cross-links land in PR #3; this PR is
read-only browsing plus the ``open_qso_requested`` signal the
cross-link will consume.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractListModel,
    QDate,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from open_sstv.gallery import (
    ThumbnailCache,
    build_qso_index,
    enrich,
    scan_dirs,
)
from open_sstv.gallery.thumbnail_cache import DEFAULT_THUMB_SIZE
from open_sstv.ui.log_qso_dialog import format_frequency

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from open_sstv.config.schema import AppConfig
    from open_sstv.gallery.model import GalleryItem
    from open_sstv.logbook.coordinator import LogbookCoordinator

_log = logging.getLogger(__name__)

_FILTER_DEBOUNCE_MS = 300
_ICON = QSize(*DEFAULT_THUMB_SIZE)          # 160×120
_GRID = QSize(_ICON.width() + 24, _ICON.height() + 40)  # cell = icon + label
_PREVIEW_W, _PREVIEW_H = 320, 240
_PIXMAP_LRU_MAX = 512

#: Sort/group modes: (combo label, key).
_SORT_MODES: tuple[tuple[str, str], ...] = (
    ("Date (newest)", "date"),
    ("Callsign", "callsign"),
    ("Mode", "mode"),
)

_EMPTY_INDEX = QModelIndex()


class GalleryListModel(QAbstractListModel):
    """Lazy-thumbnail list model over the current filtered gallery items.

    ``QListView`` requests ``DecorationRole`` only for cells it paints,
    so thumbnails are generated on demand as the user scrolls; an LRU
    keyed by ``(path, mtime)`` keeps the visible viewport instant and
    survives refreshes without going stale on an edited file.
    """

    ITEM_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, thumb_cache: ThumbnailCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache = thumb_cache
        self._items: list[GalleryItem] = []
        self._sort_key: str = "date"
        self._lru: OrderedDict[str, QPixmap] = OrderedDict()
        self._placeholder: QPixmap | None = None

    # -- population ------------------------------------------------------

    def set_items(self, items: list[GalleryItem], sort_key: str) -> None:
        self.beginResetModel()
        self._items = items
        self._sort_key = sort_key
        self.endResetModel()

    def item_at(self, row: int) -> GalleryItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    # -- QAbstractListModel ---------------------------------------------

    def rowCount(  # noqa: N802 — Qt naming
        self, parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._label(item)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.path.as_posix()
        if role == self.ITEM_ROLE:
            return item
        return None

    # -- lazy rendering --------------------------------------------------

    def _label(self, item: GalleryItem) -> str:
        """Caption under the thumbnail, adapted to the active sort key."""
        if self._sort_key == "callsign":
            return item.callsign or "(unlogged)"
        if self._sort_key == "mode":
            return item.display_mode
        # date: UTC to match the sidebar's "When … UTC" and the rest of
        # the app (filenames, logbook display are all UTC) — a local
        # caption would read a day off from the detail panel.
        return item.timestamp_utc.strftime("%Y-%m-%d")

    def _thumbnail(self, item: GalleryItem) -> QPixmap:
        key = f"{item.path.as_posix()}|{item.mtime}"
        cached = self._lru.get(key)
        if cached is not None:
            self._lru.move_to_end(key)
            return cached
        thumb_path = self._cache.get_or_create(item.path)
        pixmap = QPixmap(str(thumb_path)) if thumb_path is not None else QPixmap()
        if pixmap.isNull():
            pixmap = self._placeholder_pixmap()
        self._lru[key] = pixmap
        if len(self._lru) > _PIXMAP_LRU_MAX:
            self._lru.popitem(last=False)
        return pixmap

    def _placeholder_pixmap(self) -> QPixmap:
        if self._placeholder is None:
            pm = QPixmap(_ICON)
            pm.fill(QColor("gray"))
            self._placeholder = pm
        return self._placeholder


class GalleryDialog(QDialog):
    """The gallery window.  Scans the filesystem and left-joins the log."""

    #: Emitted when the user clicks "→ QSO" on a logged image; carries
    #: the linked ``QSO``.  MainWindow consumes this in PR #3 to focus
    #: the Logbook window on that row.
    open_qso_requested = Signal(object)  # QSO

    def __init__(
        self,
        coordinator: LogbookCoordinator,
        config_getter: Callable[[], AppConfig],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._config_getter = config_getter
        self._thumb_cache = ThumbnailCache()
        self._all_items: list[GalleryItem] = []

        self.setWindowTitle("Gallery — Open-SSTV")
        self.resize(1000, 640)
        self.setMinimumSize(820, 500)
        self.setModal(False)

        root = QVBoxLayout(self)

        self._refresh_debounce = QTimer(self)
        self._refresh_debounce.setSingleShot(True)
        self._refresh_debounce.setInterval(_FILTER_DEBOUNCE_MS)
        # Text filters only re-filter the in-memory item list (no rescan),
        # so the debounce just coalesces keystrokes into one relayout.
        self._refresh_debounce.timeout.connect(self._apply_filters_and_sort)

        # --- Filter / sort bar -------------------------------------------
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Callsign:"))
        self._f_callsign = QLineEdit()
        self._f_callsign.setPlaceholderText("substring")
        self._f_callsign.setMaximumWidth(100)
        self._f_callsign.textChanged.connect(lambda _t: self._refresh_debounce.start())
        bar.addWidget(self._f_callsign)

        bar.addWidget(QLabel("Mode:"))
        self._f_mode = QLineEdit()
        self._f_mode.setPlaceholderText("e.g. Martin")
        self._f_mode.setMaximumWidth(100)
        self._f_mode.textChanged.connect(lambda _t: self._refresh_debounce.start())
        bar.addWidget(self._f_mode)

        self._f_from_on = QCheckBox("From:")
        self._f_from_on.toggled.connect(self._apply_filters_and_sort)
        bar.addWidget(self._f_from_on)
        self._f_from = QDateEdit(QDate.currentDate().addDays(-30))
        self._f_from.setCalendarPopup(True)
        self._f_from.dateChanged.connect(self._apply_filters_and_sort)
        bar.addWidget(self._f_from)

        self._f_until_on = QCheckBox("Until:")
        self._f_until_on.toggled.connect(self._apply_filters_and_sort)
        bar.addWidget(self._f_until_on)
        self._f_until = QDateEdit(QDate.currentDate())
        self._f_until.setCalendarPopup(True)
        self._f_until.dateChanged.connect(self._apply_filters_and_sort)
        bar.addWidget(self._f_until)

        bar.addSpacing(16)
        bar.addWidget(QLabel("Sort:"))
        self._sort_combo = QComboBox()
        for label, key in _SORT_MODES:
            self._sort_combo.addItem(label, key)
        self._sort_combo.currentIndexChanged.connect(self._apply_filters_and_sort)
        bar.addWidget(self._sort_combo)

        bar.addStretch(1)
        root.addLayout(bar)

        # --- Grid + detail splitter --------------------------------------
        self._model = GalleryListModel(self._thumb_cache, self)
        self._view = QListView()
        self._view.setModel(self._model)
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setFlow(QListView.Flow.LeftToRight)
        self._view.setWrapping(True)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setIconSize(_ICON)
        self._view.setGridSize(_GRID)
        self._view.setUniformItemSizes(True)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self._preview = QLabel()
        self._preview.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("border: 1px solid palette(mid);")
        detail_layout.addWidget(self._preview)

        form = QFormLayout()
        form.setHorizontalSpacing(8)
        self._d_callsign = QLabel()
        self._d_when = QLabel()
        self._d_mode_freq = QLabel()
        self._d_rsv = QLabel()
        self._d_operator = QLabel()
        self._d_notes = QLabel()
        self._d_notes.setWordWrap(True)
        self._d_source = QLabel()
        self._d_source.setWordWrap(True)
        form.addRow("Callsign:", self._d_callsign)
        form.addRow("When:", self._d_when)
        form.addRow("Mode / freq:", self._d_mode_freq)
        form.addRow("RSV S/R:", self._d_rsv)
        form.addRow("Operator:", self._d_operator)
        form.addRow("Notes:", self._d_notes)
        form.addRow("File:", self._d_source)
        detail_layout.addLayout(form)

        self._qso_btn = QPushButton("→ QSO")
        self._qso_btn.setToolTip("Open this image's contact in the Logbook")
        self._qso_btn.clicked.connect(self._on_open_qso)
        detail_layout.addWidget(self._qso_btn)
        detail_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._view)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        root.addWidget(splitter, stretch=1)

        # --- Action row ---------------------------------------------------
        actions = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(refresh_btn)
        actions.addStretch(1)
        self._count_label = QLabel("0 images")
        actions.addWidget(self._count_label)
        root.addLayout(actions)

        self.refresh()

    # === Scan + join ===

    def _source_dirs(self) -> list[Path]:
        from pathlib import Path  # noqa: PLC0415

        cfg = self._config_getter()
        dirs: list[Path] = []
        if cfg.images_save_dir:
            dirs.append(Path(cfg.images_save_dir))
        dirs.extend(Path(d) for d in cfg.gallery_extra_dirs)
        return dirs

    @Slot()
    def refresh(self) -> None:
        """Rescan the source dirs and re-join the logbook, then relayout."""
        items = scan_dirs(self._source_dirs())
        # The join is best-effort: a broken/locked logbook must not stop
        # the gallery showing files — they just render unenriched.
        try:
            index = build_qso_index(self._coordinator.store)
        except Exception as exc:  # noqa: BLE001
            _log.warning("gallery: logbook join unavailable: %s", exc)
        else:
            enrich(items, index)
        self._all_items = items
        self._apply_filters_and_sort()

    # === Filter + sort ===

    @staticmethod
    def _local_day_start_utc(d: QDate) -> datetime:
        """Midnight local time on the picked day, as UTC (logbook audit #5)."""
        return datetime(d.year(), d.month(), d.day()).astimezone(UTC)

    def _passes(self, item: GalleryItem) -> bool:
        cs = self._f_callsign.text().strip().upper()
        if cs and cs not in item.callsign.upper():
            return False
        md = self._f_mode.text().strip().upper()
        if md and md not in item.display_mode.upper():
            return False
        ts = item.timestamp_utc
        if self._f_from_on.isChecked() and ts < self._local_day_start_utc(
            self._f_from.date()
        ):
            return False
        return not (
            self._f_until_on.isChecked()
            and ts >= self._local_day_start_utc(self._f_until.date().addDays(1))
        )

    @Slot()
    def _apply_filters_and_sort(self) -> None:
        key = self._sort_combo.currentData() or "date"
        items = [i for i in self._all_items if self._passes(i)]

        if key == "callsign":
            # Unlogged (no callsign) sort last, then by newest.
            items.sort(
                key=lambda i: (
                    i.callsign.upper() or "￿",
                    -i.timestamp_utc.timestamp(),
                )
            )
        elif key == "mode":
            items.sort(
                key=lambda i: (i.display_mode.upper(), -i.timestamp_utc.timestamp())
            )
        else:  # date, newest first
            items.sort(key=lambda i: i.timestamp_utc, reverse=True)

        self._model.set_items(items, key)
        n = len(items)
        total = len(self._all_items)
        logged = sum(1 for i in items if i.is_logged)
        suffix = f" of {total}" if n != total else ""
        self._count_label.setText(
            f"{n} image{'s' if n != 1 else ''}{suffix} ({logged} logged)"
        )
        self._show_detail(None)

    # === Detail sidebar ===

    def _selected_item(self) -> GalleryItem | None:
        idxs = self._view.selectionModel().selectedIndexes()
        if not idxs:
            return None
        return self._model.item_at(idxs[0].row())

    @Slot()
    def _on_selection_changed(self) -> None:
        self._show_detail(self._selected_item())

    def _show_detail(self, item: GalleryItem | None) -> None:
        if item is None:
            self._preview.setText("No image selected")
            for lbl in (
                self._d_callsign,
                self._d_when,
                self._d_mode_freq,
                self._d_rsv,
                self._d_operator,
                self._d_notes,
                self._d_source,
            ):
                lbl.setText("—")
            self._qso_btn.setEnabled(False)
            return

        self._set_preview(item.path)
        q = item.qso
        self._d_callsign.setText(item.callsign or "(not logged)")
        self._d_when.setText(
            item.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        )
        freq = format_frequency(q.frequency_hz) if q is not None else "—"
        direction = item.direction or "—"
        self._d_mode_freq.setText(f"{item.display_mode} · {freq} · {direction}")
        if q is not None:
            self._d_rsv.setText(f"{q.rsv_sent or '—'} / {q.rsv_received or '—'}")
            op_bits = [b for b in (q.name, q.qth, q.grid) if b]
            self._d_operator.setText(", ".join(op_bits) if op_bits else "—")
            self._d_notes.setText(q.comment or "—")
        else:
            self._d_rsv.setText("—")
            self._d_operator.setText("—")
            self._d_notes.setText("—")
        self._d_source.setText(item.path.name)
        self._qso_btn.setEnabled(item.is_logged)

    def _set_preview(self, image_path: Path) -> None:
        if not image_path.is_file():
            self._preview.setText("Missing image\n(file moved or deleted)")
            return
        pm = QPixmap(str(image_path))
        if pm.isNull():
            self._preview.setText("Unreadable image")
            return
        self._preview.setPixmap(
            pm.scaled(
                _PREVIEW_W,
                _PREVIEW_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @Slot()
    def _on_open_qso(self) -> None:
        item = self._selected_item()
        if item is not None and item.qso is not None:
            self.open_qso_requested.emit(item.qso)

    # === Lifecycle ===

    def prune_cache(self) -> None:
        """Housekeep the thumbnail cache; called by MainWindow on close."""
        self._thumb_cache.prune()


__all__ = ["GalleryDialog", "GalleryListModel"]
