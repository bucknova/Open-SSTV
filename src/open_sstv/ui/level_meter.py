# SPDX-License-Identifier: GPL-3.0-or-later
"""Vertical input-level meter (colour-zoned VU bar).

A small painted widget that shows the peak amplitude of the live RX audio
on a dBFS scale, so the operator can set the RX input gain by eye instead
of guessing in the Settings dialog.

Feeding
-------
``set_level(peak_linear)`` is called with the linear peak (0.0–1.0+) of
each incoming audio chunk — MainWindow computes this from
``RxWorker.waterfall_chunk`` on the GUI thread.  ``reset()`` drops the bar
to silence (called when capture stops).

Ballistics
----------
* Attack is instantaneous — a louder peak snaps the bar up immediately.
* Release is a smooth decay driven by a ~33 ms QTimer (~24 dB/s), the
  classic falling-VU look.
* A separate peak-hold marker jumps up with the bar and falls slowly
  (~12 dB/s after a short plateau).

The timer only runs while the bar or the hold marker is above the floor;
``reset()`` stops it.  A hidden meter never repaints.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

#: dBFS shown at the bottom / top of the bar.
_DB_FLOOR: float = -60.0
_DB_CEIL: float = 0.0

#: Zone boundaries (dBFS).  green < _DB_WARN < yellow < _DB_CLIP < red.
_DB_WARN: float = -6.0
_DB_CLIP: float = -1.0

#: Release ballistics, dB per second.
_BAR_RELEASE_DB_S: float = 24.0
_HOLD_RELEASE_DB_S: float = 12.0
#: How long the peak-hold marker sits still before it starts falling.
_HOLD_PLATEAU_S: float = 0.6

_TICK_MS: int = 33

#: Scale graticule marks (dBFS) drawn as faint lines.
_GRATICULE: tuple[float, ...] = (0.0, -6.0, -12.0, -20.0, -30.0, -40.0, -50.0)

_GREEN = QColor(46, 163, 107)
_YELLOW = QColor(214, 174, 79)
_RED = QColor(214, 79, 79)


def _linear_to_dbfs(peak: float) -> float:
    """Linear peak → dBFS, clamped to [``_DB_FLOOR``, ``_DB_CEIL``]."""
    if peak <= 0.0 or not math.isfinite(peak):
        return _DB_FLOOR
    db = 20.0 * math.log10(peak)
    if db < _DB_FLOOR:
        return _DB_FLOOR
    if db > _DB_CEIL:
        return _DB_CEIL
    return db


class LevelMeter(QWidget):
    """A slim vertical dBFS bar with colour zones and a peak-hold marker."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(20)
        self.setMinimumHeight(80)

        #: Current bar level and peak-hold marker, both in dBFS.
        self._bar_db: float = _DB_FLOOR
        self._hold_db: float = _DB_FLOOR
        #: Monotonic timestamp of the last upward move of the hold marker.
        self._hold_t: float = 0.0
        #: Monotonic timestamp of the previous decay tick.
        self._last_tick: float = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._decay)

        self.setToolTip(
            "Live RX input level (dBFS, post software gain).\n"
            "Green: healthy · Yellow: hot · Red: clipping — lower RX gain."
        )

    # === public API ===

    @Slot(float)
    def set_level(self, peak_linear: float) -> None:
        """Feed one chunk's linear peak amplitude."""
        db = _linear_to_dbfs(float(peak_linear))
        now = time.monotonic()
        if db >= self._bar_db:
            self._bar_db = db
        if db >= self._hold_db:
            self._hold_db = db
            self._hold_t = now
        if not self._timer.isActive():
            self._last_tick = now
            self._timer.start()
        self.update()

    @Slot()
    def reset(self) -> None:
        """Drop to silence and stop animating (capture stopped)."""
        self._timer.stop()
        self._bar_db = _DB_FLOOR
        self._hold_db = _DB_FLOOR
        self.update()

    # === internals ===

    def _decay(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        if dt <= 0.0:
            return

        self._bar_db = max(_DB_FLOOR, self._bar_db - _BAR_RELEASE_DB_S * dt)
        if now - self._hold_t >= _HOLD_PLATEAU_S:
            self._hold_db = max(_DB_FLOOR, self._hold_db - _HOLD_RELEASE_DB_S * dt)

        if self._bar_db <= _DB_FLOOR and self._hold_db <= _DB_FLOOR:
            self._timer.stop()
        self.update()

    def _db_to_y(self, db: float, top: float, height: float) -> float:
        """Map a dBFS value to a widget y-coordinate (top = 0 dBFS)."""
        frac = (db - _DB_FLOOR) / (_DB_CEIL - _DB_FLOOR)
        frac = min(1.0, max(0.0, frac))
        return top + (1.0 - frac) * height

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w = self.width()
        h = self.height()
        pal = self.palette()

        # Trough.
        painter.fillRect(0, 0, w, h, pal.color(pal.ColorRole.Base).darker(115))
        painter.setPen(QPen(pal.color(pal.ColorRole.Mid)))
        painter.drawRect(0, 0, w - 1, h - 1)

        inset = 2
        bar_x = inset
        bar_w = w - 2 * inset
        bar_top = float(inset)
        bar_h = float(h - 2 * inset)

        # Colour-zoned background gradient (dim), full height.
        grad = QLinearGradient(0.0, bar_top, 0.0, bar_top + bar_h)
        y_clip = (self._db_to_y(_DB_CLIP, bar_top, bar_h) - bar_top) / bar_h
        y_warn = (self._db_to_y(_DB_WARN, bar_top, bar_h) - bar_top) / bar_h
        grad.setColorAt(0.0, _RED.darker(220))
        grad.setColorAt(max(0.0, y_clip - 0.001), _RED.darker(220))
        grad.setColorAt(y_clip, _YELLOW.darker(220))
        grad.setColorAt(max(0.0, y_warn - 0.001), _YELLOW.darker(220))
        grad.setColorAt(y_warn, _GREEN.darker(220))
        grad.setColorAt(1.0, _GREEN.darker(220))
        painter.fillRect(int(bar_x), int(bar_top), int(bar_w), int(bar_h), grad)

        # Active fill up to the current bar level.
        if self._bar_db > _DB_FLOOR:
            fill_top = self._db_to_y(self._bar_db, bar_top, bar_h)
            fill_grad = QLinearGradient(0.0, bar_top, 0.0, bar_top + bar_h)
            fill_grad.setColorAt(0.0, _RED)
            fill_grad.setColorAt(max(0.0, y_clip - 0.001), _RED)
            fill_grad.setColorAt(y_clip, _YELLOW)
            fill_grad.setColorAt(max(0.0, y_warn - 0.001), _YELLOW)
            fill_grad.setColorAt(y_warn, _GREEN)
            fill_grad.setColorAt(1.0, _GREEN)
            painter.fillRect(
                int(bar_x),
                int(fill_top),
                int(bar_w),
                int(bar_top + bar_h - fill_top),
                fill_grad,
            )

        # Graticule.
        painter.setPen(QPen(pal.color(pal.ColorRole.Mid), 1, Qt.PenStyle.DotLine))
        for db in _GRATICULE:
            y = self._db_to_y(db, bar_top, bar_h)
            painter.drawLine(int(bar_x), int(y), int(bar_x + bar_w), int(y))

        # Peak-hold marker.
        if self._hold_db > _DB_FLOOR:
            y = self._db_to_y(self._hold_db, bar_top, bar_h)
            hold_col = pal.color(pal.ColorRole.BrightText)
            painter.setPen(QPen(hold_col, 2))
            painter.drawLine(int(bar_x), int(y), int(bar_x + bar_w), int(y))
