# SPDX-License-Identifier: GPL-3.0-or-later
"""LogQsoDialog — auto-fill, edits, Esc-discard, Save & New, thumbnails."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import Qt

from open_sstv.logbook.model import QSO
from open_sstv.ui.log_qso_dialog import LogQsoDialog, format_frequency

pytestmark = pytest.mark.gui


def _draft(**kw: object) -> QSO:
    defaults: dict[str, object] = {
        "direction": "RX",
        "mode": "Martin M1",
        "time_utc": datetime(2026, 6, 11, 18, 30, 0, tzinfo=UTC),
        "frequency_hz": 14_230_000,
    }
    defaults.update(kw)
    return QSO(**defaults)  # type: ignore[arg-type]


class TestFormatFrequency:
    def test_none_and_zero_are_dash(self) -> None:
        assert format_frequency(None) == "—"
        assert format_frequency(0) == "—"

    def test_khz_resolution(self) -> None:
        assert format_frequency(14_230_000) == "14.230 MHz"
        assert format_frequency(7_171_000) == "7.171 MHz"


class TestAutoFill:
    def test_tx_draft_prefills_editables(self, qtbot) -> None:
        dlg = LogQsoDialog(
            _draft(direction="TX", callsign="K1ABC", rsv_sent="595", name="Sam")
        )
        qtbot.addWidget(dlg)
        assert dlg._callsign.text() == "K1ABC"
        assert dlg._rsv_sent.currentText() == "595"
        assert dlg._rsv_received.currentText() == ""
        assert dlg._name.text() == "Sam"

    def test_rx_draft_has_empty_callsign(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft())
        qtbot.addWidget(dlg)
        assert dlg._callsign.text() == ""

    def test_title_distinguishes_new_vs_edit(self, qtbot) -> None:
        new_dlg = LogQsoDialog(_draft())
        qtbot.addWidget(new_dlg)
        assert new_dlg.windowTitle().startswith("Log QSO")
        edit_dlg = LogQsoDialog(_draft(id=7, callsign="K1ABC"))
        qtbot.addWidget(edit_dlg)
        assert edit_dlg.windowTitle().startswith("Edit QSO")
        assert "K1ABC" in edit_dlg.windowTitle()


class TestEditing:
    def test_callsign_uppercased_on_type(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft())
        qtbot.addWidget(dlg)
        dlg._callsign.setText("w0xyz")
        assert dlg._callsign.text() == "W0XYZ"

    def test_result_qso_applies_edits(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft())
        qtbot.addWidget(dlg)
        dlg._callsign.setText("k1abc")
        dlg._rsv_sent.setCurrentText("595")
        dlg._rsv_received.setCurrentText("575")
        dlg._name.setText("  Sam ")
        dlg._qth.setText("Boston, MA")
        dlg._grid.setText("fn42")
        dlg._comment.setText("first PD contact")
        q = dlg.result_qso()
        assert q.callsign == "K1ABC"
        assert q.rsv_sent == "595"
        assert q.rsv_received == "575"
        assert q.name == "Sam"
        assert q.qth == "Boston, MA"
        assert q.grid == "FN42"
        assert q.comment == "first PD contact"
        # Auto-filled facts pass through untouched.
        assert q.mode == "Martin M1"
        assert q.frequency_hz == 14_230_000

    def test_escape_rejects_without_mutation(self, qtbot) -> None:
        draft = _draft()
        dlg = LogQsoDialog(draft)
        qtbot.addWidget(dlg)
        dlg.show()
        dlg._callsign.setText("K1ABC")
        qtbot.keyClick(dlg, Qt.Key.Key_Escape)
        assert dlg.result() == int(LogQsoDialog.DialogCode.Rejected)
        # result_qso() was never called by anyone — draft untouched.
        assert draft.callsign == ""


class TestSaveAndNew:
    def test_flag_default_false_after_save(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft(), allow_save_and_new=True)
        qtbot.addWidget(dlg)
        dlg._save_btn.click()
        assert dlg.result() == int(LogQsoDialog.DialogCode.Accepted)
        assert dlg.save_and_new is False

    def test_flag_set_by_save_and_new(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft(), allow_save_and_new=True)
        qtbot.addWidget(dlg)
        assert dlg._save_new_btn is not None
        dlg._save_new_btn.click()
        assert dlg.result() == int(LogQsoDialog.DialogCode.Accepted)
        assert dlg.save_and_new is True

    def test_button_absent_in_capture_role(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft())
        qtbot.addWidget(dlg)
        assert dlg._save_new_btn is None


class TestThumbnail:
    def test_in_memory_image_renders(self, qtbot) -> None:
        img = Image.new("RGB", (320, 256), color=(0, 128, 255))
        dlg = LogQsoDialog(_draft(), preview_image=img)
        qtbot.addWidget(dlg)
        assert dlg._thumb.pixmap() is not None
        assert not dlg._thumb.pixmap().isNull()

    def test_path_fallback_renders(self, qtbot, tmp_path: Path) -> None:
        p = tmp_path / "rx.png"
        Image.new("RGB", (320, 256), color=(10, 20, 30)).save(p)
        dlg = LogQsoDialog(_draft(image_path=p))
        qtbot.addWidget(dlg)
        assert dlg._thumb.pixmap() is not None
        assert not dlg._thumb.pixmap().isNull()

    def test_missing_path_shows_indicator(self, qtbot, tmp_path: Path) -> None:
        dlg = LogQsoDialog(_draft(image_path=tmp_path / "moved-away.png"))
        qtbot.addWidget(dlg)
        assert dlg._thumb.text() == "Missing image"

    def test_no_image_shows_note(self, qtbot) -> None:
        dlg = LogQsoDialog(_draft(image_path=None))
        qtbot.addWidget(dlg)
        assert dlg._thumb.text() == "No image"
