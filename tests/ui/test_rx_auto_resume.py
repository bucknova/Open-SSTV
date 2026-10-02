# SPDX-License-Identifier: GPL-3.0-or-later
"""RX capture resumes by itself after the input device is lost.

Before the 2026-10 stability audit, a device loss (a USB audio glitch, a
re-plug) stopped capture for good until someone clicked Start.  On a
station left receiving overnight, or run from the remote web page, a
one-second glitch ended reception without anyone noticing.

These tests drive the real slots in the order the audio worker emits them:
stream_error -> stopped, then each retry's started (success) or
error + stopped (failure).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.radio.base import ManualRig
from open_sstv.ui import main_window as mw
from open_sstv.ui.main_window import MainWindow

LOST = "Audio device disconnected."


@pytest.fixture
def window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MainWindow]:
    monkeypatch.setattr("open_sstv.ui.workers.encode", MagicMock(return_value=np.zeros(100, dtype=np.int16)))
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", MagicMock())
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    yield w


@pytest.fixture
def starts(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record capture-start attempts instead of opening a real stream."""
    calls: list[int] = []
    monkeypatch.setattr(window, "_begin_capture", lambda: calls.append(1))
    return calls


def _lose_device(window: MainWindow) -> None:
    window._capture_running = True
    window._on_audio_device_lost(LOST)
    window._on_rx_stopped()


def test_device_loss_schedules_a_restart(window: MainWindow, starts: list[int]) -> None:
    _lose_device(window)
    assert window._audio_recovery_timer.isActive()
    assert window._audio_recovery_timer.interval() == mw._AUDIO_RECOVERY_DELAYS_S[0] * 1000
    assert "Retrying in" in window.statusBar().currentMessage()
    assert LOST in window.statusBar().currentMessage(), "the cause must stay visible"

    window._attempt_audio_recovery()
    assert starts == [1], "the retry must actually try to start capture"
    assert window._input_device_needs_relookup, "a re-plug renumbers devices"


def test_success_ends_recovery(window: MainWindow, starts: list[int]) -> None:
    _lose_device(window)
    window._attempt_audio_recovery()
    window._on_rx_started()
    assert window._audio_recovery_active is False
    assert not window._audio_recovery_timer.isActive()
    assert "recovered" in window.statusBar().currentMessage()


def test_failures_back_off_then_keep_retrying(window: MainWindow, starts: list[int]) -> None:
    _lose_device(window)
    seen = []
    for _ in range(len(mw._AUDIO_RECOVERY_DELAYS_S) + 3):
        seen.append(window._audio_recovery_timer.interval() // 1000)
        window._attempt_audio_recovery()
        window._on_rx_audio_error("Could not open input stream")  # failed attempt
        window._on_rx_stopped()
    expected = list(mw._AUDIO_RECOVERY_DELAYS_S) + [mw._AUDIO_RECOVERY_DELAYS_S[-1]] * 3
    assert seen == expected
    assert window._audio_recovery_timer.isActive(), "must keep trying indefinitely"


@pytest.mark.parametrize("start", [True, False], ids=["user_start", "user_stop"])
def test_the_users_own_start_or_stop_takes_over(
    window: MainWindow, starts: list[int], start: bool
) -> None:
    _lose_device(window)
    window._on_capture_requested(start)
    assert window._audio_recovery_active is False
    assert not window._audio_recovery_timer.isActive()


def test_an_attempt_waits_out_a_transmission(
    window: MainWindow, starts: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _lose_device(window)
    monkeypatch.setattr(window._tx_worker, "wait_for_idle", lambda timeout: False)
    window._attempt_audio_recovery()
    assert starts == [], "starting capture would lift the RX gate mid-TX"
    assert window._audio_recovery_timer.isActive()


def test_no_recovery_during_shutdown(window: MainWindow, starts: list[int]) -> None:
    window._closing = True
    _lose_device(window)
    assert window._audio_recovery_active is False
    assert not window._audio_recovery_timer.isActive()


def test_a_normal_stop_does_not_trigger_recovery(window: MainWindow, starts: list[int]) -> None:
    window._capture_running = True
    window._on_rx_stopped()  # the user's Stop, no device loss
    assert not window._audio_recovery_timer.isActive()
