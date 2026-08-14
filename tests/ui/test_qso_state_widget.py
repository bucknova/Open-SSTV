# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for QSOStateWidget — state capture, uppercase enforcement,
debounce signal, and Clear QSO."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from open_sstv.templates.model import QSOState
from open_sstv.ui.qso_state_widget import QSOStateWidget

pytestmark = pytest.mark.gui


@pytest.fixture
def widget(qtbot) -> QSOStateWidget:
    w = QSOStateWidget()
    qtbot.addWidget(w)
    return w


class TestInitialState:
    def test_default_state_has_empty_tocall(self, widget: QSOStateWidget) -> None:
        s = widget.get_state()
        assert s.tocall == ""

    def test_default_rst_is_595(self, widget: QSOStateWidget) -> None:
        assert widget.get_state().rst == "595"

    def test_default_name_and_note_empty(self, widget: QSOStateWidget) -> None:
        s = widget.get_state()
        assert s.tocall_name == ""
        assert s.note == ""


class TestToCallUppercase:
    def test_typing_lowercase_is_uppercased(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        widget._tocall.setText("w0xyz")
        assert widget._tocall.text() == "W0XYZ"

    def test_mixed_case_is_uppercased(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        widget._tocall.setText("K0Test")
        assert widget._tocall.text() == "K0TEST"


class TestGetState:
    def test_reflects_field_values(self, widget: QSOStateWidget) -> None:
        widget._tocall.setText("W0XYZ")
        widget._rst.setCurrentText("575")
        widget._name.setText("Alice")
        widget._note.setText("QRP")
        s = widget.get_state()
        assert s.tocall == "W0XYZ"
        assert s.rst == "575"
        assert s.tocall_name == "Alice"
        assert s.note == "QRP"

    def test_state_returns_qsostate(self, widget: QSOStateWidget) -> None:
        assert isinstance(widget.get_state(), QSOState)

    def test_empty_rst_defaults_to_595(self, widget: QSOStateWidget) -> None:
        widget._rst.setCurrentText("")
        assert widget.get_state().rst == "595"


class TestClearQSO:
    def test_clear_wipes_all_fields(self, widget: QSOStateWidget) -> None:
        widget._tocall.setText("W0XYZ")
        widget._rst.setCurrentText("575")
        widget._name.setText("Alice")
        widget._note.setText("QRP")
        widget.clear()
        s = widget.get_state()
        assert s.tocall == ""
        assert s.rst == "595"
        assert s.tocall_name == ""
        assert s.note == ""

    def test_clear_wipes_udp_log_fields(self, widget: QSOStateWidget) -> None:
        widget._rst_received.setCurrentText("589")
        widget._qth.setText("Springfield")
        widget._grid.setText("EN34")
        widget.clear()
        assert widget.get_udp_log_fields() == ("", "", "")

    def test_clear_emits_state_changed(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        widget._tocall.setText("W0XYZ")
        with qtbot.waitSignal(widget.state_changed, timeout=500) as blocker:
            widget.clear()
        state = blocker.args[0]
        assert isinstance(state, QSOState)
        assert state.tocall == ""

    def test_clear_btn_click_clears_fields(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        widget._tocall.setText("K0ABC")
        qtbot.mouseClick(widget._clear_btn, Qt.MouseButton.LeftButton)
        assert widget.get_state().tocall == ""


class TestDebounce:
    def test_state_changed_emitted_after_type(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        with qtbot.waitSignal(widget.state_changed, timeout=600) as blocker:
            widget._tocall.setText("W0XYZ")
        assert isinstance(blocker.args[0], QSOState)

    def test_state_changed_carries_current_state(
        self, qtbot, widget: QSOStateWidget
    ) -> None:
        widget._note.setText("hello")
        with qtbot.waitSignal(widget.state_changed, timeout=600) as blocker:
            widget._tocall.setText("N0CALL")
        state = blocker.args[0]
        assert state.tocall == "N0CALL"
        assert state.note == "hello"


class TestLogbookButton:
    """v0.4: the [Logbook…] button relays a logbook_requested signal."""

    def test_button_present_next_to_clear(self, widget: QSOStateWidget) -> None:
        assert widget._logbook_btn.text() == "Logbook…"

    def test_click_emits_logbook_requested(self, qtbot, widget: QSOStateWidget) -> None:
        with qtbot.waitSignal(widget.logbook_requested, timeout=1000):
            widget._logbook_btn.click()

    def test_click_does_not_touch_qso_state(self, qtbot, widget: QSOStateWidget) -> None:
        widget._tocall.setText("K1ABC")
        widget._logbook_btn.click()
        assert widget.get_state().tocall == "K1ABC"


class TestUdpLogFields:
    """v0.6.7: RST-received / QTH / Grid feed the UDP QSO log only — they
    are deliberately kept off ``QSOState`` (see module docstring)."""

    def test_default_fields_are_empty(self, widget: QSOStateWidget) -> None:
        assert widget.get_udp_log_fields() == ("", "", "")

    def test_reflects_field_values(self, widget: QSOStateWidget) -> None:
        widget._rst_received.setCurrentText("589")
        widget._qth.setText("Springfield")
        widget._grid.setText("en34")
        assert widget.get_udp_log_fields() == ("589", "Springfield", "EN34")

    def test_grid_is_uppercased_on_type(self, widget: QSOStateWidget) -> None:
        widget._grid.setText("en34")
        assert widget._grid.text() == "EN34"

    def test_not_present_on_qsostate(self, widget: QSOStateWidget) -> None:
        widget._rst_received.setCurrentText("589")
        widget._qth.setText("Springfield")
        widget._grid.setText("EN34")
        s = widget.get_state()
        assert not hasattr(s, "rsv_received")
        assert not hasattr(s, "qth")
        assert not hasattr(s, "grid")


class TestUdpLogButton:
    """v0.6.7: the [External Log] button relays a udp_log_requested signal."""

    def test_button_present_next_to_logbook(self, widget: QSOStateWidget) -> None:
        assert widget._udp_log_btn.text() == "External Log"

    def test_click_emits_udp_log_requested(self, qtbot, widget: QSOStateWidget) -> None:
        with qtbot.waitSignal(widget.udp_log_requested, timeout=1000):
            widget._udp_log_btn.click()

    def test_click_does_not_touch_qso_state(self, qtbot, widget: QSOStateWidget) -> None:
        widget._tocall.setText("K1ABC")
        widget._udp_log_btn.click()
        assert widget.get_state().tocall == "K1ABC"
