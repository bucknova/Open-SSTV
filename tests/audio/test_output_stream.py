# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.audio.output_stream``.

The happy path here would mean opening a real PortAudio output stream,
which is hardware-dependent and flaky in CI. Instead we cover only the
input-validation paths plus a mock-backed test that ``play_blocking``
forwards the right arguments to ``sounddevice.play``. Real-hardware
playback is exercised by hand during release smoke testing — see
``docs/release-checklist.md`` once it lands in Phase 3.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from sstv_app.audio import output_stream
from sstv_app.audio.devices import AudioDevice


def test_play_blocking_rejects_empty_buffer() -> None:
    with pytest.raises(ValueError, match="empty"):
        output_stream.play_blocking(np.array([], dtype=np.int16), 48000)


def test_play_blocking_rejects_2d_buffer() -> None:
    with pytest.raises(ValueError, match="1-D mono"):
        output_stream.play_blocking(np.zeros((100, 2), dtype=np.int16), 48000)


def test_play_blocking_passes_device_index_through() -> None:
    samples = np.zeros(100, dtype=np.int16)
    device = AudioDevice(
        index=7,
        name="Fake",
        host_api="Test API",
        max_input_channels=0,
        max_output_channels=2,
        default_sample_rate=48000.0,
    )

    with (
        patch("sstv_app.audio.output_stream.sd.play") as mock_play,
        patch("sstv_app.audio.output_stream.sd.wait") as mock_wait,
    ):
        output_stream.play_blocking(samples, 48000, device=device)

    mock_play.assert_called_once()
    _, kwargs = mock_play.call_args
    assert kwargs["device"] == 7
    assert kwargs["samplerate"] == 48000
    assert kwargs["blocking"] is True
    mock_wait.assert_called_once()


def test_play_blocking_accepts_raw_int_device() -> None:
    samples = np.zeros(100, dtype=np.int16)
    with (
        patch("sstv_app.audio.output_stream.sd.play") as mock_play,
        patch("sstv_app.audio.output_stream.sd.wait"),
    ):
        output_stream.play_blocking(samples, 48000, device=3)
    assert mock_play.call_args.kwargs["device"] == 3


def test_play_blocking_accepts_none_device() -> None:
    samples = np.zeros(100, dtype=np.int16)
    with (
        patch("sstv_app.audio.output_stream.sd.play") as mock_play,
        patch("sstv_app.audio.output_stream.sd.wait"),
    ):
        output_stream.play_blocking(samples, 48000)
    assert mock_play.call_args.kwargs["device"] is None


def test_stop_calls_sd_stop() -> None:
    with patch("sstv_app.audio.output_stream.sd.stop") as mock_stop:
        output_stream.stop()
    mock_stop.assert_called_once()
