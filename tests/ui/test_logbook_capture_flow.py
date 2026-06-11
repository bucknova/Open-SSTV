# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end capture flow: TX/RX completion → draft → dialog/store.

Drives ``MainWindow``'s completion handlers directly (the worker
threads are exercised elsewhere) and asserts against a real store in a
tmp dir.  Mirrors v0.4 acceptance criteria 1, 2, 5 and 7:

1. TX complete → dialog pre-filled from the QSO-state bar → Save →
   row in the logbook.
2. RX complete → dialog with mode/freq auto-filled → Save → row.
5. Esc → no row written.
7. ``auto_log_qsos=True`` → no dialog, silent draft row.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig
from open_sstv.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "open_sstv.ui.workers.encode",
        MagicMock(return_value=np.zeros(100, dtype=np.int16)),
    )
    monkeypatch.setattr(
        "open_sstv.ui.workers.output_stream.play_blocking", MagicMock()
    )
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    yield


def _make_window(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **cfg_kwargs: object
) -> MainWindow:
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False, **cfg_kwargs)  # type: ignore[arg-type]
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    return w


def _rx_image() -> Image.Image:
    return Image.new("RGB", (320, 256), color=(30, 60, 90))


class TestRxCapture:
    def test_completion_opens_prefilled_dialog(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._last_rig_freq_hz = 14_230_000
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is not None
        dlg = window._capture_context[0]
        assert dlg.isVisible()
        assert dlg._callsign.text() == ""
        q = dlg._qso
        assert q.direction == "RX"
        assert q.mode == "Martin M1"
        assert q.frequency_hz == 14_230_000
        dlg.reject()

    def test_save_writes_row_and_image(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # auto_save is OFF — the image must still be written at log
        # time so the row keeps its picture.
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._on_rx_image_complete(_rx_image(), Mode.PD_120, 95)
        dlg = window._capture_context[0]
        dlg._callsign.setText("K1ABC")
        dlg._rsv_received.setCurrentText("575")
        dlg.accept()
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == "K1ABC"
        assert rows[0].mode == "PD 120"
        assert rows[0].rsv_received == "575"
        assert rows[0].image_path is not None
        assert rows[0].image_path.is_file()
        assert rows[0].image_path.parent == tmp_path / "images"
        assert window._capture_context is None

    def test_autosaved_image_path_reused(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # auto_save ON → the draft arrives with the autosaved path and
        # no second file is written at log time.
        window = _make_window(qtbot, monkeypatch, tmp_path, auto_save=True)
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        dlg = window._capture_context[0]
        images = list((tmp_path / "images").glob("*.png"))
        assert len(images) == 1
        dlg._callsign.setText("N0XYZ")
        dlg.accept()
        rows = window._logbook_coordinator.store.list_qsos()
        assert rows[0].image_path == images[0]
        assert len(list((tmp_path / "images").glob("*.png"))) == 1

    def test_escape_writes_nothing(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        dlg = window._capture_context[0]
        dlg.reject()
        # Store was never even opened — no logbook.db on disk.
        assert not (tmp_path / "logbook.db").exists()
        assert window._capture_context is None
        # No stray image written either.
        assert not (tmp_path / "images").exists()

    def test_busy_dialog_writes_silent_draft(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        first_dlg = window._capture_context[0]
        # Second completion while the first dialog is still open: no
        # dialog stack — the contact lands as a draft row instead.
        window._on_rx_image_complete(_rx_image(), Mode.PD_120, 95)
        assert window._capture_context[0] is first_dlg
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == ""
        assert rows[0].mode == "PD 120"
        first_dlg.reject()


class TestAutoLog:
    def test_auto_log_skips_dialog(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path, auto_log_qsos=True)
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is None
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == ""
        assert rows[0].direction == "RX"


class TestTxCapture:
    def test_tx_complete_prefills_from_qso_state(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._last_rig_freq_hz = 7_171_000
        qso_bar = window._tx_panel._qso_widget
        qso_bar._tocall.setText("K1ABC")
        qso_bar._rst.setCurrentText("575")
        qso_bar._name.setText("Sam")
        qso_bar._note.setText("portable on a hill")
        window._on_tx_image_prepared(_rx_image(), Mode.SCOTTIE_S1)
        window._on_tx_complete()
        assert window._capture_context is not None
        dlg = window._capture_context[0]
        q = dlg._qso
        assert q.direction == "TX"
        assert q.callsign == "K1ABC"
        assert q.mode == "Scottie 1"
        assert q.frequency_hz == 7_171_000
        assert q.rsv_sent == "575"
        assert q.name == "Sam"
        assert q.comment == "portable on a hill"
        dlg.accept()
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == "K1ABC"
        assert rows[0].direction == "TX"

    def test_test_tone_never_captures(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._last_tx_was_test_tone = True
        window._on_tx_complete()
        assert window._capture_context is None
        assert not (tmp_path / "logbook.db").exists()
