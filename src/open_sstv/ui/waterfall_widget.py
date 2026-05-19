# SPDX-License-Identifier: GPL-3.0-or-later
"""Floating waterfall spectrogram window.

Displays the SSTV audio band (0–4000 Hz) as a scrolling FFT spectrogram.
RX audio uses the classic amateur-radio colour palette (black → blue →
cyan → green → yellow → white).  TX audio uses a warm palette (black →
dark-red → orange → yellow → white) so transmitted and received signals
are visually distinct in the same display.

The window is a floating ``QMainWindow`` shown/hidden via View → Waterfall.
It is created lazily on first show and hidden (not closed) on uncheck so the
scroll history is preserved across hide/show cycles.

FFT parameters (follow the design in docs/waterfall_scope.md)
--------------------------------------------------------------
Window size : 1024 samples at 48 kHz → ~21 ms, ~47 Hz / bin
Window fn   : Hann — good sidelobe rejection for SSTV tones
Overlap     : 512-sample tail carried into the next chunk via _overlap
              buffer.  One FFT column is produced per chunk call, so
              the effective stride matches the audio chunk size (~4800
              samples at 48 kHz / 100 ms) — not a fixed 512-sample
              stride.  The tail prevents discontinuities at chunk
              boundaries; it is not true 50%-overlap.
Display bins: 0–85  →  0–4031 Hz at 48 kHz
Display size: 200 px height (frequency), 400 px width (time axis)

Repaint is driven by a 50 ms QTimer independent of the audio chunk rate
so a slow GUI repaint never stalls the DSP threads.

The FFT is computed here in the widget (on the GUI thread) rather than in
RxWorker.  A 1024-point FFT takes < 0.1 ms — negligible on the GUI thread
and keeps the worker code clean.  Workers simply emit their raw audio chunks;
the widget decides how to render them.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMainWindow, QWidget

# ---------------------------------------------------------------------------
# FFT constants
# ---------------------------------------------------------------------------

_FFT_SIZE: int = 1024
_HANN: np.ndarray = np.hanning(_FFT_SIZE).astype(np.float64)

# Number of FFT output bins to display.  At 48 kHz, bin width ≈ 46.875 Hz,
# so bin 85 ≈ 3984 Hz.  The widget does not hard-code 48 kHz — it
# recomputes bin_hz from ``_sample_rate`` when drawing markers.
_DISPLAY_BINS: int = 86

# dBFS colour-mapping range.
_DB_MIN: float = -90.0
_DB_MAX: float = 0.0

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

_DISPLAY_WIDTH: int = 400
_DISPLAY_HEIGHT: int = 200

# SSTV frequency markers drawn as dotted horizontal lines with labels.
# (freq_hz, css-hex-colour, label)
_MARKERS: list[tuple[int, str, str]] = [
    (1200, "#ff5555", "sync 1200"),
    (1500, "#7799ff", "black 1500"),
    (1900, "#ffee55", "leader 1900"),
    (2300, "#ffffff", "white 2300"),
]


# ---------------------------------------------------------------------------
# Colour look-up tables
# ---------------------------------------------------------------------------

def _build_lut(warm: bool) -> np.ndarray:
    """Return a (256, 3) uint8 RGB colour-map array.

    ``warm=False`` — classic amateur-radio / RX palette:
        black → blue → cyan → green → yellow → white
    ``warm=True``  — warm TX palette:
        black → dark-red → red → orange → yellow → white
    """
    lut = np.zeros((256, 3), dtype=np.uint8)
    for i in range(256):
        t = i / 255.0
        if not warm:
            if t < 0.20:
                s = t / 0.20
                r, g, b = 0, 0, int(200 * s)
            elif t < 0.40:
                s = (t - 0.20) / 0.20
                r, g, b = 0, int(180 * s), 200
            elif t < 0.60:
                s = (t - 0.40) / 0.20
                r, g, b = 0, 180, int(200 * (1.0 - s))
            elif t < 0.80:
                s = (t - 0.60) / 0.20
                r, g, b = int(200 * s), 180, 0
            else:
                s = (t - 0.80) / 0.20
                r, g, b = 200, int(180 + 75 * s), int(220 * s)
        else:
            if t < 0.20:
                s = t / 0.20
                r, g, b = int(160 * s), 0, 0
            elif t < 0.40:
                s = (t - 0.20) / 0.20
                r, g, b = 160 + int(95 * s), 0, 0
            elif t < 0.60:
                s = (t - 0.40) / 0.20
                r, g, b = 255, int(140 * s), 0
            elif t < 0.80:
                s = (t - 0.60) / 0.20
                r, g, b = 255, 140 + int(115 * s), 0
            else:
                s = (t - 0.80) / 0.20
                r, g, b = 255, 255, int(255 * s)
        lut[i] = (r, g, b)
    return lut


_LUT_RX: np.ndarray = _build_lut(warm=False)
_LUT_TX: np.ndarray = _build_lut(warm=True)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class WaterfallWidget(QWidget):
    """Scrolling FFT spectrogram for SSTV audio.

    Public slots
    ------------
    add_rx_column(chunk)
        Accepts a 1-D float32/float64 audio chunk from RxWorker.
    add_tx_column(chunk)
        Accepts a 1-D int16 or float audio chunk from TxWorker.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(_DISPLAY_WIDTH, _DISPLAY_HEIGHT)

        # Backing store: (height × width × 3) uint8 RGB.
        # Columns scroll left; write cursor tracks next column to overwrite.
        self._buf = np.zeros((_DISPLAY_HEIGHT, _DISPLAY_WIDTH, 3), dtype=np.uint8)
        self._cursor: int = 0
        self._dirty: bool = False

        self._sample_rate: int = 48_000

        # Tail buffer: last 512 samples of the previous chunk, prepended
        # to the next to avoid discontinuities at chunk boundaries.
        self._overlap = np.zeros(_FFT_SIZE // 2, dtype=np.float64)

        # Repaint timer: independent of audio chunk rate (50 ms cadence).
        self._repaint_timer = QTimer(self)
        self._repaint_timer.setInterval(50)
        self._repaint_timer.timeout.connect(self._maybe_repaint)
        self._repaint_timer.start()

    def set_sample_rate(self, rate: int) -> None:
        """Update sample rate so frequency markers scale correctly."""
        self._sample_rate = max(1, rate)

    @Slot(object)
    def add_rx_column(self, chunk: object) -> None:
        """Append one RX audio chunk to the waterfall (cool palette)."""
        self._add_column(np.asarray(chunk, dtype=np.float64).ravel(), warm=False)

    @Slot(object)
    def add_tx_column(self, chunk: object) -> None:
        """Append one TX audio chunk to the waterfall (warm palette)."""
        arr = np.asarray(chunk, dtype=np.float64).ravel()
        # TX samples arrive as int16 PCM in [-32768, 32767] — normalise.
        if arr.size > 0 and np.max(np.abs(arr)) > 1.5:
            arr = arr / 32768.0
        self._add_column(arr, warm=True)

    def clear(self) -> None:
        """Reset the waterfall to black."""
        self._buf[:] = 0
        self._cursor = 0
        self._overlap[:] = 0
        self._dirty = True

    # --- internal -----------------------------------------------------------

    def _add_column(self, data: np.ndarray, warm: bool) -> None:
        """Compute one windowed FFT and write one display column."""
        combined = np.concatenate([self._overlap, data])
        if combined.size < _FFT_SIZE:
            # Not enough data yet — save for next call.
            self._overlap = combined[-(min(combined.size, _FFT_SIZE // 2)):]
            return

        frame = combined[-_FFT_SIZE:] * _HANN
        self._overlap = combined[-(_FFT_SIZE // 2):]

        mag = np.abs(np.fft.rfft(frame))[:_DISPLAY_BINS]
        mag = np.where(mag > 0.0, mag, 1e-12)
        db = 20.0 * np.log10(mag / (_FFT_SIZE / 2.0))

        idx = np.clip(
            ((db - _DB_MIN) / (_DB_MAX - _DB_MIN) * 255.0).astype(np.int32),
            0, 255,
        ).astype(np.uint8)

        lut = _LUT_TX if warm else _LUT_RX
        rgb = lut[idx]  # shape (_DISPLAY_BINS, 3)

        # Stretch _DISPLAY_BINS → _DISPLAY_HEIGHT with nearest-neighbour.
        row_idx = np.linspace(0, _DISPLAY_BINS - 1, _DISPLAY_HEIGHT).astype(np.int32)
        col = rgb[row_idx]   # (height, 3), low-freq at index 0
        col = col[::-1]      # flip: low-freq at bottom (high pixel y)

        self._buf[:, self._cursor, :] = col
        self._cursor = (self._cursor + 1) % _DISPLAY_WIDTH
        self._dirty = True

    @Slot()
    def _maybe_repaint(self) -> None:
        if self._dirty:
            self._dirty = False
            self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]  # noqa: ARG002
        w = self.width()
        h = self.height()
        painter = QPainter(self)

        # Roll backing buffer so oldest column is at x=0 and newest at x=W-1.
        rolled = np.roll(self._buf, -self._cursor, axis=1)

        img = QImage(
            rolled.tobytes(),
            _DISPLAY_WIDTH,
            _DISPLAY_HEIGHT,
            _DISPLAY_WIDTH * 3,
            QImage.Format.Format_RGB888,
        )
        pixmap = QPixmap.fromImage(img).scaled(
            w, h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(0, 0, pixmap)

        # Frequency markers.
        bin_hz = self._sample_rate / _FFT_SIZE
        max_hz = bin_hz * (_DISPLAY_BINS - 1)
        if max_hz <= 0:
            painter.end()
            return

        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        for freq_hz, color_str, label in _MARKERS:
            if freq_hz > max_hz:
                continue
            # y=0 → top of widget (high freq); y=h → bottom (0 Hz).
            y = int(h * (1.0 - freq_hz / max_hz))
            pen = QPen(QColor(color_str))
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawLine(0, y, w, y)
            painter.setPen(QColor(color_str))
            painter.drawText(2, max(y - 2, 9), label)

        painter.end()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class WaterfallWindow(QMainWindow):
    """Floating window wrapping ``WaterfallWidget``.

    Emits ``closed`` when the user clicks the title-bar X so MainWindow can
    uncheck the View → Waterfall menu action.
    """

    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open-SSTV — Waterfall")
        self.resize(_DISPLAY_WIDTH + 16, _DISPLAY_HEIGHT + 32)
        self._widget = WaterfallWidget()
        self.setCentralWidget(self._widget)

    # Public proxy slots so MainWindow can connect signals directly.

    @Slot(object)
    def add_rx_column(self, chunk: object) -> None:
        self._widget.add_rx_column(chunk)

    @Slot(object)
    def add_tx_column(self, chunk: object) -> None:
        self._widget.add_tx_column(chunk)

    def set_sample_rate(self, rate: int) -> None:
        self._widget.set_sample_rate(rate)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        event.accept()


__all__ = ["WaterfallWidget", "WaterfallWindow"]
