# SPDX-License-Identifier: GPL-3.0-or-later
"""Receive panel widget.

Composes a start/stop capture button, the in-progress / most-recent
decoded image preview, a status label that shows the detected mode
and VIS code, and an ``ImageGalleryWidget`` strip of recent decodes.
Owns no threads, no audio, and no DSP — it just emits
``capture_requested(bool)`` (True to start, False to stop) and exposes
setters/slots the ``MainWindow`` calls in response to ``RxWorker``
signals.

The live FFT waterfall from the v1 plan is intentionally deferred to
Phase 3 polish — it's a display-only nicety that doesn't gate the
"images appearing from a live radio" milestone.

Signals
-------
capture_requested(bool):
    User clicked Start/Stop. ``True`` means "open the input stream and
    begin decoding"; ``False`` means "close the stream and stop".
clear_requested():
    User clicked Clear. The MainWindow resets the ``RxWorker`` so the
    decoder starts hunting for a fresh VIS.
image_saved(PIL.Image.Image, Mode):
    User double-clicked a gallery thumbnail. Phase 3 will wire this
    to a ``QFileDialog`` save dialog; v1 just re-emits for the
    MainWindow to display a status bar confirmation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sstv_app.core.modes import Mode
from sstv_app.ui.image_gallery import ImageGalleryWidget, _pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class RxPanel(QWidget):
    """The receive half of the main window."""

    capture_requested = Signal(bool)
    clear_requested = Signal()
    image_saved = Signal(object, object)  # (PIL.Image, Mode)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._capturing: bool = False
        self._current_mode: Mode | None = None
        self._current_pil_image: "PILImage | None" = None
        # Full-resolution source pixmap for the most recent decode.
        # ``resizeEvent`` scales from here rather than from the label's
        # already-scaled pixmap so the preview stays crisp on upscale.
        self._preview_source: QPixmap | None = None

        layout = QVBoxLayout(self)

        # --- Start/Stop + Clear row ---
        button_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Capture")
        self._start_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._start_btn.clicked.connect(self._on_start_clicked)
        button_row.addWidget(self._start_btn)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._clear_btn.clicked.connect(self.clear_requested.emit)
        button_row.addWidget(self._clear_btn)

        self._save_btn = QPushButton("Save Image")
        self._save_btn.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save_clicked)
        button_row.addWidget(self._save_btn)
        layout.addLayout(button_row)

        # --- Current / most-recent decoded image ---
        self._preview = QLabel("No image decoded yet")
        self._preview.setMinimumSize(320, 240)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._preview.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); }"
        )
        layout.addWidget(self._preview, stretch=1)

        # --- Status line ---
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # --- Gallery strip ---
        self._gallery = ImageGalleryWidget(self)
        self._gallery.image_activated.connect(self.image_saved.emit)
        layout.addWidget(self._gallery)

    # === public API used by MainWindow ===

    def save_current_image(self) -> None:
        """Trigger a save of the most recent decoded image (Ctrl+S)."""
        if self._current_pil_image is not None and self._current_mode is not None:
            self.image_saved.emit(self._current_pil_image, self._current_mode)

    def set_capturing(self, capturing: bool) -> None:
        """Toggle the Start/Stop button label and internal state.

        Called by ``MainWindow`` in response to ``InputStreamWorker``'s
        ``started``/``stopped`` signals so the UI reflects what the
        audio thread is actually doing, not just what we asked it for.
        """
        self._capturing = capturing
        self._start_btn.setText("Stop Capture" if capturing else "Start Capture")
        if capturing:
            self._status.setText("Capturing… waiting for VIS header.")

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    @Slot(object, int)
    def show_image_started(self, mode: Mode, vis_code: int) -> None:
        """Announce a detected VIS header in the status line."""
        self._current_mode = mode
        self._status.setText(
            f"Decoding {mode.value} (VIS 0x{vis_code:02X})…"
        )

    @Slot(object, object, int, int, int)
    def show_image_progress(
        self,
        image: "PILImage",
        mode: Mode,
        vis_code: int,
        lines_decoded: int,
        lines_total: int,
    ) -> None:
        """Update the preview with a partial in-progress image.

        Called each time the decoder produces new scan lines. The image
        is full-size with black rows for lines not yet decoded — the
        preview scales it down so partial decodes look natural.
        """
        self._preview_source = _pil_to_pixmap(image)
        self._update_preview_pixmap()
        self._preview.setText("")
        pct = lines_decoded * 100 // lines_total if lines_total else 0
        self._status.setText(
            f"Decoding {mode.value}… {lines_decoded}/{lines_total} lines ({pct}%)"
        )
        self._current_mode = mode

    @Slot(object, object, int)
    def show_image_complete(
        self, image: "PILImage", mode: Mode, vis_code: int
    ) -> None:
        """Update the preview and add the image to the gallery.

        Called via queued connection from ``RxWorker.image_complete``,
        so the ``image`` argument is already a ``PIL.Image.Image``
        owned by this thread — safe to convert directly.
        """
        self._preview_source = _pil_to_pixmap(image)
        self._update_preview_pixmap()
        self._preview.setText("")
        self._gallery.add_image(image, mode)
        self._status.setText(
            f"Decoded {mode.value} ({image.width}×{image.height}, "
            f"VIS 0x{vis_code:02X})"
        )
        self._current_mode = mode
        self._current_pil_image = image
        self._save_btn.setEnabled(True)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt API
        """Rescale the preview when the panel resizes.

        Always scales from ``_preview_source`` (the original full-res
        pixmap) rather than from the label's already-scaled copy, so
        repeated resizes don't accumulate blur on upscale.
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

    # === private slots ===

    @Slot()
    def _on_start_clicked(self) -> None:
        # Toggle: if we're currently capturing, request stop; otherwise start.
        self.capture_requested.emit(not self._capturing)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._current_pil_image is not None and self._current_mode is not None:
            self.image_saved.emit(self._current_pil_image, self._current_mode)


__all__ = ["RxPanel"]
