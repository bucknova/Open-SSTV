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
    User double-clicked a gallery thumbnail (or invoked Save As from
    the context menu). Wired by ``MainWindow`` to a ``QFileDialog``.
    Single-click on a thumbnail instead loads it into the main preview
    via ``_show_gallery_image`` — no save dialog, no disk round-trip.
rx_image_selected(PIL.Image.Image):
    User single-clicked a gallery thumbnail (or invoked *View* from the
    context menu). Wired by ``MainWindow`` to ``TxPanel.set_rx_image``
    so reply/exchange templates can render that image in the
    ``{rx_image}`` slot.
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

from open_sstv.core.modes import Mode
from open_sstv.ui.image_gallery import ImageGalleryWidget, _pil_to_pixmap

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class RxPanel(QWidget):
    """The receive half of the main window."""

    capture_requested = Signal(bool)
    clear_requested = Signal()
    image_saved = Signal(object, object)  # (PIL.Image, Mode)
    rx_image_selected = Signal(object)  # PIL.Image — for template {rx_image} slot

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
        # v0.2.7: single-click (and context-menu *View*) on a gallery
        # thumbnail loads that image into the main preview above so the
        # user can review older decodes at full size without first
        # saving them to disk.
        self._gallery.image_preview_requested.connect(self._show_gallery_image)
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
        OP2-03: re-enables the button here (disabled in ``_on_start_clicked``
        to prevent a double-click race that would request two audio streams).
        """
        self._capturing = capturing
        self._start_btn.setEnabled(True)
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

    @Slot(object, object)
    def _show_gallery_image(self, image: "PILImage", mode: Mode) -> None:
        """Load a gallery thumbnail into the main preview (v0.2.7).

        Wired to ``ImageGalleryWidget.image_preview_requested`` so a
        single-click (or context-menu *View*) on an older decode swaps
        the big preview pixmap and rebinds the "current image" fields.
        Rebinding ``_current_pil_image`` / ``_current_mode`` means
        ``Save Image`` and ``Ctrl+S`` act on the image the user is
        actually looking at — if they just clicked a thumb to inspect
        it, Save saves that one, not whatever was most-recently decoded.

        ``mode`` may arrive as a plain ``str`` because Qt unwraps
        ``StrEnum`` when storing/retrieving via ``QStandardItem.data()``
        in the gallery.  Same coercion pattern as
        ``MainWindow._on_rx_image_saved``.

        Note: a live in-progress decode will clobber the preview on the
        next ``show_image_progress`` tick. That's intentional — the
        latest data should always win when capture is active — and the
        user will typically pause capture before browsing history.
        """
        # Re-hydrate StrEnum if Qt flattened it to a bare string.
        mode_enum = mode if isinstance(mode, Mode) else Mode(str(mode))
        self._preview_source = _pil_to_pixmap(image)
        self._update_preview_pixmap()
        self._preview.setText("")
        self._current_mode = mode_enum
        self._current_pil_image = image
        self._save_btn.setEnabled(True)
        self._status.setText(
            f"Viewing {mode_enum.value} ({image.width}×{image.height})"
        )
        # Selecting a thumbnail also pins it as the active "RX image" for
        # reply/exchange templates so {rx_image} resolves to what the user
        # is looking at, not the most recent decode.
        self.rx_image_selected.emit(image)

    # === private slots ===

    @Slot()
    def _on_start_clicked(self) -> None:
        # Toggle: if we're currently capturing, request stop; otherwise start.
        # OP2-03: disable immediately so a double-click between this emission
        # and the audio_worker.started→set_capturing(True) callback can't
        # queue a second start request.
        self._start_btn.setEnabled(False)
        self.capture_requested.emit(not self._capturing)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._current_pil_image is not None and self._current_mode is not None:
            self.image_saved.emit(self._current_pil_image, self._current_mode)


__all__ = ["RxPanel"]
