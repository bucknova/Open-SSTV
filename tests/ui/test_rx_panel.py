# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest-qt smoke tests for ``open_sstv.ui.rx_panel.RxPanel``.

Covers button → signal wiring on the RX panel.  Heavier integration
behaviour (gallery thumbnail flow, RX→TX pipeline) lives in
``test_rx_to_tx_pipeline.py``.
"""
from __future__ import annotations

import pytest

from open_sstv.ui.rx_panel import RxPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def panel(qtbot) -> RxPanel:
    p = RxPanel()
    qtbot.addWidget(p)
    return p


def test_start_button_emits_capture_requested(qtbot, panel: RxPanel) -> None:
    with qtbot.waitSignal(panel.capture_requested, timeout=1000) as blocker:
        panel._start_btn.click()
    # Toggle semantics: initial click means "start" (True).
    assert blocker.args == [True]


def test_clear_button_emits_clear_requested(qtbot, panel: RxPanel) -> None:
    with qtbot.waitSignal(panel.clear_requested, timeout=1000):
        panel._clear_btn.click()


def test_decode_audio_button_emits_signal(qtbot, panel: RxPanel) -> None:
    """v0.3.10: Decode Audio button replaces the v0.3.9 File-menu item.

    The button always emits — no payload, no payload-shape coupling.
    MainWindow handles the file picker.
    """
    with qtbot.waitSignal(
        panel.decode_audio_file_requested, timeout=1000
    ):
        panel._decode_audio_btn.click()


def test_decode_audio_button_always_enabled(panel: RxPanel) -> None:
    """Unlike Save Image (which requires a decoded image), the Decode
    Audio button is always enabled — file decoding doesn't depend on
    panel state."""
    assert panel._decode_audio_btn.isEnabled()
    # Even after toggling capture state, the button stays enabled.
    panel.set_capturing(True)
    assert panel._decode_audio_btn.isEnabled()
    panel.set_capturing(False)
    assert panel._decode_audio_btn.isEnabled()
