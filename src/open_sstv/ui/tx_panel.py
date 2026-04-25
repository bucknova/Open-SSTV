# SPDX-License-Identifier: GPL-3.0-or-later
"""Transmit panel widget.

Pure presentation: image preview, v0.3 template gallery, QSO state,
mode picker, and Transmit/Stop buttons.  Owns no threads, no audio,
and no rig — it just emits signals that ``MainWindow`` forwards to
``TxWorker``.

Signals
-------
transmit_requested(PIL.Image.Image, Mode):
    User clicked Transmit.  If a v0.3 template is selected, the emitted
    image is the fully composited result (template + photo + QSO state).
    If no template is selected, the emitted image is the loaded photo
    unchanged — TxWorker's banner system applies in that case.
stop_requested():
    User clicked Stop.
template_composited(bool):
    Emitted when the selected-template state changes.  True = a v0.3
    template has been composited into the TX image, so TxWorker should
    skip its own banner stamp.  False = fall back to existing banner.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from open_sstv.config.templates import (
    QSOTemplate,
    load_templates,
    needs_user_input,
    resolve_placeholders,
    save_templates,
)
from open_sstv.core.encoder import DEFAULT_SAMPLE_RATE
from open_sstv.core.modes import MODE_TABLE, Mode

def _make_tx_context(mode: Mode, photo: "PILImage | None") -> TXContext:
    """Build a TXContext for the given mode and photo."""
    spec = MODE_TABLE[mode]
    return TXContext(
        mode_display_name=mode.value,
        frame_size=(spec.width, spec.display_height),
        photo_image=photo,
    )
from open_sstv.templates import manager as template_manager
from open_sstv.templates.model import QSOState, TXContext, Template
from open_sstv.templates.renderer import render_template
from open_sstv.ui.draw_text import draw_text_overlay
from open_sstv.ui.image_editor import ImageEditorDialog
from open_sstv.ui.qso_state_widget import QSOStateWidget
from open_sstv.ui.quick_fill_dialog import QuickFillDialog
from open_sstv.ui.template_editor import TemplateEditor
from open_sstv.ui.template_gallery import TemplateGallery
from open_sstv.ui.utils import pil_to_pixmap as _pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

    from open_sstv.config.schema import AppConfig


_IMAGE_FILE_FILTER = (
    "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp);;All files (*)"
)


class TxPanel(QWidget):
    """The transmit half of the main window."""

    transmit_requested = Signal(object, object)  # (PIL.Image.Image, Mode)
    stop_requested = Signal()
    #: True when the emitted TX image already has a v0.3 template composited
    #: in so TxWorker can skip its own banner stamp.
    template_composited = Signal(bool)

    def __init__(
        self,
        templates: list[QSOTemplate] | None = None,
        default_mode: str | None = None,
        app_config: "AppConfig | None" = None,
        templates_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._current_image: "PILImage | None" = None
        self._base_image: "PILImage | None" = None
        self._current_path: Path | None = None
        self._callsign: str = ""
        self._app_config: "AppConfig | None" = app_config
        self._templates_dir: Path | None = templates_dir
        self._selected_template: Template | None = None
        # v0.2 compat: kept so set_templates() callers don't break.
        self._v2_templates: list[QSOTemplate] = templates or load_templates()
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._preview_source: QPixmap | None = None

        layout = QVBoxLayout(self)

        # --- Image preview ---
        self._preview = QLabel("No image loaded")
        self._preview.setMinimumSize(320, 240)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._preview.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        layout.addWidget(self._preview, stretch=1)

        # TX target status label (aspect match / mismatch).
        self._tx_target_status = QLabel("")
        self._tx_target_status.setWordWrap(True)
        self._tx_target_status.setStyleSheet(
            "QLabel { padding: 4px 8px; border-radius: 3px; }"
        )
        layout.addWidget(self._tx_target_status)

        # --- v0.3 Template Gallery ---
        self._gallery = TemplateGallery(
            app_config=app_config,
            templates_dir=templates_dir,
            parent=self,
        )
        self._gallery.template_selected.connect(self._on_v3_template_selected)
        self._gallery.new_template_requested.connect(self._on_new_template_requested)
        self._gallery.edit_template_requested.connect(self._on_edit_template_requested)
        self._gallery.duplicate_template_requested.connect(
            self._on_duplicate_template_requested
        )
        self._gallery.rename_template_requested.connect(
            self._on_rename_template_requested
        )
        self._gallery.delete_template_requested.connect(
            self._on_delete_template_requested
        )
        layout.addWidget(self._gallery)

        # Open editors registered here so reload doesn't garbage-collect them
        # while the user is still typing.
        self._open_editors: list[TemplateEditor] = []

        # --- QSO State widget ---
        self._qso_widget = QSOStateWidget(self)
        self._qso_widget.state_changed.connect(self._on_qso_state_changed)
        layout.addWidget(self._qso_widget)

        # --- Mode picker ---
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for mode in Mode:
            spec = MODE_TABLE[mode]
            label = (
                f"{mode.value}  "
                f"({spec.width}\u00d7{spec.display_height}, "
                f"{spec.total_duration_s:.0f}s)"
            )
            self._mode_combo.addItem(label, mode)
        if default_mode:
            for i in range(self._mode_combo.count()):
                item = self._mode_combo.itemData(i)
                if item is not None:
                    item_value = item if isinstance(item, str) else item.value
                    if item_value == default_mode:
                        self._mode_combo.setCurrentIndex(i)
                        break
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, stretch=1)
        layout.addLayout(mode_row)

        # --- Buttons ---
        load_row = QHBoxLayout()
        self._load_btn = QPushButton("Load Image\u2026")
        self._load_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._load_btn.clicked.connect(self._on_load_clicked)
        load_row.addWidget(self._load_btn)

        self._edit_btn = QPushButton("Edit Image\u2026")
        self._edit_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        load_row.addWidget(self._edit_btn)
        layout.addLayout(load_row)

        button_row = QHBoxLayout()
        self._transmit_btn = QPushButton("Transmit")
        self._transmit_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._transmit_btn.setEnabled(False)
        self._transmit_btn.clicked.connect(self._on_transmit_clicked)
        button_row.addWidget(self._transmit_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.stop_requested.emit)
        button_row.addWidget(self._stop_btn)
        layout.addLayout(button_row)

        # --- TX progress bar ---
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p% — %vs elapsed")
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        # --- Status line ---
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # Load bundled test image so the panel is transmit-ready on startup.
        _default = Path(__file__).parent.parent / "assets" / "testimage.jpg"
        if _default.is_file():
            self.load_image(_default)

        # Load templates into gallery (may be empty on fresh install before
        # migration runs; gallery silently shows "No templates installed").
        self._gallery.reload_templates()

    # === Public API ===

    @property
    def current_image(self) -> "PILImage | None":
        return self._current_image

    def set_app_config(self, cfg: "AppConfig") -> None:
        """Push updated config to gallery for token resolution."""
        self._app_config = cfg
        self._gallery.set_app_config(cfg)
        # Also update the callsign shortcut used elsewhere.
        self._callsign = cfg.callsign

    def reload_templates(self) -> None:
        """Ask the gallery to reload from disk (called after migration)."""
        self._gallery.reload_templates()

    def get_qso_state(self) -> QSOState:
        """Return the current QSO state from the QSO widget."""
        return self._qso_widget.get_state()

    def load_image(self, path: Path) -> None:
        """Load an image from disk into the preview."""
        try:
            img = Image.open(path)
            img.load()
        except (OSError, ValueError) as exc:
            self._status.setText(f"Failed to load: {exc}")
            return

        self._current_image = img
        self._base_image = img.copy()
        self._current_path = path

        pix = QPixmap(str(path))
        if not pix.isNull():
            self._preview_source = pix
            self._update_preview_pixmap()
            self._preview.setText("")
        self._transmit_btn.setEnabled(True)
        self._edit_btn.setEnabled(True)
        self._status.setText(f"Loaded: {path.name}  ({img.width}×{img.height})")

        # Tell the gallery to re-render thumbs with the new photo.
        self._gallery.set_photo(img)
        # Refresh the live preview composite.
        self._refresh_composite_preview()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_preview_pixmap()

    def set_transmitting(self, transmitting: bool) -> None:
        has_image = self._current_image is not None
        self._transmit_btn.setEnabled(not transmitting and has_image)
        self._stop_btn.setEnabled(transmitting)
        self._load_btn.setEnabled(not transmitting)
        self._edit_btn.setEnabled(not transmitting and has_image)
        self._mode_combo.setEnabled(not transmitting)
        if transmitting:
            self._progress_bar.setValue(0)
            self._progress_bar.setVisible(True)
        else:
            self._progress_bar.setVisible(False)

    @Slot(int, int)
    def show_tx_progress(self, samples_played: int, samples_total: int) -> None:
        if samples_total > 0:
            pct = int(samples_played * 100 / samples_total)
            elapsed_s = int(samples_played / self._sample_rate)
            total_s = int(samples_total / self._sample_rate)
            self._progress_bar.setValue(pct)
            self._progress_bar.setFormat(f"{pct}% — {elapsed_s}s / {total_s}s")

    def set_sample_rate(self, sample_rate: int) -> None:
        if sample_rate > 0:
            self._sample_rate = sample_rate

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_callsign(self, callsign: str) -> None:
        self._callsign = callsign

    def set_default_mode(self, mode_value: str) -> None:
        for i in range(self._mode_combo.count()):
            item = self._mode_combo.itemData(i)
            if item is not None:
                item_val = item if isinstance(item, str) else item.value
                if item_val == mode_value:
                    self._mode_combo.setCurrentIndex(i)
                    break

    def selected_mode(self) -> Mode:
        data = self._mode_combo.currentData()
        return data if isinstance(data, Mode) else Mode(data)

    def set_templates(self, templates: list[QSOTemplate]) -> None:
        """v0.2 compat shim — no-op in v0.3."""
        self._v2_templates = templates

    # === Private slots ===

    @Slot(int)
    def _on_mode_changed(self, _index: int) -> None:
        self._gallery.set_mode(self.selected_mode())
        self._update_preview_pixmap()

    @Slot(object)
    def _on_v3_template_selected(self, template: "Template | None") -> None:
        self._selected_template = template
        self.template_composited.emit(template is not None)
        self._refresh_composite_preview()

    @Slot(object)
    def _on_qso_state_changed(self, qso_state: QSOState) -> None:
        self._gallery.set_qso_state(qso_state)
        self._refresh_composite_preview()

    # --- Gallery CRUD signals ---

    @Slot()
    def _on_new_template_requested(self) -> None:
        new_tpl = Template(name="New Template", role="custom", layers=[])
        self._open_editor(new_tpl, path=None)

    @Slot(object, object)
    def _on_edit_template_requested(self, template: Template, path: Path) -> None:
        self._open_editor(template, path=path)

    @Slot(object)
    def _on_duplicate_template_requested(self, path: Path) -> None:
        try:
            new_path = template_manager.duplicate_template(path)
        except OSError as exc:
            QMessageBox.critical(
                self, "Duplicate failed", f"Could not duplicate template:\n{exc}"
            )
            return
        self._gallery.reload_templates()
        self._status.setText(f"Duplicated: {new_path.name}")

    @Slot(object, object)
    def _on_rename_template_requested(self, template: Template, path: Path) -> None:
        new_name, ok = QInputDialog.getText(
            self,
            "Rename template",
            "New name:",
            text=template.name,
        )
        if not ok or not new_name.strip() or new_name == template.name:
            return
        # Mutate the loaded template, save to a new filename slug, and remove
        # the old file once the write succeeds.  Atomic-ish — if save fails
        # we leave the original untouched.
        old_path = path
        template.name = new_name
        try:
            new_path = template_manager.save(template, old_path.parent)
        except OSError as exc:
            QMessageBox.critical(
                self, "Rename failed", f"Could not save renamed template:\n{exc}"
            )
            return
        if new_path != old_path:
            try:
                template_manager.delete(old_path)
            except OSError as exc:
                # Non-fatal: the new file is good; just inform the user.
                self._status.setText(
                    f"Renamed but couldn't remove old file {old_path.name}: {exc}"
                )
        self._gallery.reload_templates()

    @Slot(object, object)
    def _on_delete_template_requested(self, template: Template, path: Path) -> None:
        confirm = QMessageBox.question(
            self,
            "Delete template",
            f"Delete template '{template.name}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            template_manager.delete(path)
        except OSError as exc:
            QMessageBox.critical(
                self, "Delete failed", f"Could not delete template:\n{exc}"
            )
            return
        # If the deleted template is currently selected, drop the selection
        # so the main TX preview falls back to the bare photo.
        if (
            self._selected_template is not None
            and self._selected_template.name == template.name
        ):
            self._selected_template = None
            self.template_composited.emit(False)
            self._refresh_composite_preview()
        self._gallery.reload_templates()
        self._status.setText(f"Deleted: {path.name}")

    def _open_editor(self, template: Template, path: Path | None) -> None:
        """Spawn a non-modal editor and wire it to refresh the gallery on save."""
        if self._app_config is None:
            QMessageBox.warning(
                self,
                "Cannot edit yet",
                "Application config isn't ready — try again in a moment.",
            )
            return
        editor = TemplateEditor(
            template,
            path=path,
            app_config=self._app_config,
            templates_dir=self._templates_dir,
            current_photo=self._base_image,
            current_mode=self.selected_mode(),
            parent=self,
        )
        editor.template_saved.connect(self._on_template_saved_in_editor)
        editor.finished.connect(self._on_editor_finished)
        self._open_editors.append(editor)
        editor.show()
        editor.raise_()

    @Slot(object)
    def _on_template_saved_in_editor(self, _path: Path) -> None:
        self._gallery.reload_templates()

    @Slot(int)
    def _on_editor_finished(self, _result: int) -> None:
        sender = self.sender()
        if isinstance(sender, TemplateEditor) and sender in self._open_editors:
            self._open_editors.remove(sender)
            sender.deleteLater()

    @Slot()
    def _on_load_clicked(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Load Image", "", _IMAGE_FILE_FILTER
        )
        if path_str:
            self.load_image(Path(path_str))

    @Slot()
    def _on_edit_clicked(self) -> None:
        if self._current_image is None:
            return
        dlg = ImageEditorDialog(
            self._current_image,
            self.selected_mode(),
            callsign=self._callsign,
            parent=self,
        )
        if dlg.exec() == ImageEditorDialog.DialogCode.Accepted:
            result = dlg.result_image()
            if result is not None:
                self._current_image = result
                base = dlg.result_base_image()
                self._base_image = base if base is not None else result.copy()
                self._preview_source = _pil_to_pixmap(result)
                self._update_preview_pixmap()
                self._preview.setText("")
                self._status.setText(f"Edited: {result.width}x{result.height}")
                self._gallery.set_photo(self._base_image)
                self._refresh_composite_preview()

    @Slot()
    def _on_transmit_clicked(self) -> None:
        if self._current_image is None:
            return
        mode = self.selected_mode()
        if self._selected_template is not None and self._base_image is not None:
            # v0.3: render template composite and emit that.
            composed = self._compose_template()
            if composed is not None:
                self.transmit_requested.emit(composed, mode)
                return
        # Fallback: emit the current image (TxWorker banner will apply if enabled).
        self.transmit_requested.emit(self._current_image, mode)

    # === Preview helpers ===

    def _compose_template(self) -> "PILImage | None":
        """Render the selected template over the base photo. Returns None on error."""
        if self._selected_template is None or self._base_image is None:
            return None
        if self._app_config is None:
            return None
        mode = self.selected_mode()
        qso = self._qso_widget.get_state()
        ctx = _make_tx_context(mode, self._base_image)
        try:
            return render_template(self._selected_template, qso, self._app_config, ctx)
        except Exception as exc:  # noqa: BLE001
            self._status.setText(f"Template render failed: {exc}")
            return None

    def _refresh_composite_preview(self) -> None:
        """Update the main preview with the current template composite."""
        if self._selected_template is None or self._base_image is None:
            # No template selected — show the raw loaded image.
            if self._preview_source is None and self._base_image is not None:
                self._preview_source = _pil_to_pixmap(self._base_image)
            self._update_preview_pixmap()
            return
        composed = self._compose_template()
        if composed is not None:
            self._preview_source = _pil_to_pixmap(composed)
        self._update_preview_pixmap()

    def _update_preview_pixmap(self) -> None:
        if self._preview_source is None or self._preview_source.isNull():
            return
        scaled = self._preview_source.scaled(
            self._preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        annotated = self._paint_target_outline(scaled)
        self._preview.setPixmap(annotated)
        self._update_tx_target_status()

    def _paint_target_outline(self, pixmap: QPixmap) -> QPixmap:
        if self._current_image is None:
            return pixmap
        try:
            mode = self.selected_mode()
            spec = MODE_TABLE[mode]
        except (ValueError, KeyError):
            return pixmap

        iw, ih = self._current_image.width, self._current_image.height
        tw, th = spec.width, spec.display_height
        if iw <= 0 or ih <= 0 or tw <= 0 or th <= 0:
            return pixmap

        src_aspect = iw / ih
        tgt_aspect = tw / th
        aspect_match = abs(src_aspect - tgt_aspect) / tgt_aspect < 0.01

        pw = pixmap.width()
        ph = pixmap.height()
        if tgt_aspect > src_aspect:
            box_w = pw
            box_h = int(round(pw / tgt_aspect))
        else:
            box_w = int(round(ph * tgt_aspect))
            box_h = ph
        box_x = (pw - box_w) // 2
        box_y = (ph - box_h) // 2

        out = QPixmap(pixmap)
        painter = QPainter(out)
        try:
            pen_color = (
                QColor(180, 220, 180, 180)
                if aspect_match
                else QColor(255, 170, 60, 220)
            )
            pen = QPen(pen_color)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(box_x, box_y, box_w - 1, box_h - 1)
        finally:
            painter.end()
        return out

    def _update_tx_target_status(self) -> None:
        if self._current_image is None:
            self._tx_target_status.setText("")
            self._tx_target_status.setStyleSheet(
                "QLabel { padding: 4px 8px; border-radius: 3px; }"
            )
            return
        try:
            mode = self.selected_mode()
            spec = MODE_TABLE[mode]
        except (ValueError, KeyError):
            self._tx_target_status.setText("")
            return
        iw, ih = self._current_image.width, self._current_image.height
        tw, th = spec.width, spec.display_height
        src_aspect = iw / ih if ih else 0.0
        tgt_aspect = tw / th if th else 0.0
        if tgt_aspect == 0:
            return
        aspect_match = abs(src_aspect - tgt_aspect) / tgt_aspect < 0.01

        if iw == tw and ih == th:
            text = (
                f"Image {iw}×{ih} matches {mode.value} target — "
                "TX will encode at native resolution."
            )
            bg, fg = "#e6f4ea", "#1b5e20"
        elif aspect_match:
            text = (
                f"Image {iw}×{ih} · {mode.value} target {tw}×{th} — "
                "aspect matches; LANCZOS resize on TX, no distortion."
            )
            bg, fg = "#e6f4ea", "#1b5e20"
        else:
            text = (
                f"Image {iw}×{ih} · {mode.value} target {tw}×{th} — "
                "aspect mismatch; image will be stretched.  Consider "
                "re-editing for the new mode."
            )
            bg, fg = "#fff4e5", "#b26a00"
        self._tx_target_status.setText(text)
        self._tx_target_status.setStyleSheet(
            f"QLabel {{ padding: 4px 8px; border-radius: 3px; "
            f"background: {bg}; color: {fg}; }}"
        )


    # -----------------------------------------------------------------------
    # v0.2 compat private methods
    # These are no longer wired to any UI element (QSOTemplateBar was
    # replaced by the v0.3 gallery) but remain as private helpers so that
    # existing tests and external callers that poke the internal API don't
    # immediately break.  Will be removed in v0.4.
    # -----------------------------------------------------------------------

    @Slot(object)
    def _on_template_activated(self, tpl: "QSOTemplate") -> None:
        """Apply a v0.2 QSOTemplate overlay onto the base image."""
        if self._current_image is None:
            self._status.setText("Load an image first before applying a template.")
            return

        if needs_user_input(tpl):
            dlg = QuickFillDialog(tpl, mycall=self._callsign, parent=self)
            if dlg.exec() != QuickFillDialog.DialogCode.Accepted:
                return
            overlays = dlg.resolved_overlays()
        else:
            overlays = []
            for ov in tpl.overlays:
                overlays.append({
                    "text": resolve_placeholders(ov.text, mycall=self._callsign),
                    "position": ov.position,
                    "size": ov.size,
                    "color": ov.color,
                    "x": ov.x,
                    "y": ov.y,
                })
        self._apply_overlays(overlays)

    def _apply_overlays(self, overlays: list[dict]) -> None:
        if self._base_image is None:
            return
        img = self._base_image.copy()
        draw = ImageDraw.Draw(img)
        for ov in overlays:
            draw_text_overlay(
                draw,
                img.size,
                text=ov["text"],
                position=ov["position"],
                size=ov["size"],
                color=ov["color"],
                x=ov.get("x"),
                y=ov.get("y"),
            )
        self._current_image = img
        self._preview_source = _pil_to_pixmap(img)
        self._update_preview_pixmap()
        self._preview.setText("")
        self._status.setText("Template applied.")

    @Slot()
    def _on_clear_text(self) -> None:
        if self._base_image is None:
            return
        self._current_image = self._base_image.copy()
        self._preview_source = _pil_to_pixmap(self._current_image)
        self._update_preview_pixmap()
        self._preview.setText("")
        self._status.setText("Template text cleared.")


__all__ = ["TxPanel"]
