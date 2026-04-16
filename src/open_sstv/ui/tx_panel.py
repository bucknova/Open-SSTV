# SPDX-License-Identifier: GPL-3.0-or-later
"""Transmit panel widget.

Pure presentation: an image preview, a "Load Image..." button, a mode
picker, Transmit/Stop buttons, and a status label. Owns no threads, no
audio, and no rig — it just emits two signals (``transmit_requested`` and
``stop_requested``) and exposes a few UI-state setters that ``MainWindow``
calls in response to ``TxWorker`` signals.

Drag-and-drop loading is on the Phase 3 polish list; for Phase 1 the
Load button (which opens a ``QFileDialog``) is enough to demonstrate
end-to-end TX.

Signals
-------
transmit_requested(PIL.Image.Image, Mode):
    User clicked Transmit with a loaded image. The MainWindow forwards
    this directly to ``TxWorker.transmit`` (Qt's auto-connect handles
    the cross-thread queuing).
stop_requested():
    User clicked Stop. MainWindow calls ``TxWorker.request_stop``
    (which is a plain method, not a slot, because the worker thread is
    blocked in ``play_blocking``).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
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
from open_sstv.ui.draw_text import draw_text_overlay
from open_sstv.ui.image_editor import ImageEditorDialog
from open_sstv.ui.qso_template_bar import QSOTemplateBar
from open_sstv.ui.quick_fill_dialog import QuickFillDialog
from open_sstv.ui.template_editor_dialog import TemplateEditorDialog
from open_sstv.ui.utils import pil_to_pixmap as _pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


_IMAGE_FILE_FILTER = (
    "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp);;All files (*)"
)


class TxPanel(QWidget):
    """The transmit half of the main window."""

    transmit_requested = Signal(object, object)  # (PIL.Image.Image, Mode)
    stop_requested = Signal()

    def __init__(
        self,
        templates: list[QSOTemplate] | None = None,
        default_mode: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._current_image: "PILImage | None" = None
        # The "clean" image before any template text was burned on.
        # Clicking a template always draws on this, so re-applying
        # auto-clears the previous template's text.
        self._base_image: "PILImage | None" = None
        self._current_path: Path | None = None
        self._callsign: str = ""
        self._templates: list[QSOTemplate] = templates or load_templates()
        # Sample rate used for converting samples_played → seconds in the
        # progress label.  Defaulted to the encoder default; ``MainWindow``
        # calls ``set_sample_rate()`` whenever the user changes the rate
        # in Settings (OP-06).  Without this the progress label was hard-
        # coded to 48 kHz and showed the wrong elapsed seconds at 44.1 kHz.
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        # Full-resolution source pixmap kept so ``resizeEvent`` can
        # rescale from the original instead of from the already-scaled
        # label pixmap (which would progressively blur on upscale).
        self._preview_source: QPixmap | None = None

        layout = QVBoxLayout(self)

        # --- Image preview ---
        self._preview = QLabel("No image loaded")
        self._preview.setMinimumSize(320, 240)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # A subtle border so the empty preview area is visible.
        self._preview.setStyleSheet("QLabel { border: 1px solid palette(mid); }")
        layout.addWidget(self._preview, stretch=1)

        # --- QSO template bar ---
        self._template_bar = QSOTemplateBar(self._templates, self)
        self._template_bar.template_activated.connect(self._on_template_activated)
        self._template_bar.clear_text_requested.connect(self._on_clear_text)
        self._template_bar.edit_templates_requested.connect(self._on_edit_templates)
        layout.addWidget(self._template_bar)

        # --- Mode picker ---
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for mode in Mode:
            spec = MODE_TABLE[mode]
            label = f"{mode.value}  ({spec.width}\u00d7{spec.display_height}, {spec.total_duration_s:.0f}s)"
            self._mode_combo.addItem(label, mode)
        if default_mode:
            # Find the combo entry whose Mode.value matches the config string.
            # Qt may unwrap the stored StrEnum back to a plain str via QVariant,
            # so guard against both a Mode object and a raw string.
            for i in range(self._mode_combo.count()):
                item = self._mode_combo.itemData(i)
                if item is not None:
                    item_value = item if isinstance(item, str) else item.value
                    if item_value == default_mode:
                        self._mode_combo.setCurrentIndex(i)
                        break
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

    # === public API used by MainWindow ===

    def load_image(self, path: Path) -> None:
        """Load an image from disk into the preview.

        Public so the main window can also wire it to a ``--image`` CLI
        flag later. Failures are reported via the status label rather
        than raised — TX panel is "GUI in, GUI out".
        """
        try:
            img = Image.open(path)
            img.load()  # force decode now so a corrupt file fails here, not on TX
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

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt API
        """Rescale the preview when the panel resizes.

        Always scales from ``_preview_source`` (the original file
        pixmap) so repeated resizes don't accumulate blur from
        re-scaling an already-scaled copy.
        """
        super().resizeEvent(event)
        self._update_preview_pixmap()

    def _update_preview_pixmap(self) -> None:
        if self._preview_source is None or self._preview_source.isNull():
            return
        self._preview.setPixmap(
            self._preview_source.scaled(
                self._preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_transmitting(self, transmitting: bool) -> None:
        """Toggle button state for the in-flight TX cycle."""
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
        """Update the progress bar during transmission.

        Uses the panel's configured sample rate (set via ``set_sample_rate``)
        rather than the hardcoded 48 kHz that the original implementation
        assumed.  At 44.1 kHz a 114 s Martin M1 transmission used to display
        "124 s / 124 s" at completion (OP-06).
        """
        if samples_total > 0:
            pct = int(samples_played * 100 / samples_total)
            elapsed_s = int(samples_played / self._sample_rate)
            total_s = int(samples_total / self._sample_rate)
            self._progress_bar.setValue(pct)
            self._progress_bar.setFormat(
                f"{pct}% — {elapsed_s}s / {total_s}s"
            )

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update the rate used to convert samples → seconds in the
        progress label.  Called from ``MainWindow._apply_config`` so the
        label tracks Settings changes (OP-06)."""
        if sample_rate > 0:
            self._sample_rate = sample_rate

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_callsign(self, callsign: str) -> None:
        """Update the callsign pre-populated in the image editor."""
        self._callsign = callsign

    def set_default_mode(self, mode_value: str) -> None:
        """Select the combo entry matching ``mode_value`` (a Mode.value string).

        Called from MainWindow when settings are saved so the mode picker
        reflects any change the user made in the Settings dialog.
        Does nothing if the value doesn't match any known mode.
        """
        for i in range(self._mode_combo.count()):
            item = self._mode_combo.itemData(i)
            if item is not None:
                item_val = item if isinstance(item, str) else item.value
                if item_val == mode_value:
                    self._mode_combo.setCurrentIndex(i)
                    break

    def selected_mode(self) -> Mode:
        # Qt's QVariant unwraps a StrEnum back to a plain ``str`` when it
        # comes out of ``currentData()``, so we have to re-wrap.
        data = self._mode_combo.currentData()
        return data if isinstance(data, Mode) else Mode(data)

    # === private slots ===

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
                # Use the text-free base so "Clear Text" removes ALL
                # overlays (template-applied AND manually-added in the
                # editor).  Falls back to a copy of the full result if
                # the editor didn't provide a separate base (shouldn't
                # happen, but defensive).
                base = dlg.result_base_image()
                self._base_image = base if base is not None else result.copy()
                self._preview_source = _pil_to_pixmap(result)
                self._update_preview_pixmap()
                self._preview.setText("")
                self._status.setText(
                    f"Edited: {result.width}x{result.height}"
                )

    @Slot()
    def _on_transmit_clicked(self) -> None:
        if self._current_image is None:
            return
        self.transmit_requested.emit(self._current_image, self.selected_mode())

    # === template slots ===

    @Slot(object)
    def _on_template_activated(self, tpl: QSOTemplate) -> None:
        """Handle a template button click from the bar."""
        if self._current_image is None:
            self._status.setText("Load an image first before applying a template.")
            return

        needed = needs_user_input(tpl)
        if needed:
            dlg = QuickFillDialog(tpl, mycall=self._callsign, parent=self)
            if dlg.exec() != QuickFillDialog.DialogCode.Accepted:
                return
            overlays = dlg.resolved_overlays()
        else:
            # No user input needed — resolve automatically.  x/y MUST
            # be forwarded so Custom-position overlays render at the
            # user's saved coordinates.  Previously omitted, which
            # caused Custom templates to fall through the ``x is None``
            # branch in ``draw_text_overlay`` and render at top-left
            # via ``position_to_xy("Custom", ...)`` returning the
            # default (margin, margin).  Fixed in v0.1.36.
            overlays = []
            for ov in tpl.overlays:
                overlays.append({
                    "text": resolve_placeholders(
                        ov.text, mycall=self._callsign,
                    ),
                    "position": ov.position,
                    "size": ov.size,
                    "color": ov.color,
                    "x": ov.x,
                    "y": ov.y,
                })

        self._apply_overlays(overlays)

    def _apply_overlays(self, overlays: list[dict]) -> None:
        """Draw resolved text overlays onto the base (clean) TX image.

        Always starts from ``_base_image`` so re-applying a template
        auto-clears the previous one's text.
        """
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
        """Restore the base (clean) image, removing all template text."""
        if self._base_image is None:
            return
        self._current_image = self._base_image.copy()
        self._preview_source = _pil_to_pixmap(self._current_image)
        self._update_preview_pixmap()
        self._preview.setText("")
        self._status.setText("Template text cleared.")

    @Slot()
    def _on_edit_templates(self) -> None:
        """Open the template editor dialog."""
        dlg = TemplateEditorDialog(
            self._templates, mycall=self._callsign, parent=self
        )
        if dlg.exec() == TemplateEditorDialog.DialogCode.Accepted:
            self._templates = dlg.result_templates()
            try:
                save_templates(self._templates)
            except OSError as exc:
                QMessageBox.warning(self, "Could not save templates", str(exc))
            self._template_bar.set_templates(self._templates)

    def set_templates(self, templates: list[QSOTemplate]) -> None:
        """Replace the current template list and refresh the bar."""
        self._templates = templates
        self._template_bar.set_templates(templates)


__all__ = ["TxPanel"]
