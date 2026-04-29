# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.first_launch_dialog.FirstLaunchDialog``
and the ``MainWindow`` trigger that shows it on a fresh install.

Covers:

* Uppercase coercion of the callsign input.
* ``callsign()`` returns trimmed + uppercased.
* Save button sets ``DialogCode.Accepted``; Skip sets ``Rejected``.
* ``load_config`` migration — a pre-v0.2.7 TOML file (no
  ``first_launch_seen`` key) auto-grandfathers the user to
  ``first_launch_seen=True`` so we don't nag upgraders.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from open_sstv.config.schema import AppConfig
from open_sstv.config.store import load_config
from open_sstv.ui.first_launch_dialog import FirstLaunchDialog

pytestmark = pytest.mark.gui


# === dialog widget ===


def test_typing_lowercase_is_forced_uppercase(qapp, qtbot) -> None:
    """Callsigns render canonically uppercase on FCC ULS, LOTW, QRZ —
    so the input field forces uppercase as the user types."""
    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)

    dlg._callsign_input.setText("w0aez")
    assert dlg._callsign_input.text() == "W0AEZ"


def test_typing_mixed_case_is_forced_uppercase(qapp, qtbot) -> None:
    """Mixed case should also be normalised (``Ve3Abc`` → ``VE3ABC``)."""
    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)

    dlg._callsign_input.setText("Ve3Abc/Mm")
    assert dlg._callsign_input.text() == "VE3ABC/MM"


def test_callsign_accessor_trims_and_uppercases(qapp, qtbot) -> None:
    """``callsign()`` must strip surrounding whitespace so a pasted
    value with trailing spaces round-trips cleanly."""
    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)

    dlg._callsign_input.setText("  w0aez  ")
    # setText already triggers the uppercase coercion — but strip still
    # has work to do because the whitespace survives.
    assert dlg.callsign() == "W0AEZ"


def test_save_button_accepts_dialog(qapp, qtbot) -> None:
    """Clicking *Save* must put the dialog in the Accepted state."""
    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)

    dlg._callsign_input.setText("w0aez")
    dlg._save_btn.click()

    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.callsign() == "W0AEZ"


def test_skip_button_rejects_dialog(qapp, qtbot) -> None:
    """Clicking *Skip for now* must put the dialog in the Rejected state
    regardless of whether the user typed anything."""
    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)

    # Type something — skip should ignore it.
    dlg._callsign_input.setText("w0aez")
    dlg._skip_btn.click()

    assert dlg.result() == QDialog.DialogCode.Rejected


# === config migration ===


def test_existing_toml_without_key_grandfathers_user(
    tmp_path: Path,
) -> None:
    """A pre-v0.2.7 config file that never carried ``first_launch_seen``
    must load with ``first_launch_seen=True`` — we don't prompt existing
    users just because they upgraded."""
    toml_path = tmp_path / "config.toml"
    # Mimic a config file written by v0.2.6 or earlier: callsign set,
    # no first_launch_seen key.
    toml_path.write_text(
        'callsign = "W0AEZ"\n'
        'sample_rate = 48000\n'
        'default_tx_mode = "robot_36"\n'
    )

    cfg = load_config(path=toml_path)

    assert cfg.first_launch_seen is True
    assert cfg.callsign == "W0AEZ"


def test_missing_toml_file_keeps_false_default(tmp_path: Path) -> None:
    """If the TOML file doesn't exist at all, a truly fresh install,
    ``first_launch_seen`` stays False so the welcome dialog fires."""
    toml_path = tmp_path / "nonexistent.toml"
    assert not toml_path.exists()

    cfg = load_config(path=toml_path)

    assert cfg.first_launch_seen is False
    assert cfg == AppConfig()


def test_toml_with_explicit_false_is_respected(tmp_path: Path) -> None:
    """If a user somehow ends up with ``first_launch_seen = false`` in
    their TOML (manual edit, test setup), the migration must NOT rewrite
    it to True — only the absence of the key triggers the grandfather
    path."""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        'first_launch_seen = false\n'
        'callsign = ""\n'
    )

    cfg = load_config(path=toml_path)

    assert cfg.first_launch_seen is False


