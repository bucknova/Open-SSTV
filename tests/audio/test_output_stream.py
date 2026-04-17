# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.audio.output_stream``.

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

from open_sstv.audio import output_stream
from open_sstv.audio.devices import AudioDevice


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
        patch("open_sstv.audio.output_stream.sd.play") as mock_play,
        patch("open_sstv.audio.output_stream.sd.wait") as mock_wait,
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
        patch("open_sstv.audio.output_stream.sd.play") as mock_play,
        patch("open_sstv.audio.output_stream.sd.wait"),
    ):
        output_stream.play_blocking(samples, 48000, device=3)
    assert mock_play.call_args.kwargs["device"] == 3


def test_play_blocking_accepts_none_device() -> None:
    samples = np.zeros(100, dtype=np.int16)
    with (
        patch("open_sstv.audio.output_stream.sd.play") as mock_play,
        patch("open_sstv.audio.output_stream.sd.wait"),
    ):
        output_stream.play_blocking(samples, 48000)
    assert mock_play.call_args.kwargs["device"] is None


def test_stop_calls_sd_stop() -> None:
    with patch("open_sstv.audio.output_stream.sd.stop") as mock_stop:
        output_stream.stop()
    mock_stop.assert_called_once()


# --- Live gain (test-tone ALC calibration) ---

class _FakeStream:
    """Minimal sd.OutputStream stand-in that records every chunk it was
    handed. Supports the context-manager protocol so ``with
    sd.OutputStream(...)`` works under patch.
    """

    def __init__(self) -> None:
        self.writes: list[np.ndarray] = []

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, chunk: np.ndarray) -> None:
        # Store a copy — play_blocking hands us a view into the parent
        # buffer and will be GC'd before the assertions run.
        self.writes.append(np.asarray(chunk).copy())


def test_play_blocking_applies_live_gain_per_chunk() -> None:
    """Regression: the test-tone TX gain slider used to only affect the
    next tone because ``transmit_test_tone`` pre-scaled the whole buffer.
    ``gain_provider`` is re-read for each ~0.1 s chunk so slider drags
    are audible in <100 ms. This test fakes a 4-chunk playback and
    verifies each chunk is scaled by the *then-current* provider value.
    """
    sr = 48000
    # 0.4 s of full-scale DC so scaling is easy to check. Four 0.1 s
    # chunks at 48 kHz → 4 chunks of 4800 samples.
    samples = np.full(sr // 10 * 4, 10_000, dtype=np.int16)

    # Provider returns 0.5, 1.0, 1.5, 2.0 in order.
    gains = iter([0.5, 1.0, 1.5, 2.0])

    fake_stream = _FakeStream()
    with (
        patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream),
    ):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,  # force chunked path
            gain_provider=lambda: next(gains),
        )

    assert len(fake_stream.writes) == 4
    # Each chunk should be scaled by the gain at its iteration.
    # Writes reshape to (-1, 1) so compare the first column.
    peak_by_chunk = [int(np.abs(w).max()) for w in fake_stream.writes]
    assert peak_by_chunk == [5000, 10000, 15000, 20000]


def test_play_blocking_gain_provider_clips_int16_overflow() -> None:
    """With a sample near int16 max and gain > 1, scaled output must
    clip to the dtype's range instead of wrapping negative.
    """
    sr = 48000
    samples = np.full(sr // 10, 30_000, dtype=np.int16)  # one chunk

    fake_stream = _FakeStream()
    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            gain_provider=lambda: 2.0,  # would overflow to 60_000 without clip
        )

    assert len(fake_stream.writes) == 1
    assert fake_stream.writes[0].max() == np.iinfo(np.int16).max  # 32767
    # And crucially, no wrap-around to negative.
    assert fake_stream.writes[0].min() >= 0


def test_play_blocking_gain_provider_unity_is_passthrough() -> None:
    """When the provider returns 1.0 the chunk should be written
    unmodified (no allocation, no clip). We assert array identity via
    data equality rather than ``is`` because play_blocking slices the
    parent buffer either way.
    """
    sr = 48000
    samples = np.full(sr // 10, 12345, dtype=np.int16)

    fake_stream = _FakeStream()
    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            gain_provider=lambda: 1.0,
        )

    assert len(fake_stream.writes) == 1
    np.testing.assert_array_equal(fake_stream.writes[0].ravel(), samples)
