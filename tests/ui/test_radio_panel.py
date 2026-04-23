# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.radio_panel.RadioPanel``.

Focus: the v0.2.11 connect-timeout / Cancel-button behaviour.  The older
connect/disconnect/TX-lock paths are exercised indirectly through the
MainWindow integration tests; these tests zero in on the new ``_connecting``
state surface.
"""
from __future__ import annotations

import pytest

from open_sstv.ui.radio_panel import RadioPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def panel(qapp, qtbot) -> RadioPanel:
    p = RadioPanel()
    qtbot.addWidget(p)
    return p


# === set_connecting → Cancel button ===


def test_set_connecting_shows_cancel_text(panel: RadioPanel) -> None:
    """set_connecting() must relabel the button 'Cancel'."""
    panel.set_connecting()
    assert panel._connect_btn.text() == "Cancel"


def test_set_connecting_button_is_enabled(panel: RadioPanel) -> None:
    """Button must be *enabled* while connecting so the user can click Cancel."""
    panel.set_connecting()
    assert panel._connect_btn.isEnabled()


def test_set_connecting_status_label(panel: RadioPanel) -> None:
    """Status label must read 'Connecting…' (orange) during connecting."""
    panel.set_connecting()
    assert "Connecting" in panel._status_label.text()


# === cancel_requested signal ===


def test_click_while_connecting_emits_cancel_requested(panel: RadioPanel, qtbot) -> None:
    """Clicking the button while _connecting must emit cancel_requested,
    not connect_requested or disconnect_requested."""
    panel.set_connecting()

    with qtbot.waitSignal(panel.cancel_requested, timeout=500):
        panel._connect_btn.click()


def test_click_while_connecting_does_not_emit_connect_requested(
    panel: RadioPanel, qtbot
) -> None:
    fired: list[str] = []
    panel.connect_requested.connect(lambda: fired.append("connect"))
    panel.disconnect_requested.connect(lambda: fired.append("disconnect"))

    panel.set_connecting()
    panel._connect_btn.click()

    assert fired == [], "connect/disconnect must not fire while connecting"


# === state reset paths ===


def test_set_connection_error_resets_button_text(panel: RadioPanel) -> None:
    """After a timeout/error, button must go back to 'Connect Rig' (not 'Cancel')."""
    panel.set_connecting()
    assert panel._connect_btn.text() == "Cancel"

    panel.set_connection_error()
    assert panel._connect_btn.text() == "Connect Rig"


def test_set_connection_error_re_enables_button(panel: RadioPanel) -> None:
    panel.set_connecting()
    panel.set_connection_error()
    assert panel._connect_btn.isEnabled()


def test_set_connected_false_resets_button_text(panel: RadioPanel) -> None:
    """set_connected(False) (used by cancel handler) also resets button text."""
    panel.set_connecting()
    panel.set_connected(False)
    assert panel._connect_btn.text() == "Connect Rig"


# === TX lock does not affect Cancel availability ===


def test_tx_active_disables_button_even_while_connecting(panel: RadioPanel) -> None:
    """TX takes priority — button is disabled even if connecting."""
    panel.set_connecting()
    panel.set_tx_active(True)
    assert not panel._connect_btn.isEnabled()


def test_tx_inactive_button_enabled_while_connecting(panel: RadioPanel) -> None:
    panel.set_connecting()
    panel.set_tx_active(False)
    assert panel._connect_btn.isEnabled()
