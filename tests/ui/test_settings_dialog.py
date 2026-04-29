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


# ---------------------------------------------------------------------------
# v0.3.3 — minimum width bumped 480 → 640 so the rigctld group renders
# without truncation at default size
# ---------------------------------------------------------------------------


class TestRigctldGroupFitsAtDefaultWidth:
    """The Radio tab's rigctld group has a long title ("rigctld — Hamlib
    Daemon"), a wrapped help paragraph, a verbose checkbox label
    ("Auto-launch rigctld on Connect"), and a multi-button row.  At the
    pre-v0.3.3 minimum of 480 px these all clipped and the user had to
    drag the dialog wider before they could read the panel.

    The fix is a higher floor on the dialog's minimum width.  These
    tests pin that floor and the size-hint fit so a future refactor
    that re-narrows the dialog or adds longer labels gets caught here.
    """

    def test_minimum_width_is_at_least_640(self, dialog: SettingsDialog) -> None:
        assert dialog.minimumWidth() >= 640

    def test_rigctld_group_title_is_the_expected_string(
        self, dialog: SettingsDialog
    ) -> None:
        """Sanity: the title we sized the dialog around hasn't changed.
        If a future commit extends or renames it, the width budget may
        need re-evaluating."""
        assert dialog._rigctld_group.title() == "rigctld — Hamlib Daemon"

    def test_rigctld_help_label_has_room_for_three_wrapped_lines(
        self, dialog: SettingsDialog
    ) -> None:
        """v0.3.5 regression: the rigctld help QLabel has setWordWrap(True)
        but Qt underestimates its height inside a QFormLayout, so the
        third wrapped line ("Hamlib installed.") was getting clipped at
        the top of the label box.  We reserve room for ~3 lines of
        wrapped text using font metrics; this test pins that the
        minimum height is at least that much so a future refactor that
        drops the explicit minimum reverts the clip."""
        from PySide6.QtWidgets import QLabel
        # The help label is the only QLabel inside the rigctld group
        # whose text mentions "Hamlib".
        help_labels = [
            lbl for lbl in dialog._rigctld_group.findChildren(QLabel)
            if "Hamlib" in lbl.text()
        ]
        assert len(help_labels) == 1, (
            f"expected one help label in rigctld group, found {len(help_labels)}"
        )
        help_label = help_labels[0]
        fm_h = help_label.fontMetrics().height()
        # Minimum height must accommodate at least ~3 wrapped lines.
        assert help_label.minimumHeight() >= fm_h * 3, (
            f"rigctld help label minimumHeight={help_label.minimumHeight()} "
            f"is below 3 line-heights ({fm_h} px each); wrapped text will "
            f"clip when the dialog is at its minimum width"
        )

    def test_rigctld_group_size_hint_fits_minimum_width(
        self, dialog: SettingsDialog
    ) -> None:
        """Qt's preferred size for the rigctld group must fit within the
        dialog's minimum width, with a small margin reserved for the
        QTabWidget frame and dialog padding.  If a future change adds a
        wider widget, sizeHint().width() will exceed this budget and
        signal that the floor needs another bump."""
        # Conservative margin: the QTabWidget frame and dialog content
        # margins together reserve roughly 30 px on most platforms.
        margin = 30
        hint_w = dialog._rigctld_group.sizeHint().width()
        budget = dialog.minimumWidth() - margin
        assert hint_w <= budget, (
            f"rigctld group sizeHint width {hint_w} exceeds "
            f"available budget {budget} (dialog min {dialog.minimumWidth()})"
        )


# ---------------------------------------------------------------------------
# v0.3.4 — General tab as the first settings tab
# ---------------------------------------------------------------------------


class TestGeneralTabIsFirst:
    """The new General tab consolidates app-level settings (identity,
    default TX mode, update checker) on the first tab, with Audio /
    Radio / Images focused on their own domains."""

    def test_general_tab_is_present(self, dialog: SettingsDialog) -> None:
        from PySide6.QtWidgets import QTabWidget
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        assert "General" in labels

    def test_general_tab_is_at_index_zero(self, dialog: SettingsDialog) -> None:
        from PySide6.QtWidgets import QTabWidget
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        assert tabs.tabText(0) == "General"

    def test_tab_order_general_audio_radio_images(
        self, dialog: SettingsDialog
    ) -> None:
        from PySide6.QtWidgets import QTabWidget
        tabs = dialog.findChild(QTabWidget)
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        assert labels == ["General", "Audio", "Radio", "Images"]


class TestIdentityGroupOnGeneralTab:
    """Identity group: callsign + the three new operator-info fields."""

    def test_callsign_widget_still_findable(
        self, dialog: SettingsDialog
    ) -> None:
        """Existing tests grab `dialog._callsign` directly — the move
        from Radio tab to General tab must not change the attribute name."""
        assert dialog._callsign is not None
        assert dialog._callsign.text() == "W0AEZ"

    def test_operator_name_widget_exists(
        self, dialog: SettingsDialog
    ) -> None:
        assert dialog._operator_name is not None
        assert dialog._operator_name.text() == ""

    def test_grid_square_widget_exists(
        self, dialog: SettingsDialog
    ) -> None:
        assert dialog._grid_square is not None
        assert dialog._grid_square.maxLength() == 6

    def test_qth_widget_exists(self, dialog: SettingsDialog) -> None:
        assert dialog._qth is not None
        assert dialog._qth.text() == ""

    def test_pre_populated_from_config(
        self, qtbot
    ) -> None:
        """If AppConfig already carries operator info, the General tab's
        widgets pre-populate from those values."""
        cfg = AppConfig(
            callsign="W0AEZ",
            operator_name="Kevin",
            grid_square="EM29",
            qth="Kansas City, MO",
        )
        dlg = SettingsDialog(config=cfg, rig_connected=False)
        qtbot.addWidget(dlg)
        assert dlg._callsign.text() == "W0AEZ"
        assert dlg._operator_name.text() == "Kevin"
        assert dlg._grid_square.text() == "EM29"
        assert dlg._qth.text() == "Kansas City, MO"


