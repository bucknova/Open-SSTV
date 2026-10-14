# SPDX-License-Identifier: GPL-3.0-or-later
"""Swap and quit must not wait out the 2 s budget on an idle audio worker.

v0.6.14 audit UI-M6: ``_swap_audio_worker`` and ``closeEvent`` waited for the
worker's ``stopped`` signal, which ``stop()`` emits only when a stream was
running.  With capture idle, each sat out the full 2 s timeout and then
logged a false "wedged worker" warning.  They now wait for
``stop_finished``, which every ``stop()`` call emits.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from open_sstv.audio.input_stream import InputStreamWorker
from open_sstv.config.schema import AppConfig
from open_sstv.radio.base import ManualRig

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    from open_sstv.ui.main_window import MainWindow

    monkeypatch.setattr(
        "open_sstv.ui.workers.encode",
        MagicMock(return_value=np.zeros(100, dtype=np.int16)),
    )
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", MagicMock())
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    monkeypatch.setattr("open_sstv.ui.main_window.save_config", MagicMock())
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    yield w


def test_swap_with_idle_capture_does_not_wait(window, caplog) -> None:
    caplog.set_level(logging.WARNING)
    t0 = time.monotonic()
    window._swap_audio_worker(InputStreamWorker())
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"idle swap took {elapsed:.2f} s"
    assert "did not complete" not in caplog.text


def test_close_with_idle_capture_does_not_warn(window, caplog) -> None:
    caplog.set_level(logging.WARNING)
    t0 = time.monotonic()
    window.close()
    elapsed = time.monotonic() - t0
    assert "did not complete" not in caplog.text
    assert elapsed < 2.0, f"idle close took {elapsed:.2f} s"
