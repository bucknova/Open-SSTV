# SPDX-License-Identifier: GPL-3.0-or-later
"""Full CRUD editor dialog for QSO templates.

Opened from the gear icon on the ``QSOTemplateBar``.  The user can add,
remove, rename, and reorder templates and their overlays.  Changes are
saved to ``templates.toml`` on Accept.
"""
from __future__ import annotations

from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from open_sstv.config.templates import (
    QSOTemplate,
    QSOTemplateOverlay,
    resolve_placeholders,
    save_templates,
)
from open_sstv.ui.draw_text import POSITIONS, draw_text_overlay


class TemplateEditorDialog(QDialog):
    """Dialog for creating, editing, and deleting QSO templates."""

    def __init__(
        self,
        templates: list[QSOTemplate],
        mycall: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit QSO Templates")
        self.setMinimumSize(700, 500)

        self._mycall = mycall
        # Deep copy so edits don't mutate the caller's list until Accept.
        # ``x`` / ``y`` MUST be round-tripped (OP-03): templates saved with
        # explicit pixel coordinates (via hand-edited TOML) would otherwise
        # be silently stripped by the dialog and erased from disk on Accept.
        self._templates = [
            QSOTemplate(
                name=t.name,
                overlays=[
                    QSOTemplateOverlay(
                        text=o.text,
                        position=o.position,
                        size=o.size,
                        color=o.color,
                        x=o.x,
                        y=o.y,
                    )
                    for o in t.overlays
                ],
            )
            for t in templates
        ]

        main_layout = QVBoxLayout(self)
        root = QHBoxLayout()

        # === Left: template list ===
        left = QVBoxLayout()
        left.addWidget(QLabel("Templates"))
        self._tpl_list = QListWidget()
        self._tpl_list.currentRowChanged.connect(self._on_template_selected)
        left.addWidget(self._tpl_list)

        btn_row = QHBoxLayout()
        self._add_tpl_btn = QPushButton("Add")
        self._add_tpl_btn.clicked.connect(self._add_template)
        btn_row.addWidget(self._add_tpl_btn)
        self._remove_tpl_btn = QPushButton("Remove")
        self._remove_tpl_btn.clicked.connect(self._remove_template)
        btn_row.addWidget(self._remove_tpl_btn)
        left.addLayout(btn_row)

        root.addLayout(left, stretch=1)

        # === Right: template detail ===
        right = QVBoxLayout()

        # Template name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._on_name_changed)
        name_row.addWidget(self._name_edit)
        right.addLayout(name_row)

        # Overlay list
        overlay_group = QGroupBox("Text Overlays")
        overlay_layout = QVBoxLayout(overlay_group)
        self._overlay_list = QListWidget()
        self._overlay_list.currentRowChanged.connect(self._on_overlay_selected)
        overlay_layout.addWidget(self._overlay_list)

        ov_btn_row = QHBoxLayout()
        self._add_ov_btn = QPushButton("Add Overlay")
        self._add_ov_btn.clicked.connect(self._add_overlay)
        ov_btn_row.addWidget(self._add_ov_btn)
        self._remove_ov_btn = QPushButton("Remove")
        self._remove_ov_btn.clicked.connect(self._remove_overlay)
        ov_btn_row.addWidget(self._remove_ov_btn)
        overlay_layout.addLayout(ov_btn_row)
        right.addWidget(overlay_group)

        # Overlay detail fields
        detail_group = QGroupBox("Overlay Details")
        detail_layout = QVBoxLayout(detail_group)

        # Text
        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("Text:"))
        self._text_edit = QLineEdit()
        self._text_edit.setPlaceholderText("e.g. CQ CQ DE {mycall} K")
        self._text_edit.textChanged.connect(self._on_overlay_field_changed)
        text_row.addWidget(self._text_edit)
        detail_layout.addLayout(text_row)

        # Position + Size
        pos_size_row = QHBoxLayout()
        pos_size_row.addWidget(QLabel("Position:"))
        self._position_combo = QComboBox()
        for p in POSITIONS:
            self._position_combo.addItem(p)
        self._position_combo.currentTextChanged.connect(self._on_overlay_field_changed)
        pos_size_row.addWidget(self._position_combo)

        pos_size_row.addWidget(QLabel("Size:"))
        self._size_spin = QSpinBox()
        self._size_spin.setRange(8, 72)
        self._size_spin.setValue(24)
        self._size_spin.setMinimumWidth(60)
        self._size_spin.valueChanged.connect(self._on_overlay_field_changed)
        pos_size_row.addWidget(self._size_spin)
        detail_layout.addLayout(pos_size_row)

        # Color
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Color:"))
        self._color_swatch = QLabel()
        self._color_swatch.setFixedSize(24, 24)
        self._color_swatch.setStyleSheet("background: white; border: 1px solid gray;")
        color_row.addWidget(self._color_swatch)
        self._color_btn = QPushButton("Pick\u2026")
        self._color_btn.clicked.connect(self._pick_color)
        color_row.addWidget(self._color_btn)
        color_row.addStretch()
        detail_layout.addLayout(color_row)

        right.addWidget(detail_group)

        # Preview
        self._preview_label = QLabel()
        self._preview_label.setMinimumSize(320, 120)
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setStyleSheet("QLabel { border: 1px solid palette(mid); background: #222; }")
        self._preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right.addWidget(self._preview_label, stretch=1)

        root.addLayout(right, stretch=2)

        main_layout.addLayout(root)

        # === OK / Cancel ===
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)

        # Track the current overlay color (not yet saved)
        self._current_color: tuple[int, int, int] = (255, 255, 255)
        self._updating_fields = False

        # Populate
        self._refresh_template_list()
        if self._templates:
            self._tpl_list.setCurrentRow(0)

    # === public ===

    def result_templates(self) -> list[QSOTemplate]:
        """Return the edited template list (call after ``Accepted``)."""
        return self._templates

    # === template list ===

    def _refresh_template_list(self) -> None:
        self._tpl_list.clear()
        for tpl in self._templates:
            self._tpl_list.addItem(tpl.name or "(untitled)")

    @Slot(int)
    def _on_template_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._templates):
            self._name_edit.clear()
            self._overlay_list.clear()
            self._clear_overlay_detail()
            return
        tpl = self._templates[row]
        self._updating_fields = True
        self._name_edit.setText(tpl.name)
        self._updating_fields = False
        self._refresh_overlay_list(tpl)
        if tpl.overlays:
            self._overlay_list.setCurrentRow(0)
        else:
            self._clear_overlay_detail()
        self._update_preview()

    @Slot()
    def _add_template(self) -> None:
        tpl = QSOTemplate(name="New Template", overlays=[
            QSOTemplateOverlay(text="{mycall}", position="Bottom Center"),
        ])
        self._templates.append(tpl)
        self._refresh_template_list()
        self._tpl_list.setCurrentRow(len(self._templates) - 1)

    @Slot()
    def _remove_template(self) -> None:
        row = self._tpl_list.currentRow()
        if row < 0:
            return
        self._templates.pop(row)
        self._refresh_template_list()
        if self._templates:
            self._tpl_list.setCurrentRow(min(row, len(self._templates) - 1))

    @Slot(str)
    def _on_name_changed(self, text: str) -> None:
        if self._updating_fields:
            return
        row = self._tpl_list.currentRow()
        if row < 0 or row >= len(self._templates):
            return
        self._templates[row].name = text
        item = self._tpl_list.item(row)
        if item:
            item.setText(text or "(untitled)")

    # === overlay list ===

    def _refresh_overlay_list(self, tpl: QSOTemplate) -> None:
        self._overlay_list.clear()
        for ov in tpl.overlays:
            label = ov.text[:40] + ("\u2026" if len(ov.text) > 40 else "")
            self._overlay_list.addItem(label or "(empty)")

    @Slot(int)
    def _on_overlay_selected(self, row: int) -> None:
        tpl_row = self._tpl_list.currentRow()
        if tpl_row < 0 or tpl_row >= len(self._templates):
            return
        tpl = self._templates[tpl_row]
        if row < 0 or row >= len(tpl.overlays):
            self._clear_overlay_detail()
            return
        ov = tpl.overlays[row]
        self._updating_fields = True
        self._text_edit.setText(ov.text)
        idx = self._position_combo.findText(ov.position)
        if idx >= 0:
            self._position_combo.setCurrentIndex(idx)
        self._size_spin.setValue(ov.size)
        self._current_color = ov.color
        self._update_color_swatch()
        self._updating_fields = False
        self._update_preview()

    def _clear_overlay_detail(self) -> None:
        self._updating_fields = True
        self._text_edit.clear()
        self._position_combo.setCurrentIndex(0)
        self._size_spin.setValue(24)
        self._current_color = (255, 255, 255)
        self._update_color_swatch()
        self._updating_fields = False

    @Slot()
    def _add_overlay(self) -> None:
        tpl_row = self._tpl_list.currentRow()
        if tpl_row < 0:
            return
        tpl = self._templates[tpl_row]
        tpl.overlays.append(QSOTemplateOverlay(text="New text"))
        self._refresh_overlay_list(tpl)
        self._overlay_list.setCurrentRow(len(tpl.overlays) - 1)

    @Slot()
    def _remove_overlay(self) -> None:
        tpl_row = self._tpl_list.currentRow()
        ov_row = self._overlay_list.currentRow()
        if tpl_row < 0 or ov_row < 0:
            return
        tpl = self._templates[tpl_row]
        if ov_row >= len(tpl.overlays):
            return
        tpl.overlays.pop(ov_row)
        self._refresh_overlay_list(tpl)
        if tpl.overlays:
            self._overlay_list.setCurrentRow(min(ov_row, len(tpl.overlays) - 1))

    # === overlay detail sync ===

    @Slot()
    def _on_overlay_field_changed(self) -> None:
        if self._updating_fields:
            return
        tpl_row = self._tpl_list.currentRow()
        ov_row = self._overlay_list.currentRow()
        if tpl_row < 0 or ov_row < 0:
            return
        tpl = self._templates[tpl_row]
        if ov_row >= len(tpl.overlays):
            return
        ov = tpl.overlays[ov_row]
        ov.text = self._text_edit.text()
        ov.position = self._position_combo.currentText()
        ov.size = self._size_spin.value()
        ov.color = self._current_color
        # Update overlay list label
        item = self._overlay_list.item(ov_row)
        if item:
            label = ov.text[:40] + ("\u2026" if len(ov.text) > 40 else "")
            item.setText(label or "(empty)")
        self._update_preview()

    @Slot()
    def _pick_color(self) -> None:
        from PySide6.QtGui import QColor
        initial = QColor(*self._current_color)
        color = QColorDialog.getColor(initial, self, "Overlay Color")
        if color.isValid():
            self._current_color = (color.red(), color.green(), color.blue())
            self._update_color_swatch()
            self._on_overlay_field_changed()

    def _update_color_swatch(self) -> None:
        r, g, b = self._current_color
        self._color_swatch.setStyleSheet(
            f"background: rgb({r},{g},{b}); border: 1px solid gray;"
        )

    # === preview ===

    def _update_preview(self) -> None:
        tpl_row = self._tpl_list.currentRow()
        if tpl_row < 0 or tpl_row >= len(self._templates):
            self._preview_label.clear()
            return
        tpl = self._templates[tpl_row]

        # Render a small sample image
        w, h = 320, 240
        img = Image.new("RGB", (w, h), (34, 34, 34))
        draw = ImageDraw.Draw(img)
        for ov in tpl.overlays:
            resolved = resolve_placeholders(
                ov.text,
                mycall=self._mycall or "W0AEZ",
                theircall="N0CALL",
                rst="59",
            )
            draw_text_overlay(draw, (w, h), resolved, ov.position, ov.size, ov.color)

        # Convert to QPixmap
        from open_sstv.ui.image_gallery import _pil_to_pixmap
        pix = _pil_to_pixmap(img)
        scaled = pix.scaled(
            self._preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview_label.setPixmap(scaled)


__all__ = ["TemplateEditorDialog"]
