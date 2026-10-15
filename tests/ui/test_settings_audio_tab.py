# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Audio tab (v0.6.14 audit UI-M3 and the gain-rounding Low).

UI-M3: with the saved USB codec unplugged, the device combos fell back to
"System default", and pressing OK for any reason overwrote the saved
device names.  An absent saved device now appears as "<name> (not
connected)" and survives OK.

Low: the gain sliders were seeded with ``int(gain * 100)``.  Float
products such as 0.29 * 100 = 28.999… truncated, so every OK ratcheted
the saved gain down by 1 %.
"""
from __future__ import annotations

import pytest

from open_sstv.audio.devices import AudioDevice
from open_sstv.config.schema import AppConfig
from open_sstv.ui.settings_dialog import SettingsDialog

pytestmark = pytest.mark.gui

_PRESENT_IN = AudioDevice(
    index=0, name="Built-in Mic", host_api="Core Audio",
    max_input_channels=1, max_output_channels=0, default_sample_rate=48000.0,
)
_PRESENT_OUT = AudioDevice(
    index=1, name="Built-in Speakers", host_api="Core Audio",
    max_input_channels=0, max_output_channels=2, default_sample_rate=48000.0,
)


@pytest.fixture
def no_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "open_sstv.ui.settings_dialog.list_input_devices", lambda: [_PRESENT_IN]
    )
    monkeypatch.setattr(
        "open_sstv.ui.settings_dialog.list_output_devices", lambda: [_PRESENT_OUT]
    )
    monkeypatch.setattr(
        "open_sstv.ui.settings_dialog.list_pipewire_sinks", lambda: []
    )


def _dialog(qtbot, cfg: AppConfig) -> SettingsDialog:
    dlg = SettingsDialog(config=cfg, rig_connected=False)
    qtbot.addWidget(dlg)
    return dlg


def test_unplugged_devices_are_listed_and_preserved(qtbot, no_codec) -> None:
    cfg = AppConfig(
        callsign="W0AEZ",
        audio_input_device="USB Audio CODEC",
        audio_output_device="USB Audio CODEC ",
    )
    dlg = _dialog(qtbot, cfg)
    assert dlg._input_combo.currentText() == "USB Audio CODEC (not connected)"
    assert dlg._output_combo.currentText() == "USB Audio CODEC  (not connected)"
    out = dlg.result_config()
    assert out.audio_input_device == "USB Audio CODEC"
    assert out.audio_output_device == "USB Audio CODEC "


def test_present_device_gets_no_placeholder(qtbot, no_codec) -> None:
    cfg = AppConfig(callsign="W0AEZ", audio_input_device="Built-in Mic")
    dlg = _dialog(qtbot, cfg)
    texts = [dlg._input_combo.itemText(i) for i in range(dlg._input_combo.count())]
    assert not any("not connected" in t for t in texts)
    assert dlg.result_config().audio_input_device == "Built-in Mic"


def test_default_device_gets_no_placeholder(qtbot, no_codec) -> None:
    dlg = _dialog(qtbot, AppConfig(callsign="W0AEZ"))
    assert dlg._input_combo.currentData() is None
    assert dlg._output_combo.currentData() is None


@pytest.mark.parametrize("gain", [0.29, 0.57, 0.58, 1.13])
def test_gain_round_trips_through_ok(qtbot, no_codec, gain: float) -> None:
    cfg = AppConfig(
        callsign="W0AEZ", audio_input_gain=gain, audio_output_gain=min(gain, 1.0)
    )
    out = _dialog(qtbot, cfg).result_config()
    assert out.audio_input_gain == pytest.approx(gain)
    assert out.audio_output_gain == pytest.approx(min(gain, 1.0))
