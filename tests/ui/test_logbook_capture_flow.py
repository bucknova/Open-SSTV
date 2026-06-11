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

    def test_busy_dialog_drafts_partner_image_when_engaged(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # Engaged (ToCall filled): a partner's back-to-back image while
        # the first dialog is still open must not be lost — it lands as
        # a silent draft instead of stacking a second modal.
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._tx_panel._qso_widget._tocall.setText("K1ABC")
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        first_dlg = window._capture_context[0]
        window._on_rx_image_complete(_rx_image(), Mode.PD_120, 95)
        assert window._capture_context[0] is first_dlg
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == ""
        assert rows[0].mode == "PD 120"
        first_dlg.reject()

    def test_busy_dialog_drops_third_party_traffic(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # NOT engaged (empty ToCall): a monitoring station's logbook
        # must not fill with strangers' exchanges — the second decode
        # stays in the gallery only.
        window = _make_window(qtbot, monkeypatch, tmp_path)
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        first_dlg = window._capture_context[0]
        window._on_rx_image_complete(_rx_image(), Mode.PD_120, 95)
        assert window._capture_context[0] is first_dlg
        # Store never opened — no rows, no db file.
        assert not (tmp_path / "logbook.db").exists()
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


class TestLogbookButton:
    def test_qso_bar_button_opens_logbook_window(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path)
        assert window._logbook_dialog is None
        window._tx_panel._qso_widget._logbook_btn.click()
        assert window._logbook_dialog is not None
        assert window._logbook_dialog.isVisible()


class TestRxCapturePrompt:
    """v0.4: rx_capture_prompt gates the RX dialog on engagement —
    calling frequencies are party lines, most decodes aren't yours."""

    def test_in_qso_not_engaged_skips_silently(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="in_qso")
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is None
        assert not (tmp_path / "logbook.db").exists()

    def test_in_qso_engaged_prompts(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="in_qso")
        window._tx_panel._qso_widget._tocall.setText("K1ABC")
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is not None
        window._capture_context[0].reject()

    def test_never_skips_even_when_engaged(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="never")
        window._tx_panel._qso_widget._tocall.setText("K1ABC")
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is None
        assert not (tmp_path / "logbook.db").exists()

    def test_auto_log_overrides_never(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # auto_log_qsos is the explicit hoover-everything mode; the
        # prompt setting only governs the dialog.
        window = _make_window(
            qtbot, monkeypatch, tmp_path,
            rx_capture_prompt="never", auto_log_qsos=True,
        )
        window._on_rx_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        assert window._capture_context is None
        assert window._logbook_coordinator.store.count() == 1

    def test_tx_still_prompts_under_never(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # The gate is RX-only — your own transmissions are always yours.
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="never")
        window._on_tx_image_prepared(_rx_image(), Mode.SCOTTIE_S1)
        window._on_tx_complete()
        assert window._capture_context is not None
        window._capture_context[0].reject()


class TestGalleryLogQso:
    def test_gallery_request_opens_prefilled_dialog(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="never")
        window._last_rig_freq_hz = 14_230_000
        window._on_gallery_log_qso(_rx_image(), Mode.PD_120)
        assert window._capture_context is not None
        dlg = window._capture_context[0]
        q = dlg._qso
        assert q.direction == "RX"
        assert q.mode == "PD 120"
        assert q.frequency_hz == 14_230_000
        dlg._callsign.setText("K1ABC")
        dlg.accept()
        rows = window._logbook_coordinator.store.list_qsos()
        assert len(rows) == 1
        assert rows[0].callsign == "K1ABC"
        assert rows[0].image_path is not None and rows[0].image_path.is_file()

    def test_gallery_bypasses_auto_log(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # An explicit click means "give me the form", not a silent draft.
        window = _make_window(qtbot, monkeypatch, tmp_path, auto_log_qsos=True)
        window._on_gallery_log_qso(_rx_image(), Mode.MARTIN_M1)
        assert window._capture_context is not None
        window._capture_context[0].reject()
        assert window._logbook_coordinator.store.count() == 0

    def test_relay_chain_from_gallery_widget(
        self, qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patched_audio
    ) -> None:
        # Full wiring: gallery dispatch → RxPanel relay → MainWindow dialog.
        # The gallery is populated by show_image_complete (the panel
        # slot wired to RxWorker.image_complete), not by the capture
        # handler — mirror that here.
        window = _make_window(qtbot, monkeypatch, tmp_path, rx_capture_prompt="never")
        window._rx_panel.show_image_complete(_rx_image(), Mode.MARTIN_M1, 44)
        gallery = window._rx_panel._gallery
        assert gallery.count() == 1
        item = gallery._model.item(0)
        gallery._dispatch_context_action(item, "Log QSO…")
        assert window._capture_context is not None
        assert window._capture_context[0]._qso.mode == "Martin M1"
        window._capture_context[0].reject()
