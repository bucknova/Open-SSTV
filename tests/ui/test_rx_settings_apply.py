# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings OK must not destroy an in-progress RX decode (v0.6.14 audit UI-M2).

Before the fix, every Settings OK re-sent weak-signal, incremental-decode and
sample rate to the RxWorker, and each one rebuilt the Decoder unconditionally.
An unrelated change (callsign, say) therefore threw away the image being
received.  Worse, ``_decoding`` and the watchdog state survived the rebuild,
so a few seconds later the watchdog emitted the stale partial as a
"completed" image, triggering auto-save and the logbook prompt.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.decoder import ImageProgress, ImageStarted
from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig
from open_sstv.ui.workers import _RX_WATCHDOG_LINE_FLOOR_S, RxWorker

pytestmark = pytest.mark.gui


def _start_decoding(worker: RxWorker) -> list:
    """Drive the worker into DECODING with one progress line; return the
    image_complete log."""
    prog = Image.new("RGB", (320, 240))
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = [
        ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
        ImageProgress(
            image=prog, mode=Mode.ROBOT_36, vis_code=8,
            lines_decoded=50, lines_total=240,
        ),
    ]
    completes: list = []
    worker.image_complete.connect(lambda *a: completes.append(a))
    worker.feed_chunk(np.zeros(10, dtype=np.float32))
    assert worker._decoding is True
    return completes


@pytest.mark.parametrize(
    ("setter", "value"),
    [
        ("set_weak_signal", False),
        ("set_incremental_decode", True),
        ("set_sample_rate", 48_000),
    ],
)
def test_unchanged_value_keeps_the_decoder(qapp, setter: str, value: object) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    _start_decoding(worker)
    decoder = worker._decoder
    getattr(worker, setter)(value)
    assert worker._decoder is decoder, f"{setter}({value}) must not rebuild"
    assert worker._decoding is True, "the in-progress decode must survive"


@pytest.mark.parametrize(
    ("setter", "value"),
    [
        ("set_weak_signal", True),
        ("set_incremental_decode", False),
        ("set_sample_rate", 44_100),
    ],
)
def test_rebuild_resets_decode_and_watchdog_state(
    qapp, setter: str, value: object
) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    completes = _start_decoding(worker)
    getattr(worker, setter)(value)
    assert worker._decoding is False
    assert worker._decoding_mode is None
    assert worker._last_progress_image is None
    # The watchdog must not later emit the discarded partial as "complete".
    worker._last_progress_time = time.monotonic() - (_RX_WATCHDOG_LINE_FLOOR_S + 10)
    worker._on_watchdog_tick()
    assert completes == []


def test_sample_rate_change_recomputes_flush_thresholds(qapp) -> None:
    worker = RxWorker(sample_rate=48_000)
    worker.set_sample_rate(44_100)
    other = RxWorker(sample_rate=44_100)
    assert worker._flush_samples_incremental_idle == other._flush_samples_incremental_idle
    assert (
        worker._flush_samples_incremental_decoding
        == other._flush_samples_incremental_decoding
    )
    assert worker._flush_samples_batch == other._flush_samples_batch
    assert worker._current_flush_samples() == other._current_flush_samples()


# --- MainWindow side -------------------------------------------------------


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


def _record(signal) -> list:
    out: list = []
    signal.connect(lambda *a: out.append(a))
    return out


def test_apply_config_emits_rx_changes_only_when_changed(window) -> None:
    weak = _record(window._rx_weak_signal_changed)
    inc = _record(window._rx_incremental_decode_changed)
    rate = _record(window._rx_sample_rate_changed)

    window._apply_config()  # nothing changed
    assert (weak, inc, rate) == ([], [], [])

    window._config.rx_weak_signal_mode = not window._config.rx_weak_signal_mode
    window._apply_config()
    assert len(weak) == 1 and inc == [] and rate == []


def test_sample_rate_change_is_deferred_while_capturing(window) -> None:
    rate = _record(window._rx_sample_rate_changed)
    new_rate = 44_100 if window._config.sample_rate != 44_100 else 48_000
    window._capture_running = True
    window._config.sample_rate = new_rate
    window._apply_config()
    assert rate == [], "the running stream is still at the old rate"

    # Next capture start brings the decoder in line, before the reset.
    order: list[str] = []
    window._rx_sample_rate_changed.connect(lambda r: order.append(f"rate {r}"))
    window._request_rx_reset.connect(lambda: order.append("reset"))
    window._capture_running = False
    window._begin_capture()
    assert order[:2] == [f"rate {new_rate}", "reset"]


def test_sample_rate_change_applies_at_once_when_idle(window) -> None:
    rate = _record(window._rx_sample_rate_changed)
    new_rate = 44_100 if window._config.sample_rate != 44_100 else 48_000
    window._config.sample_rate = new_rate
    window._apply_config()
    assert rate == [(new_rate,)]


def test_waterfall_follows_sample_rate_change(window) -> None:
    window._on_toggle_waterfall(True)
    new_rate = 44_100 if window._config.sample_rate != 44_100 else 48_000
    window._config.sample_rate = new_rate
    window._apply_config()
    assert window._waterfall_window._widget._sample_rate == new_rate
    window._on_toggle_waterfall(False)
