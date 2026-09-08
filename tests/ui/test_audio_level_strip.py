# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.audio_level_strip.AudioLevelStrip``."""
from __future__ import annotations

import pytest

from open_sstv.ui.audio_level_strip import AudioLevelStrip

pytestmark = pytest.mark.gui


@pytest.fixture
def strip(qtbot) -> AudioLevelStrip:
    s = AudioLevelStrip()
    qtbot.addWidget(s)
    return s


def test_slider_tick_emits_gain_as_float(qtbot, strip: AudioLevelStrip) -> None:
    with qtbot.waitSignal(strip.rx_gain_changed, timeout=1000) as blocker:
        strip._rx_slider.setValue(150)
    assert blocker.args == [pytest.approx(1.5)]
    assert strip._rx_value.text() == "150%"


def test_slider_release_emits_committed(qtbot, strip: AudioLevelStrip) -> None:
    strip._tx_slider.setValue(80)
    with qtbot.waitSignal(strip.tx_gain_committed, timeout=1000) as blocker:
        strip._tx_slider.sliderReleased.emit()
    assert blocker.args == [pytest.approx(0.8)]


def test_tx_ceiling_follows_overdrive(strip: AudioLevelStrip) -> None:
    assert strip._tx_slider.maximum() == 100
    strip.set_tx_overdrive(True)
    assert strip._tx_slider.maximum() == 200
    strip._tx_slider.setValue(175)
    # Turning overdrive back off must pull an out-of-range value down.
    strip.set_tx_overdrive(False)
    assert strip._tx_slider.maximum() == 100
    assert strip._tx_slider.value() == 100
    assert strip._tx_value.text() == "100%"


def test_setters_do_not_emit(qtbot, strip: AudioLevelStrip) -> None:
    seen: list[float] = []
    strip.tx_gain_changed.connect(seen.append)
    strip.rx_gain_changed.connect(seen.append)
    strip.set_tx_overdrive(True)
    strip.set_tx_gain(1.8)
    strip.set_rx_gain(0.4)
    assert seen == []
    assert strip._tx_slider.value() == 180
    assert strip._rx_slider.value() == 40


def test_set_tx_gain_clamps_to_current_ceiling(strip: AudioLevelStrip) -> None:
    # Overdrive off → 150 % request must clamp to the 100 % ceiling.
    strip.set_tx_gain(1.5)
    assert strip._tx_slider.value() == 100


def test_input_level_reaches_meter(strip: AudioLevelStrip) -> None:
    strip.set_input_level(0.5)
    assert strip._meter._bar_db == pytest.approx(-6.02, abs=0.05)
    strip.reset_level()
    assert not strip._meter._timer.isActive()