def test_toml_with_explicit_true_is_respected(tmp_path: Path) -> None:
    """A user who saved their config post-v0.2.7 carries the explicit
    True value through — no migration rewrite."""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        'first_launch_seen = true\n'
        'callsign = "W0AEZ"\n'
    )

    cfg = load_config(path=toml_path)

    assert cfg.first_launch_seen is True


# ---------------------------------------------------------------------------
# v0.3.4 — optional Name / Grid Square / QTH inputs
# ---------------------------------------------------------------------------


class TestOperatorInfoInputs:
    """The first-launch dialog gained three optional fields in v0.3.4 so
    new users can populate the v0.3 template tokens (``{name}``,
    ``{grid}``, ``{qth}``) without immediately hunting through Settings.
    All three are optional; empty submissions still flow through the
    Save / Skip dispatch unchanged."""

    def test_inputs_exist(self, qapp, qtbot) -> None:
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        assert dlg._name_input is not None
        assert dlg._grid_input is not None
        assert dlg._qth_input is not None

    def test_all_three_default_empty(self, qapp, qtbot) -> None:
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        assert dlg.operator_name() == ""
        assert dlg.grid_square() == ""
        assert dlg.qth() == ""

    def test_grid_input_forces_uppercase(self, qapp, qtbot) -> None:
        """Maidenhead grid renders canonically with the field letters
        uppercase (EM29, FN42, etc.).  Same as-you-type pattern as
        callsign."""
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg._grid_input.setText("em29lk")
        assert dlg._grid_input.text() == "EM29LK"

    def test_grid_input_max_length_is_six(self, qapp, qtbot) -> None:
        """6-character subsquare precision is the practical maximum;
        anything longer is non-standard."""
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        assert dlg._grid_input.maxLength() == 6

    def test_name_input_does_not_force_uppercase(self, qapp, qtbot) -> None:
        """Operator name should preserve mixed case — it's a display
        string, not a callsign."""
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg._name_input.setText("Kevin")
        assert dlg._name_input.text() == "Kevin"

    def test_qth_input_does_not_force_uppercase(self, qapp, qtbot) -> None:
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg._qth_input.setText("Kansas City, MO")
        assert dlg._qth_input.text() == "Kansas City, MO"

    def test_accessors_trim_whitespace(self, qapp, qtbot) -> None:
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg._name_input.setText("  Kevin  ")
        dlg._grid_input.setText("  em29  ")
        dlg._qth_input.setText("  Kansas City, MO  ")
        assert dlg.operator_name() == "Kevin"
        assert dlg.grid_square() == "EM29"
        assert dlg.qth() == "Kansas City, MO"

    def test_save_with_all_fields_populated(self, qapp, qtbot) -> None:
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg.show()
        qtbot.waitExposed(dlg)

        dlg._callsign_input.setText("w0aez")
        dlg._name_input.setText("Kevin")
        dlg._grid_input.setText("em29")
        dlg._qth_input.setText("Kansas City, MO")
        dlg._save_btn.click()

        assert dlg.result() == QDialog.DialogCode.Accepted
        assert dlg.callsign() == "W0AEZ"
        assert dlg.operator_name() == "Kevin"
        assert dlg.grid_square() == "EM29"
        assert dlg.qth() == "Kansas City, MO"

    def test_save_with_only_callsign_leaves_optional_fields_empty(
        self, qapp, qtbot
    ) -> None:
        """The optional-fields contract: a user who only fills callsign
        and clicks Save should not silently get garbage in the other
        fields — they stay empty and the caller decides whether to
        write them."""
        dlg = FirstLaunchDialog()
        qtbot.addWidget(dlg)
        dlg.show()
        qtbot.waitExposed(dlg)

        dlg._callsign_input.setText("w0aez")
        dlg._save_btn.click()

        assert dlg.result() == QDialog.DialogCode.Accepted
        assert dlg.callsign() == "W0AEZ"
        assert dlg.operator_name() == ""
        assert dlg.grid_square() == ""
        assert dlg.qth() == ""