class TestResultConfigIncludesOperatorInfo:
    """``result_config()`` must return AppConfig with the operator-info
    fields populated from the General tab widgets."""

    def test_empty_widgets_yield_empty_strings(
        self, dialog: SettingsDialog
    ) -> None:
        cfg = dialog.result_config()
        assert cfg.operator_name == ""
        assert cfg.grid_square == ""
        assert cfg.qth == ""

    def test_populated_widgets_propagate_to_result(
        self, dialog: SettingsDialog
    ) -> None:
        dialog._operator_name.setText("Kevin")
        dialog._grid_square.setText("em29")
        dialog._qth.setText("  Kansas City, MO  ")
        cfg = dialog.result_config()
        assert cfg.operator_name == "Kevin"
        assert cfg.grid_square == "EM29"  # uppercased on save
        assert cfg.qth == "Kansas City, MO"  # whitespace stripped


class TestRadioTabPttGroupRenamed:
    """After moving Callsign out, the Radio tab's group is just "PTT" —
    no longer "PTT / Identity"."""

    def test_callsign_not_in_radio_tab_ptt_group(
        self, dialog: SettingsDialog
    ) -> None:
        """The Callsign widget should not be a descendant of any group
        box on the Radio tab — it lives on the General tab now."""
        from PySide6.QtWidgets import QGroupBox
        # Find every group box; verify the one titled "PTT" doesn't
        # contain the callsign widget.
        for group in dialog.findChildren(QGroupBox):
            if group.title() == "PTT":
                assert dialog._callsign not in group.findChildren(
                    type(dialog._callsign)
                )

    def test_ptt_group_title_is_just_ptt(self, dialog: SettingsDialog) -> None:
        from PySide6.QtWidgets import QGroupBox
        titles = [g.title() for g in dialog.findChildren(QGroupBox)]
        # The renamed group is present; the old title is gone.
        assert "PTT" in titles
        assert "PTT / Identity" not in titles


class TestImagesTabHasNoUpdatesGroup:
    """The Updates group moved from Images to General; the Images tab
    must no longer carry an Updates QGroupBox."""

    def test_no_updates_group_on_images(
        self, dialog: SettingsDialog
    ) -> None:
        from PySide6.QtWidgets import QGroupBox
        titles = [g.title() for g in dialog.findChildren(QGroupBox)]
        # "Updates" still appears once — on the General tab, not Images.
        # Counting > 1 would mean we duplicated; counting 0 would mean
        # we lost the move target.
        assert titles.count("Updates") == 1


class TestDefaultTxModeOnGeneralTab:
    """``default_tx_mode`` was on the Images tab pre-v0.3.4; it now
    lives on General with the same widget attribute name (``_tx_mode``)
    so existing autosave-preview wiring keeps working."""

    def test_tx_mode_widget_still_findable(
        self, dialog: SettingsDialog
    ) -> None:
        assert dialog._tx_mode is not None

    def test_tx_mode_pre_selects_config_value(self, qtbot) -> None:
        cfg = AppConfig(callsign="W0AEZ", default_tx_mode="robot_36")
        dlg = SettingsDialog(config=cfg, rig_connected=False)
        qtbot.addWidget(dlg)
        assert dlg._tx_mode.currentData() == "robot_36"

    def test_tx_mode_propagates_to_result_config(
        self, dialog: SettingsDialog
    ) -> None:
        idx = dialog._tx_mode.findData("scottie_s1")
        assert idx >= 0
        dialog._tx_mode.setCurrentIndex(idx)
        cfg = dialog.result_config()
        assert cfg.default_tx_mode == "scottie_s1"


class TestAutosavePatternHelpButton:
    """v0.3.4 polish: a "?" button next to the filename template field
    surfaces the token reference more discoverably than the hover-only
    tooltip — macOS Qt sometimes fails to show QLineEdit tooltips
    reliably and new users don't know to hover."""

    def test_help_button_exists(self, dialog: SettingsDialog) -> None:
        assert dialog._autosave_pattern_help_btn is not None

    def test_help_button_has_question_mark_label(
        self, dialog: SettingsDialog
    ) -> None:
        assert dialog._autosave_pattern_help_btn.text() == "?"

    def test_help_button_sits_next_to_pattern_field(
        self, dialog: SettingsDialog
    ) -> None:
        """Both the field and button must share a parent layout so they
        appear on the same form row.  We assert their parent widget is
        the same (the row container)."""
        assert dialog._autosave_pattern.parentWidget() is (
            dialog._autosave_pattern_help_btn.parentWidget()
        )

    def test_help_button_click_invokes_message_box(
        self, dialog: SettingsDialog
    ) -> None:
        """Clicking the button calls QMessageBox.information with content
        that includes the token reference (<tt>%d</tt>, <tt>%c</tt>, etc.).
        We patch QMessageBox.information so the test doesn't actually
        pop a modal dialog."""
        with patch(
            "open_sstv.ui.settings_dialog.QMessageBox.information"
        ) as mock_info:
            dialog._autosave_pattern_help_btn.click()
            assert mock_info.called
            args, _kwargs = mock_info.call_args
            # args[0] is the parent widget; args[1] is the title;
            # args[2] is the body text.
            assert args[0] is dialog
            assert "Filename Template" in args[1]
            body = args[2]
            # Spot-check that the body lists the load-bearing tokens.
            for token in ("%d", "%t", "%c", "%m", "%%"):
                assert token in body, f"help body missing token reference {token!r}"
