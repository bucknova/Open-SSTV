# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest-qt smoke tests for ``sstv_app.ui.main_window.MainWindow``.

These verify the window can be constructed, the worker thread starts,
and the basic signal wiring fires through to the panel — without
actually playing audio. ``encode`` and ``play_blocking`` are patched out
in conftest-style fixtures because the worker would otherwise try to
encode a real image and open a real audio device.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from sstv_app.radio.base import ManualRig
from sstv_app.ui.main_window import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, MagicMock]]:
    encode_mock = MagicMock(return_value=np.zeros(100, dtype=np.int16))
    play_mock = MagicMock()
    stop_mock = MagicMock()
    monkeypatch.setattr("sstv_app.ui.workers.encode", encode_mock)
    monkeypatch.setattr("sstv_app.ui.workers.output_stream.play_blocking", play_mock)
    monkeypatch.setattr("sstv_app.ui.workers.output_stream.stop", stop_mock)
    yield {"encode": encode_mock, "play": play_mock, "stop": stop_mock}


@pytest.fixture
def window(qtbot, patched_audio: dict[str, MagicMock]) -> MainWindow:
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)  # ensures pytest-qt cleans up the window on test exit
    return w


@pytest.fixture
def gradient_path(tmp_path: Path) -> Path:
    img = Image.new("RGB", (100, 100), color=(20, 40, 60))
    p = tmp_path / "img.png"
    img.save(p)
    return p


def test_window_constructs_and_shows(window: MainWindow, qtbot) -> None:
    window.show()
    qtbot.waitExposed(window)
    assert window.windowTitle() == "Open SSTV"
    assert window._tx_thread.isRunning()


def test_central_widget_is_tx_panel(window: MainWindow) -> None:
    from sstv_app.ui.tx_panel import TxPanel

    assert isinstance(window.centralWidget(), TxPanel)


def test_transmit_round_trip_through_worker(
    qtbot,
    window: MainWindow,
    gradient_path: Path,
    patched_audio: dict[str, MagicMock],
) -> None:
    """Load an image, click Transmit, and wait for the worker's
    transmission_complete signal to come back to the main thread."""
    window._tx_panel.load_image(gradient_path)

    # Speed the PTT delay down to zero so the test doesn't sit there
    # waiting 200 ms for nothing.
    window._tx_worker._ptt_delay_s = 0

    with qtbot.waitSignal(
        window._tx_worker.transmission_complete, timeout=2000
    ):
        window._tx_panel._transmit_btn.click()

    patched_audio["play"].assert_called_once()
    # ``transmission_complete`` is emitted from the worker thread and
    # delivered to the panel via a queued connection, so the button
    # state update is one event-loop spin behind the signal. Wait for
    # the panel to actually re-enable rather than racing on it.
    qtbot.waitUntil(
        lambda: window._tx_panel._transmit_btn.isEnabled(), timeout=1000
    )
    assert not window._tx_panel._stop_btn.isEnabled()


def test_stop_button_calls_request_stop(
    qtbot,
    window: MainWindow,
    patched_audio: dict[str, MagicMock],
) -> None:
    """Stop is a direct method call, so we don't get a queued signal —
    we just verify the panel's stop_requested wire reaches the worker
    and the underlying sounddevice.stop is called."""
    # Force the panel into "transmitting" state so the Stop button is enabled.
    window._tx_panel.set_transmitting(True)
    window._tx_panel._stop_btn.click()
    patched_audio["stop"].assert_called()
