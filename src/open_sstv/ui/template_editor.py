# SPDX-License-Identifier: GPL-3.0-or-later
"""Form-based template editor for the v0.3 image-template compositor.

A non-modal QDialog with three panels:

* **Left** — layer list with visibility checkboxes, add/reorder/delete buttons.
* **Center** — live-rendered preview of the template at the chosen mode's
  aspect ratio.  Rebuilt 300 ms after the last property change so dragging
  spin-boxes doesn't thrash the renderer.
* **Right** — scrollable property inspector whose fields swap based on the
  selected layer's type (Text / Rect / Gradient / Photo / RX Image / Image).

The dialog never mutates the caller's template; it works on a deep copy
and emits ``template_saved(Path)`` after a successful save so the gallery
can refresh.

v0.3.0 scope note
─────────────────
This is the *form* editor; drag-and-drop canvas placement is a v0.3.1
concern.  The form covers the full TextLayer / RectLayer / GradientLayer
/ PhotoLayer property surface so users can author every starter-pack
template through the UI alone.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.templates import manager
from open_sstv.templates.fonts import list_available_fonts
from open_sstv.templates.model import (
    GradientLayer,
    Layer,
    PhotoLayer,
    QSOState,
    RectLayer,
    RxImageLayer,
    StationImageLayer,
    StrokeSpec,
    Template,
    TextLayer,
    TXContext,
)
from open_sstv.templates.renderer import render_template
from open_sstv.ui.utils import pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from open_sstv.config.schema import AppConfig

_log = logging.getLogger(__name__)

_DEBOUNCE_MS: int = 300

_ANCHOR_LABELS: tuple[tuple[str, str], ...] = (
    ("Top-Left", "TL"),
    ("Top-Center", "TC"),
    ("Top-Right", "TR"),
    ("Center-Left", "CL"),
    ("Center", "C"),
    ("Center-Right", "CR"),
    ("Bottom-Left", "BL"),
    ("Bottom-Center", "BC"),
    ("Bottom-Right", "BR"),
    ("Fill (cover canvas)", "FILL"),
)

_ROLE_LABELS: tuple[tuple[str, str], ...] = (
    ("CQ", "cq"),
    ("Reply", "reply"),
    ("Closing (73)", "closing"),
    ("Custom", "custom"),
)

_FIT_LABELS: tuple[str, ...] = ("contain", "cover", "stretch")
_ALIGN_LABELS: tuple[str, ...] = ("left", "center", "right")
_ORIENTATION_LABELS: tuple[tuple[str, str], ...] = (
    ("Horizontal", "horizontal"),
    ("Stacked", "stacked"),
)

_TOKEN_CHEAT_SHEET: str = (
    "Common tokens (resolved at TX time):\n"
    "\n"
    "  %c   {callsign}    Your callsign\n"
    "  %o   {tocall}      Other op's callsign\n"
    "  %r   {rst}         RST report\n"
    "  %name_o {tocallname}  Other op's name\n"
    "  %g   {grid}        Your grid square\n"
    "  %n   {name}        Your name\n"
    "  %m   {mode}        Current SSTV mode\n"
    "  %d   {date}        UTC date\n"
    "  %t   {time}        UTC time\n"
    "  %f   {freq}        Rig frequency (MHz)\n"
    "  %b   {band}        Ham band (40m, 20m, …)\n"
    "  %q   {qso_serial}  QSO serial #\n"
    "  %v   {version}     Open-SSTV version\n"
    "  %note {note}       Free-form QSO note\n"
    "\n"
    "Use \\n in the text for a line break.  Unknown tokens pass\n"
    "through unchanged (forward-compatible)."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _make_color_button(color: tuple[int, int, int, int]) -> QPushButton:
    """Return a small swatch button initialised to *color*."""
    btn = QPushButton()
    btn.setFixedSize(QSize(48, 22))
    _apply_color_to_button(btn, color)
    return btn


def _apply_color_to_button(
    btn: QPushButton, color: tuple[int, int, int, int]
) -> None:
    r, g, b, a = color
    btn.setStyleSheet(
        f"QPushButton {{ background: rgba({r},{g},{b},{a/255:.2f}); "
        f"border: 1px solid palette(mid); }}"
    )
    btn.setToolTip(f"R={r} G={g} B={b} A={a}")


def _pick_color(
    initial: tuple[int, int, int, int], parent: QWidget, title: str
) -> tuple[int, int, int, int] | None:
    """Open a color dialog seeded with *initial*; return new RGBA or None.

    The native color dialog can cause a non-modal parent to drop behind the
    main window on some platforms (observed on macOS). After the dialog
    closes we re-raise and re-activate the parent so focus returns there.
    """
    qcol = QColor(initial[0], initial[1], initial[2], initial[3])
    out = QColorDialog.getColor(
        qcol,
        parent,
        title,
        QColorDialog.ColorDialogOption.ShowAlphaChannel,
    )
    if parent is not None:
        parent.raise_()
        parent.activateWindow()
    if not out.isValid():
        return None
    return (out.red(), out.green(), out.blue(), out.alpha())


# ---------------------------------------------------------------------------
# Main editor dialog
# ---------------------------------------------------------------------------


class TemplateEditor(QDialog):
    """Non-modal QDialog for editing a single Template.

    Parameters
    ----------
    template:
        The Template to edit.  Pass a fresh ``Template(name="…")`` for
        New; pass an existing one (deep-copied internally) for Edit.
    path:
        File path the template was loaded from (drives "Save"); ``None``
        for new templates so "Save" prompts for a destination.
    app_config:
        AppConfig used by the renderer for token resolution.
    templates_dir:
        Directory new templates are written into.  Defaults to
        :func:`manager.default_templates_dir`.
    current_photo:
        Current TX photo to use as the base in the preview.  May be ``None``.
    current_mode:
        SSTV ``Mode`` driving the preview's aspect ratio; falls back to
        ``Mode("scottie_s1")`` if ``None``.

    Signals
    -------
    template_saved(Path):
        Emitted after Save succeeds.  Carries the file path the gallery
        should refresh from.
    """

    template_saved = Signal(object)  # Path

    def __init__(
        self,
        template: Template,
        *,
        path: Path | None = None,
        app_config: AppConfig,
        templates_dir: Path | None = None,
        current_photo: PILImage | None = None,
        current_mode: Mode | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Non-modal so the user can keep the main window visible.
        self.setModal(False)
        self.setWindowTitle("Template Editor")
        # The inspector needs ~360 px to show its widest form rows without
        # horizontal scroll; combined with the layer list and a usable
        # preview the dialog needs ~1200 px.
        self.setMinimumSize(1200, 720)
        self.resize(1280, 780)

        self._template: Template = copy.deepcopy(template)
        self._path: Path | None = path
        self._app_config = app_config
        self._templates_dir = templates_dir or manager.default_templates_dir()
        self._photo: PILImage | None = current_photo
        self._mode: Mode = current_mode or Mode("scottie_s1")

        # Sample QSO state for preview token resolution.  Pre-filled with
        # plausible values so a fresh editor immediately shows live text
        # instead of empty placeholders.
        self._sample_qso = QSOState(tocall="W0XYZ", rst="595", tocall_name="Alex")

        # Set when bulk-loading widget values so callbacks don't echo back
        # into the model and trigger redundant preview renders.
        self._loading_form: bool = False

        # Debounced preview re-render — wired to every property edit.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._render_preview)

        self._build_ui()
        self._refresh_layer_list()
        if self._template.layers:
            self._layer_list.setCurrentRow(0)
        else:
            self._populate_inspector(None)
        self._render_preview()

    # === Public API ===

    def set_photo(self, photo: PILImage | None) -> None:
        """Update the photo used in the preview (call when TX photo changes)."""
        self._photo = photo
        self._schedule_preview()

    def set_mode(self, mode: Mode) -> None:
        """Update the preview's mode (drives aspect ratio)."""
        self._mode = mode
        self._schedule_preview()

    # === UI construction ===

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        outer.addLayout(self._build_top_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_layer_panel())
        splitter.addWidget(self._build_preview_panel())
        inspector = self._build_inspector_panel()
        # Inspector needs enough width for QFormLayout + a vertical scrollbar
        # so colour-pickers, stroke rows, etc. don't push into horizontal scroll.
        inspector.setMinimumWidth(360)
        splitter.addWidget(inspector)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([220, 560, 400])
        outer.addWidget(splitter, stretch=1)

        outer.addWidget(self._build_bottom_bar())

    def _build_top_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        bar.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit(self._template.name)
        self._name_edit.setMinimumWidth(180)
        self._name_edit.textEdited.connect(self._on_template_name_edited)
        bar.addWidget(self._name_edit, stretch=1)

        bar.addWidget(QLabel("Role:"))
        self._role_combo = QComboBox()
        for label, value in _ROLE_LABELS:
            self._role_combo.addItem(label, value)
        self._role_combo.setCurrentIndex(
            self._role_combo.findData(self._template.role)
        )
        self._role_combo.currentIndexChanged.connect(self._on_role_changed)
        bar.addWidget(self._role_combo)

        bar.addWidget(QLabel("Preview mode:"))
        self._mode_combo = QComboBox()
        for mode in Mode:
            spec = MODE_TABLE[mode]
            self._mode_combo.addItem(
                f"{mode.value} ({spec.width}×{spec.display_height})",
                mode,
            )
        idx = self._mode_combo.findData(self._mode)
        if idx >= 0:
            self._mode_combo.setCurrentIndex(idx)
        self._mode_combo.currentIndexChanged.connect(self._on_preview_mode_changed)
        bar.addWidget(self._mode_combo)

        bar.addStretch(0)

        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save_clicked)
        bar.addWidget(self._save_btn)

        self._save_as_btn = QPushButton("Save As…")
        self._save_as_btn.clicked.connect(self._on_save_as_clicked)
        bar.addWidget(self._save_as_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bar.addWidget(close_btn)

        return bar

    def _build_layer_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        v.addWidget(QLabel("Layers"))

        self._layer_list = QListWidget()
        self._layer_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self._layer_list.currentRowChanged.connect(self._on_layer_selected)
        self._layer_list.itemChanged.connect(self._on_layer_item_changed)
        v.addWidget(self._layer_list, stretch=1)

        # --- Add layer buttons ---
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        for label, kind in (
            ("+Text", "text"),
            ("+Rect", "rect"),
            ("+Image", "photo"),
            ("+RX Image", "rx_image"),
            ("+Gradient", "gradient"),
        ):
            btn = QPushButton(label)
            btn.setProperty("layer_kind", kind)
            btn.clicked.connect(self._on_add_layer_clicked)
            add_row.addWidget(btn)
        v.addLayout(add_row)

        # --- Reorder / delete row ---
        action_row = QHBoxLayout()
        self._up_btn = QPushButton("↑")
        self._up_btn.setToolTip("Move layer up (renders later, on top)")
        self._up_btn.setMaximumWidth(32)
        self._up_btn.clicked.connect(self._on_move_up)
        action_row.addWidget(self._up_btn)
        self._down_btn = QPushButton("↓")
        self._down_btn.setToolTip("Move layer down (renders earlier, behind)")
        self._down_btn.setMaximumWidth(32)
        self._down_btn.clicked.connect(self._on_move_down)
        action_row.addWidget(self._down_btn)
        action_row.addStretch(1)
        self._delete_btn = QPushButton("✕ Delete")
        self._delete_btn.clicked.connect(self._on_delete_layer)
        action_row.addWidget(self._delete_btn)
        v.addLayout(action_row)

        return frame

    def _build_preview_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("Preview"))
        header.addStretch(1)
        v.addLayout(header)

        self._preview_label = QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(320, 240)
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._preview_label.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: #1a1a1a; }"
        )
        self._preview_label.setText("(no preview yet)")
        v.addWidget(self._preview_label, stretch=1)

        return frame

    def _build_inspector_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        v.addWidget(QLabel("Properties"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        v.addWidget(scroll, stretch=1)

        # Container that the dynamic form is rebuilt into.
        self._inspector_host = QWidget()
        self._inspector_layout = QVBoxLayout(self._inspector_host)
        self._inspector_layout.setContentsMargins(2, 2, 2, 2)
        self._inspector_layout.setSpacing(8)
        self._inspector_layout.addStretch(1)
        scroll.setWidget(self._inspector_host)

        return frame

    def _build_bottom_bar(self) -> QWidget:
        box = QGroupBox("Sample QSO state (preview only)")
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        h.addWidget(QLabel("ToCall:"))
        self._sample_tocall = QLineEdit(self._sample_qso.tocall)
        self._sample_tocall.setMaximumWidth(110)
        self._sample_tocall.textEdited.connect(self._on_sample_changed)
        h.addWidget(self._sample_tocall)

        h.addWidget(QLabel("RST:"))
        self._sample_rst = QLineEdit(self._sample_qso.rst)
        self._sample_rst.setMaximumWidth(60)
        self._sample_rst.textEdited.connect(self._on_sample_changed)
        h.addWidget(self._sample_rst)

        h.addWidget(QLabel("Name:"))
        self._sample_name = QLineEdit(self._sample_qso.tocall_name)
        self._sample_name.setMaximumWidth(110)
        self._sample_name.textEdited.connect(self._on_sample_changed)
        h.addWidget(self._sample_name)

        h.addStretch(1)

        cheat_btn = QToolButton()
        cheat_btn.setText("Tokens …")
        cheat_btn.setToolTip("Show available template tokens")
        cheat_btn.clicked.connect(self._show_token_cheatsheet)
        h.addWidget(cheat_btn)

        return box

    # === Layer list ===

    def _refresh_layer_list(self) -> None:
        """Rebuild the QListWidget from ``self._template.layers``."""
        self._layer_list.blockSignals(True)
        self._layer_list.clear()
        for layer in self._template.layers:
            item = QListWidgetItem(self._layer_label(layer))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked
            )
            self._layer_list.addItem(item)
        self._layer_list.blockSignals(False)

    @staticmethod
    def _layer_label(layer: Layer) -> str:
        """One-line description for the layer list."""
        type_icon = {
            "text": "T",
            "rect": "■",
            "gradient": "▰",
            "photo": "\U0001f5bc",
            "rx_image": "RX",
            "station_image": "✮",
            "pattern": "▒",
        }.get(layer.type, "?")
        name = layer.name or layer.id or layer.type
        return f"  [{type_icon}]  {name}"

    def _selected_layer(self) -> Layer | None:
        row = self._layer_list.currentRow()
        if 0 <= row < len(self._template.layers):
            return self._template.layers[row]
        return None

    @Slot(int)
    def _on_layer_selected(self, row: int) -> None:
        if 0 <= row < len(self._template.layers):
            self._populate_inspector(self._template.layers[row])
        else:
            self._populate_inspector(None)

    @Slot(QListWidgetItem)
    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        """Visibility checkbox toggled in the layer list."""
        row = self._layer_list.row(item)
        if not (0 <= row < len(self._template.layers)):
            return
        new_visible = item.checkState() == Qt.CheckState.Checked
        if self._template.layers[row].visible != new_visible:
            self._template.layers[row].visible = new_visible
            self._schedule_preview()

    @Slot()
    def _on_add_layer_clicked(self) -> None:
        kind = self.sender().property("layer_kind")
        new_layer = self._build_default_layer(kind)
        if new_layer is None:
            return
        self._template.layers.append(new_layer)
        self._refresh_layer_list()
        self._layer_list.setCurrentRow(len(self._template.layers) - 1)
        self._schedule_preview()

    def _build_default_layer(self, kind: str) -> Layer | None:
        existing_ids = {l.id for l in self._template.layers}
        idx = 1
        while f"{kind}_{idx}" in existing_ids:
            idx += 1
        layer_id = f"{kind}_{idx}"

        if kind == "text":
            return TextLayer(
                id=layer_id,
                name="Text",
                text_raw="%c",
                anchor="BC",
                offset_y_pct=4.0,
                font_family="DejaVu Sans Bold",
                font_size_pct=8.0,
                fill=(255, 255, 255, 255),
                align="center",
            )
        if kind == "rect":
            return RectLayer(
                id=layer_id,
                name="Rectangle",
                anchor="BC",
                width_pct=100.0,
                height_pct=18.0,
                fill=(0, 0, 0, 180),
            )
        if kind == "gradient":
            return GradientLayer(
                id=layer_id,
                name="Gradient",
                anchor="FILL",
                from_color=(0, 0, 0, 200),
                to_color=(0, 0, 0, 0),
                angle_deg=270.0,
            )
        if kind == "photo":
            return PhotoLayer(
                id=layer_id,
                name="Photo",
                anchor="FILL",
                fit="cover",
            )
        if kind == "rx_image":
            return RxImageLayer(
                id=layer_id,
                name="RX Image",
                anchor="BR",
                width_pct=30.0,
                height_pct=25.0,
                offset_x_pct=2.0,
                offset_y_pct=2.0,
                fit="cover",
            )
        return None

    @Slot()
    def _on_move_up(self) -> None:
        row = self._layer_list.currentRow()
        if row <= 0:
            return
        layers = self._template.layers
        layers[row - 1], layers[row] = layers[row], layers[row - 1]
        self._refresh_layer_list()
        self._layer_list.setCurrentRow(row - 1)
        self._schedule_preview()

    @Slot()
    def _on_move_down(self) -> None:
        row = self._layer_list.currentRow()
        layers = self._template.layers
        if row < 0 or row >= len(layers) - 1:
            return
        layers[row + 1], layers[row] = layers[row], layers[row + 1]
        self._refresh_layer_list()
        self._layer_list.setCurrentRow(row + 1)
        self._schedule_preview()

    @Slot()
    def _on_delete_layer(self) -> None:
        row = self._layer_list.currentRow()
        if not (0 <= row < len(self._template.layers)):
            return
        del self._template.layers[row]
        self._refresh_layer_list()
        if self._template.layers:
            self._layer_list.setCurrentRow(min(row, len(self._template.layers) - 1))
        else:
            self._populate_inspector(None)
        self._schedule_preview()

    # === Inspector ===

    def _populate_inspector(self, layer: Layer | None) -> None:
        """Rebuild the right-panel form for the currently selected layer."""
        # Drop existing children.
        while self._inspector_layout.count():
            item = self._inspector_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if layer is None:
            placeholder = QLabel(
                "No layer selected.\n\nUse the +Text / +Rect / +Image / +RX Image "
                "/ +Gradient buttons to add a layer, or click an entry in the list."
            )
            placeholder.setWordWrap(True)
            placeholder.setStyleSheet("color: palette(mid);")
            self._inspector_layout.addWidget(placeholder)
            self._inspector_layout.addStretch(1)
            return

        self._loading_form = True
        try:
            self._add_common_fields(layer)
            if isinstance(layer, TextLayer):
                self._add_text_fields(layer)
            elif isinstance(layer, RectLayer):
                self._add_rect_fields(layer)
            elif isinstance(layer, GradientLayer):
                self._add_gradient_fields(layer)
            elif isinstance(layer, (PhotoLayer, RxImageLayer, StationImageLayer)):
                self._add_image_fields(layer)
            self._inspector_layout.addStretch(1)
        finally:
            self._loading_form = False

    # --- Inspector field helpers --------------------------------------------

    def _add_common_fields(self, layer: Layer) -> None:
        box = QGroupBox("Common")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        id_edit = QLineEdit(layer.id)
        id_edit.editingFinished.connect(self._on_id_finished)
        self._field_id = id_edit
        form.addRow("ID:", id_edit)

        name_edit = QLineEdit(layer.name)
        name_edit.textEdited.connect(self._on_layer_name_edited)
        self._field_layer_name = name_edit
        form.addRow("Name:", name_edit)

        visible_cb = QCheckBox()
        visible_cb.setChecked(layer.visible)
        visible_cb.toggled.connect(self._on_visible_toggled)
        self._field_visible = visible_cb
        form.addRow("Visible:", visible_cb)

        opacity_spin = QDoubleSpinBox()
        opacity_spin.setRange(0.0, 1.0)
        opacity_spin.setDecimals(2)
        opacity_spin.setSingleStep(0.05)
        opacity_spin.setValue(layer.opacity)
        opacity_spin.valueChanged.connect(self._on_opacity_changed)
        self._field_opacity = opacity_spin
        form.addRow("Opacity:", opacity_spin)

        anchor_combo = QComboBox()
        for label, value in _ANCHOR_LABELS:
            anchor_combo.addItem(label, value)
        idx = anchor_combo.findData(layer.anchor)
        if idx >= 0:
            anchor_combo.setCurrentIndex(idx)
        anchor_combo.currentIndexChanged.connect(self._on_anchor_changed)
        self._field_anchor = anchor_combo
        form.addRow("Anchor:", anchor_combo)

        self._field_offset_x = self._make_pct_spin(
            layer.offset_x_pct, self._on_offset_x_changed, mn=-100.0, mx=100.0
        )
        form.addRow("Offset X (%):", self._field_offset_x)

        self._field_offset_y = self._make_pct_spin(
            layer.offset_y_pct, self._on_offset_y_changed, mn=-100.0, mx=100.0
        )
        form.addRow("Offset Y (%):", self._field_offset_y)

        self._field_width = self._make_pct_spin(
            layer.width_pct if layer.width_pct is not None else 100.0,
            self._on_width_changed,
            mn=0.0, mx=400.0,
        )
        form.addRow("Width (%):", self._field_width)

        self._field_height = self._make_pct_spin(
            layer.height_pct if layer.height_pct is not None else 100.0,
            self._on_height_changed,
            mn=0.0, mx=400.0,
        )
        form.addRow("Height (%):", self._field_height)

        self._inspector_layout.addWidget(box)

    @staticmethod
    def _make_pct_spin(
        initial: float, slot, *, mn: float = 0.0, mx: float = 100.0
    ) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(mn, mx)
        s.setDecimals(2)
        s.setSingleStep(0.5)
        s.setValue(initial)
        s.valueChanged.connect(slot)
        return s

    def _add_text_fields(self, layer: TextLayer) -> None:
        box = QGroupBox("Text")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        text_edit = QLineEdit(layer.text_raw)
        text_edit.setPlaceholderText("e.g. CQ CQ DE %c")
        text_edit.textEdited.connect(self._on_text_raw_edited)
        self._field_text_raw = text_edit
        form.addRow("Text:", text_edit)

        font_combo = QComboBox()
        fonts = list_available_fonts()
        # Always include the layer's family — if a user typed a custom name
        # earlier we keep it visible rather than silently swapping it out.
        if layer.font_family not in fonts:
            fonts = [layer.font_family, *fonts]
        for f in fonts:
            font_combo.addItem(f)
        font_combo.setCurrentText(layer.font_family)
        font_combo.setEditable(True)
        font_combo.currentTextChanged.connect(self._on_font_family_changed)
        self._field_font_family = font_combo
        form.addRow("Font:", font_combo)

        size_spin = QDoubleSpinBox()
        size_spin.setRange(0.5, 80.0)
        size_spin.setDecimals(1)
        size_spin.setSingleStep(0.5)
        size_spin.setValue(layer.font_size_pct)
        size_spin.valueChanged.connect(self._on_font_size_changed)
        self._field_font_size = size_spin
        form.addRow("Size (% of frame H):", size_spin)

        align_combo = QComboBox()
        for a in _ALIGN_LABELS:
            align_combo.addItem(a)
        align_combo.setCurrentText(layer.align)
        align_combo.currentTextChanged.connect(self._on_align_changed)
        self._field_align = align_combo
        form.addRow("Align:", align_combo)

        orientation_combo = QComboBox()
        for label, value in _ORIENTATION_LABELS:
            orientation_combo.addItem(label, value)
        idx = orientation_combo.findData(layer.orientation)
        if idx >= 0:
            orientation_combo.setCurrentIndex(idx)
        orientation_combo.currentIndexChanged.connect(self._on_orientation_changed)
        self._field_orientation = orientation_combo
        form.addRow("Orientation:", orientation_combo)

        # --- Fill colour ---
        fill_btn = _make_color_button(layer.fill)
        fill_btn.clicked.connect(self._on_pick_text_fill)
        self._field_fill_btn = fill_btn
        form.addRow("Fill:", fill_btn)

        # --- Stroke ---
        stroke_row = QHBoxLayout()
        self._field_stroke_color_btn = _make_color_button(
            layer.stroke.color if layer.stroke else (0, 0, 0, 255)
        )
        self._field_stroke_color_btn.clicked.connect(self._on_pick_stroke_color)
        stroke_row.addWidget(self._field_stroke_color_btn)
        stroke_w = QSpinBox()
        stroke_w.setRange(0, 32)
        stroke_w.setSuffix(" px")
        stroke_w.setValue(layer.stroke.width_px if layer.stroke else 0)
        stroke_w.valueChanged.connect(self._on_stroke_width_changed)
        self._field_stroke_width = stroke_w
        stroke_row.addWidget(stroke_w, stretch=1)
        wrap = QWidget()
        wrap.setLayout(stroke_row)
        form.addRow("Stroke:", wrap)

        # --- Slashed zero ---
        slashed_cb = QCheckBox("0 → Ø in callsigns")
        slashed_cb.setChecked(layer.slashed_zero)
        slashed_cb.toggled.connect(self._on_slashed_zero_toggled)
        self._field_slashed_zero = slashed_cb
        form.addRow("", slashed_cb)

        self._inspector_layout.addWidget(box)

    def _add_rect_fields(self, layer: RectLayer) -> None:
        box = QGroupBox("Rectangle")
        form = QFormLayout(box)
        fill_btn = _make_color_button(layer.fill)
        fill_btn.clicked.connect(self._on_pick_rect_fill)
        self._field_rect_fill_btn = fill_btn
        form.addRow("Fill:", fill_btn)
        self._inspector_layout.addWidget(box)

    def _add_gradient_fields(self, layer: GradientLayer) -> None:
        box = QGroupBox("Gradient")
        form = QFormLayout(box)
        fb = _make_color_button(layer.from_color)
        fb.clicked.connect(self._on_pick_gradient_from)
        self._field_grad_from_btn = fb
        form.addRow("From:", fb)
        tb = _make_color_button(layer.to_color)
        tb.clicked.connect(self._on_pick_gradient_to)
        self._field_grad_to_btn = tb
        form.addRow("To:", tb)
        ang = QDoubleSpinBox()
        ang.setRange(0.0, 360.0)
        ang.setDecimals(1)
        ang.setSingleStep(5.0)
        ang.setValue(layer.angle_deg)
        ang.valueChanged.connect(self._on_gradient_angle_changed)
        self._field_grad_angle = ang
        form.addRow("Angle (deg):", ang)
        self._inspector_layout.addWidget(box)

    def _add_image_fields(self, layer: PhotoLayer | RxImageLayer | StationImageLayer) -> None:
        box = QGroupBox("Image")
        form = QFormLayout(box)
        fit_combo = QComboBox()
        for f in _FIT_LABELS:
            fit_combo.addItem(f)
        fit_combo.setCurrentText(layer.fit)
        fit_combo.currentTextChanged.connect(self._on_image_fit_changed)
        self._field_image_fit = fit_combo
        form.addRow("Fit:", fit_combo)
        self._inspector_layout.addWidget(box)

    # === Field-change slots ==================================================

    @Slot(str)
    def _on_template_name_edited(self, text: str) -> None:
        self._template.name = text
        # Window title hint, no preview refresh needed.
        self.setWindowTitle(f"Template Editor — {text}" if text else "Template Editor")

    @Slot(int)
    def _on_role_changed(self, _idx: int) -> None:
        value = self._role_combo.currentData()
        if value:
            self._template.role = value

    @Slot(int)
    def _on_preview_mode_changed(self, _idx: int) -> None:
        data = self._mode_combo.currentData()
        if isinstance(data, Mode):
            self._mode = data
            self._schedule_preview()

    # --- Common-field slots ---

    @Slot()
    def _on_id_finished(self) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        new_id = self._field_id.text().strip() or layer.id
        if new_id == layer.id:
            return
        # Ensure uniqueness.
        used = {l.id for l in self._template.layers if l is not layer}
        if new_id in used:
            QMessageBox.warning(
                self, "Duplicate ID", f"Layer ID {new_id!r} is already used."
            )
            self._field_id.setText(layer.id)
            return
        layer.id = new_id

    @Slot(str)
    def _on_layer_name_edited(self, text: str) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.name = text
        row = self._layer_list.currentRow()
        item = self._layer_list.item(row)
        if item is not None:
            item.setText(self._layer_label(layer))

    @Slot(bool)
    def _on_visible_toggled(self, checked: bool) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.visible = checked
        # Sync the list checkbox without re-entering this slot.
        row = self._layer_list.currentRow()
        item = self._layer_list.item(row)
        if item is not None:
            self._layer_list.blockSignals(True)
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self._layer_list.blockSignals(False)
        self._schedule_preview()

    @Slot(float)
    def _on_opacity_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.opacity = value
        self._schedule_preview()

    @Slot(int)
    def _on_anchor_changed(self, _idx: int) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        value = self._field_anchor.currentData()
        if value:
            layer.anchor = value
            self._schedule_preview()

    @Slot(float)
    def _on_offset_x_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.offset_x_pct = value
        self._schedule_preview()

    @Slot(float)
    def _on_offset_y_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.offset_y_pct = value
        self._schedule_preview()

    @Slot(float)
    def _on_width_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        # Treat 100% as "no override" so the renderer's None-default path
        # is preserved for layers that should fill the canvas.
        layer.width_pct = value
        self._schedule_preview()

    @Slot(float)
    def _on_height_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if layer is None or self._loading_form:
            return
        layer.height_pct = value
        self._schedule_preview()

    # --- Text-field slots ---

    @Slot(str)
    def _on_text_raw_edited(self, text: str) -> None:
        layer = self._selected_layer()
        if isinstance(layer, TextLayer) and not self._loading_form:
            layer.text_raw = text
            self._schedule_preview()

    @Slot(str)
    def _on_font_family_changed(self, family: str) -> None:
        layer = self._selected_layer()
        if isinstance(layer, TextLayer) and not self._loading_form:
            layer.font_family = family
            self._schedule_preview()

    @Slot(float)
    def _on_font_size_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if isinstance(layer, TextLayer) and not self._loading_form:
            layer.font_size_pct = value
            self._schedule_preview()

    @Slot(str)
    def _on_align_changed(self, value: str) -> None:
        layer = self._selected_layer()
        if isinstance(layer, TextLayer) and not self._loading_form:
            layer.align = value  # type: ignore[assignment]
            self._schedule_preview()

    @Slot(int)
    def _on_orientation_changed(self, _idx: int) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, TextLayer) or self._loading_form:
            return
        value = self._field_orientation.currentData()
        if value:
            layer.orientation = value  # type: ignore[assignment]
            self._schedule_preview()

    @Slot()
    def _on_pick_text_fill(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, TextLayer):
            return
        new = _pick_color(layer.fill, self, "Text Fill Colour")
        if new is None:
            return
        layer.fill = new
        _apply_color_to_button(self._field_fill_btn, new)
        self._schedule_preview()

    @Slot()
    def _on_pick_stroke_color(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, TextLayer):
            return
        current = layer.stroke.color if layer.stroke else (0, 0, 0, 255)
        new = _pick_color(current, self, "Stroke Colour")
        if new is None:
            return
        width = layer.stroke.width_px if layer.stroke else 0
        if width > 0:
            layer.stroke = StrokeSpec(color=new, width_px=width)
        else:
            # Remember the colour for next time the user nudges the width.
            layer.stroke = StrokeSpec(color=new, width_px=0)
        _apply_color_to_button(self._field_stroke_color_btn, new)
        self._schedule_preview()

    @Slot(int)
    def _on_stroke_width_changed(self, value: int) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, TextLayer) or self._loading_form:
            return
        if value <= 0:
            layer.stroke = None
        else:
            current_color = layer.stroke.color if layer.stroke else (0, 0, 0, 255)
            layer.stroke = StrokeSpec(color=current_color, width_px=value)
        self._schedule_preview()

    @Slot(bool)
    def _on_slashed_zero_toggled(self, checked: bool) -> None:
        layer = self._selected_layer()
        if isinstance(layer, TextLayer) and not self._loading_form:
            layer.slashed_zero = checked
            self._schedule_preview()

    # --- Rect / Gradient / Image slots ---

    @Slot()
    def _on_pick_rect_fill(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, RectLayer):
            return
        new = _pick_color(layer.fill, self, "Rectangle Fill")
        if new is None:
            return
        layer.fill = new
        _apply_color_to_button(self._field_rect_fill_btn, new)
        self._schedule_preview()

    @Slot()
    def _on_pick_gradient_from(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, GradientLayer):
            return
        new = _pick_color(layer.from_color, self, "Gradient Start Colour")
        if new is None:
            return
        layer.from_color = new
        _apply_color_to_button(self._field_grad_from_btn, new)
        self._schedule_preview()

    @Slot()
    def _on_pick_gradient_to(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, GradientLayer):
            return
        new = _pick_color(layer.to_color, self, "Gradient End Colour")
        if new is None:
            return
        layer.to_color = new
        _apply_color_to_button(self._field_grad_to_btn, new)
        self._schedule_preview()

    @Slot(float)
    def _on_gradient_angle_changed(self, value: float) -> None:
        layer = self._selected_layer()
        if isinstance(layer, GradientLayer) and not self._loading_form:
            layer.angle_deg = value
            self._schedule_preview()

    @Slot(str)
    def _on_image_fit_changed(self, value: str) -> None:
        layer = self._selected_layer()
        if isinstance(layer, (PhotoLayer, RxImageLayer, StationImageLayer)) and not self._loading_form:
            layer.fit = value  # type: ignore[assignment]
            self._schedule_preview()

    # === Sample QSO ==========================================================

    @Slot()
    def _on_sample_changed(self) -> None:
        self._sample_qso = QSOState(
            tocall=self._sample_tocall.text(),
            rst=self._sample_rst.text() or "595",
            tocall_name=self._sample_name.text(),
        )
        self._schedule_preview()

    @Slot()
    def _show_token_cheatsheet(self) -> None:
        QMessageBox.information(self, "Template tokens", _TOKEN_CHEAT_SHEET)

    # === Preview =============================================================

    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def _render_preview(self) -> None:
        spec = MODE_TABLE[self._mode]
        ctx = TXContext(
            mode_display_name=self._mode.value,
            frame_size=(spec.width, spec.display_height),
            photo_image=self._photo,
        )
        try:
            img = render_template(
                self._template, self._sample_qso, self._app_config, ctx
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Preview render failed: %s", exc)
            self._preview_label.setText(f"Render error: {exc}")
            return
        pix = pil_to_pixmap(img)
        scaled = pix.scaled(
            self._preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Re-scale the existing pixmap; preview itself doesn't need
        # re-rendering on resize.
        if self._preview_label.pixmap() is not None and not self._preview_label.pixmap().isNull():
            self._schedule_preview()

    # === Saving ==============================================================

    @Slot()
    def _on_save_clicked(self) -> None:
        if not self._template.name.strip():
            QMessageBox.warning(self, "Name required", "Please enter a template name before saving.")
            return
        try:
            if self._path is not None:
                manager.save(self._template, self._templates_dir, filename=self._path.name)
                saved_path = self._path
            else:
                saved_path = manager.save(self._template, self._templates_dir)
                self._path = saved_path
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"Could not write template:\n{exc}")
            return
        self.template_saved.emit(saved_path)
        self._save_btn.setText("Saved ✓")
        QTimer.singleShot(1500, self._restore_save_label)

    @Slot()
    def _on_save_as_clicked(self) -> None:
        if not self._template.name.strip():
            QMessageBox.warning(self, "Name required", "Please enter a template name before saving.")
            return
        try:
            saved_path = manager.save(self._template, self._templates_dir)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"Could not write template:\n{exc}")
            return
        self._path = saved_path
        self.template_saved.emit(saved_path)
        QMessageBox.information(self, "Saved", f"Saved to {saved_path.name}.")

    @Slot()
    def _restore_save_label(self) -> None:
        self._save_btn.setText("Save")


__all__ = ["TemplateEditor"]
