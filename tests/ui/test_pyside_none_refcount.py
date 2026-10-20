# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression guard for the PySide6 6.12.0 ``none_dealloc`` abort.

v0.6.16's Linux AppImage died about a minute into RX with::

    Fatal Python error: none_dealloc: deallocating None

PySide6 6.12.0 on Python 3.11 drops one reference to ``None`` too many
in some void-returning Qt methods.  ``LevelMeter.paintEvent`` calls
``QLinearGradient.setColorAt`` about 12 times per repaint at ~30 fps, so
``None``'s refcount bled away until it hit zero and the interpreter
aborted.  v0.6.17 pins ``PySide6<6.12`` (PR #84); these tests make sure
the pin can't be relaxed onto a still-broken PySide6 without CI noticing.

The check is a net *decrease* in ``sys.getrefcount(None)`` — a healthy
run drifts upward a little (cached frames, interned results), never down
by hundreds.

Skipped on Python 3.12+, where ``None`` is immortal (PEP 683): its
refcount is pinned and the bug can't bite there.

Deliberately *not* marked ``gui``: CI runs ``-m "not gui"`` under
``QT_QPA_PLATFORM=offscreen``, and the Linux py3.11 jobs are the ones
that matter (the bug did not reproduce on macOS arm64 wheels).
"""
from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QLinearGradient, QPen

from open_sstv.ui.level_meter import LevelMeter

pytestmark = pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason="None is immortal on Python 3.12+ (PEP 683)",
)

#: Allowed net decrease in None's refcount.  A leak of one reference per
#: call loses hundreds to thousands over these loops; noise is a handful.
_TOLERANCE = 10


def _none_refs() -> int:
    return sys.getrefcount(None)


def test_level_meter_repaint_keeps_none_refcount(qtbot) -> None:
    meter = LevelMeter()
    qtbot.addWidget(meter)
    meter.resize(24, 160)
    # Bar and hold marker both above the floor so every branch of
    # paintEvent (both gradients, graticule, hold line) runs.
    meter.set_level(0.9)
    meter.grab()  # warm-up: first paint populates Qt/PySide caches

    before = _none_refs()
    for _ in range(300):
        meter.grab()
    after = _none_refs()

    assert after >= before - _TOLERANCE, (
        f"None refcount fell by {before - after} over 300 LevelMeter "
        f"repaints — PySide6 {_pyside_version()} is leaking None "
        "references (the 6.12.0 none_dealloc bug)"
    )


def _call_set_color_at() -> None:
    grad = QLinearGradient(0.0, 0.0, 0.0, 100.0)
    color = QColor(214, 79, 79)
    for i in range(1000):
        grad.setColorAt((i % 100) / 100.0, color)


def _call_set_stops() -> None:
    grad = QLinearGradient(0.0, 0.0, 0.0, 100.0)
    stops = [(0.0, QColor(214, 79, 79)), (1.0, QColor(46, 163, 107))]
    for _ in range(1000):
        grad.setStops(stops)


def _call_pen_set_style() -> None:
    pen = QPen()
    styles = (Qt.PenStyle.DotLine, Qt.PenStyle.SolidLine)
    for i in range(1000):
        pen.setStyle(styles[i % 2])


@pytest.mark.parametrize(
    "calls",
    [_call_set_color_at, _call_set_stops, _call_pen_set_style],
    ids=["QLinearGradient.setColorAt", "QLinearGradient.setStops", "QPen.setStyle"],
)
def test_void_qt_setters_keep_none_refcount(calls) -> None:
    calls()  # warm-up

    before = _none_refs()
    calls()
    after = _none_refs()

    assert after >= before - _TOLERANCE, (
        f"None refcount fell by {before - after} over 1000 calls — "
        f"PySide6 {_pyside_version()} is leaking None references "
        "(the 6.12.0 none_dealloc bug)"
    )


def _pyside_version() -> str:
    import PySide6

    return PySide6.__version__
