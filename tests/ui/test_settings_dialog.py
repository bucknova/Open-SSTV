# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SettingsDialog BZ-series fixes.

BZ-01: reject() must terminate any rigctld process launched during the dialog
       session and clear _rigctld_proc to None.
BZ-02: _refresh_banner_preview must use the live callsign widget value, not
       the stale self._config.callsign value captured at dialog construction.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.ui.settings_dialog import SettingsDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def default_config() -> AppConfig:
    return AppConfig(callsign="W0AEZ")


@pytest.fixture
def dialog(qtbot, default_config: AppConfig) -> SettingsDialog:
    dlg = SettingsDialog(config=default_config, rig_connected=False)
    qtbot.addWidget(dlg)
    return dlg


# ---------------------------------------------------------------------------
# BZ-01: orphan rigctld process
# ---------------------------------------------------------------------------


class TestRejectKillsRigctld:
    """reject() must call _stop_rigctld() so no orphan process is left."""

    def test_reject_with_no_process_does_not_crash(self, dialog: SettingsDialog) -> None:
        """Baseline: reject() with no process launched is a no-op."""
        assert dialog._rigctld_proc is None
        dialog.reject()  # must not raise
        assert dialog._rigctld_proc is None

    def test_reject_terminates_launched_process(
        self, dialog: SettingsDialog
    ) -> None:
        """After _launch_rigctld, reject() must kill the subprocess."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            # Simulate picking a radio model so the launch guard passes
            dialog._custom_model_id.setValue(1035)
            dialog._launch_rigctld()

        assert dialog._rigctld_proc is not None, "process should be stored after launch"

        dialog.reject()

        assert dialog._rigctld_proc is None, "reject() must clear _rigctld_proc"
        mock_proc.terminate.assert_called_once()

    def test_accept_does_not_kill_process(
        self, dialog: SettingsDialog
    ) -> None:
        """accept() must NOT kill the process — ownership passes to MainWindow."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            dialog._custom_model_id.setValue(1035)
            dialog._launch_rigctld()

        assert dialog._rigctld_proc is mock_proc

        # accept() must leave the process alive for MainWindow to adopt
        dialog.accept()

        mock_proc.terminate.assert_not_called()
        # rigctld_process property still returns it
        assert dialog.rigctld_process is mock_proc


# ---------------------------------------------------------------------------
# BZ-02: banner preview uses live callsign, not config.callsign
# ---------------------------------------------------------------------------


class TestBannerPreviewLiveCallsign:
    """_refresh_banner_preview must pass the live callsign text, not config."""

    def test_preview_uses_widget_value(
        self, dialog: SettingsDialog, default_config: AppConfig
    ) -> None:
        """Changing the callsign widget must change what the preview renders."""
        captured_callsigns: list[str] = []

        def _fake_apply_tx_banner(source, version, callsign, *args, **kwargs):
            captured_callsigns.append(callsign)
            # Return a minimal 320×240 image to satisfy the preview logic
            from PIL import Image
            return Image.new("RGB", (320, 240), (0x80, 0x80, 0x80))

        with patch(
            "open_sstv.ui.settings_dialog.apply_tx_banner",
            side_effect=_fake_apply_tx_banner,
        ):
            # Change the callsign to something different from the config
            dialog._callsign.setText("N0CALL")
            dialog._refresh_banner_preview()

        assert captured_callsigns, "apply_tx_banner should have been called"
        assert captured_callsigns[-1] == "N0CALL", (
            f"preview should use live callsign 'N0CALL', got {captured_callsigns[-1]!r}"
        )
        assert captured_callsigns[-1] != default_config.callsign, (
            "preview must not use the stale config.callsign"
        )

    def test_preview_strips_and_uppercases(
        self, dialog: SettingsDialog
    ) -> None:
        """Callsign passed to the preview is stripped and upper-cased."""
        captured: list[str] = []

        def _fake(source, version, callsign, *args, **kwargs):
            captured.append(callsign)
            from PIL import Image
            return Image.new("RGB", (320, 240), (0x80, 0x80, 0x80))

        with patch("open_sstv.ui.settings_dialog.apply_tx_banner", side_effect=_fake):
            dialog._callsign.setText("  w0aez  ")
            dialog._refresh_banner_preview()

        assert captured[-1] == "W0AEZ"

    def test_textchanged_triggers_preview_refresh(
        self, dialog: SettingsDialog
    ) -> None:
        """textChanged on _callsign must trigger a banner preview refresh."""
        call_count = [0]
        original = dialog._refresh_banner_preview

        def _counting_refresh():
            call_count[0] += 1
            original()

        dialog._refresh_banner_preview = _counting_refresh  # type: ignore[method-assign]

        before = call_count[0]
        dialog._callsign.setText("KD9ABC")
        after = call_count[0]

        assert after > before, "textChanged should have triggered _refresh_banner_preview"
