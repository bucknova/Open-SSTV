# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.level_meter.LevelMeter``."""
from __future__ import annotations

import pytest

from open_sstv.ui.level_meter import (
    _DB_FLOOR,
    LevelMeter,
    _linear_to_dbfs,
)

pytestmark = pytest.mark.gui


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (1.0, 0.0),
        (0.5, -6.02),
        (0.1, -20.0),
        (0.0, _DB_FLOOR),
        (1e-9, _DB_FLOOR),
        (-1.0, _DB_FLOOR),
        (2.0, 0.0),  # clipped to ceiling
    ],
)
def test_linear_to_dbfs(peak: float, expected: float) -> None:
    assert _linear_to_dbfs(peak) == pytest.approx(expected, abs=0.05)


@pytest.fixture
def meter(qtbot) -> LevelMeter:
    m = LevelMeter()
    qtbot.addWidget(m)
    return m


def test_set_level_attacks_instantly(meter: LevelMeter) -> None:
    meter.set_level(0.5)
    assert meter._bar_db == pytest.approx(-6.02, abs=0.05)
    assert meter._hold_db == pytest.approx(-6.02, abs=0.05)
    assert meter._timer.isActive()


def test_lower_level_does_not_pull_bar_down_immediately(meter: LevelMeter) -> None:
    meter.set_level(1.0)
    meter.set_level(0.01)
    # Attack only — the bar stays up and decays via the timer instead.
    assert meter._bar_db == pytest.approx(0.0, abs=0.05)


def test_reset_drops_to_floor_and_stops_timer(meter: LevelMeter) -> None:
    meter.set_level(0.8)
    meter.reset()
    assert meter._bar_db == _DB_FLOOR
    assert meter._hold_db == _DB_FLOOR
    assert not meter._timer.isActive()


def test_decay_lowers_bar_and_eventually_stops(qtbot, meter: LevelMeter) -> None:
    meter.set_level(1.0)
    start = meter._bar_db
    qtbot.wait(200)
    assert meter._bar_db < start
    # Long enough for both the bar and the slow peak-hold marker to fall
    # all the way back to silence, after which the timer must stop itself.
    qtbot.waitUntil(lambda: not meter._timer.isActive(), timeout=8000)
    assert meter._bar_db == _DB_FLOOR
    assert meter._hold_db == _DB_FLOOR


def test_paints_without_error(meter: LevelMeter) -> None:
    meter.resize(24, 160)
    meter.set_level(0.6)
    meter.grab()  # forces a paintEvent
