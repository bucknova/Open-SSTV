# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.radio_panel.RadioPanel``.

Focus: the v0.2.11 connect-timeout / Cancel-button behaviour.  The older
connect/disconnect/TX-lock paths are exercised indirectly through the
MainWindow integration tests; these tests zero in on the new ``_connecting``
state surface.
"""
from __future__ import annotations

import pytest

from open_sstv.radio.band_plan import SSTV_BAND_PLAN
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


# === _build_band_menu() structure ===


class TestBandMenuStructure:
    """Pin the structure of the band-plan popup menu built by _build_band_menu()."""

    def test_separators_land_at_region_boundaries(self, panel: RadioPanel) -> None:
        """A separator must appear at each region transition (HF→VHF, VHF→UHF)
        and nowhere else."""
        menu = panel._build_band_menu()
        actions = menu.actions()

        # Reconstruct the expected separator pattern from the data layer.
        expected_seps: list[bool] = []
        last_region = ""
        for entry in SSTV_BAND_PLAN:
            if last_region and entry.region != last_region:
                expected_seps.append(True)
            expected_seps.append(False)
            last_region = entry.region

        assert len(actions) == len(expected_seps)
        for i, (action, is_sep) in enumerate(zip(actions, expected_seps)):
            assert action.isSeparator() == is_sep, (
                f"actions[{i}]: expected {'separator' if is_sep else 'entry'}, "
                f"got {'separator' if action.isSeparator() else 'entry'}"
            )

    def test_lambda_binds_correct_freq_per_entry(self, panel: RadioPanel) -> None:
        """Each action must emit the (freq_hz, rig_mode, passband_hz) it was
        built from — not the last loop value."""
        menu = panel._build_band_menu()
        non_sep = [a for a in menu.actions() if not a.isSeparator()]
        assert len(non_sep) == len(SSTV_BAND_PLAN)

        emitted: list[tuple[int, str, int]] = []
        panel.tune_requested.connect(lambda f, m, p: emitted.append((f, m, p)))
        for action, entry in zip(non_sep, SSTV_BAND_PLAN):
            emitted.clear()
            action.trigger()
            assert emitted == [(entry.freq_hz, entry.rig_mode, entry.passband_hz)], (
                f"{action.text()!r} emitted {emitted}"
            )
