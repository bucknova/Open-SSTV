# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.ui.workers.RxWorker``.

Covers the batching / flushing logic plus DecoderEvent → Qt signal
translation. Uses a real ``Decoder`` with a tiny flush interval so
the tests run in milliseconds rather than driving a full 36 s audio
buffer through PySSTV.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from sstv_app.core.decoder import (
    DecodeError,
    ImageComplete,
    ImageStarted,
)
from sstv_app.core.modes import Mode
from sstv_app.ui.workers import RxWorker

pytestmark = pytest.mark.gui


def _record_signals(worker: RxWorker) -> dict[str, list]:
    log: dict[str, list] = {
        "image_started": [],
        "image_complete": [],
        "error": [],
    }
    worker.image_started.connect(
        lambda mode, code: log["image_started"].append((mode, code))
    )
    worker.image_complete.connect(
        lambda img, mode, code: log["image_complete"].append((img, mode, code))
    )
    worker.error.connect(lambda msg: log["error"].append(msg))
    return log


# === batching ===


def test_feed_chunk_buffers_until_flush_threshold(qapp) -> None:
    """Below the flush threshold, feed_chunk should NOT call Decoder.feed."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1000)
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = []

    worker.feed_chunk(np.zeros(400, dtype=np.float32))
    worker.feed_chunk(np.zeros(400, dtype=np.float32))

    worker._decoder.feed.assert_not_called()
    assert worker._scratch_samples == 800


def test_feed_chunk_flushes_at_threshold(qapp) -> None:
    """Crossing the flush threshold should trigger exactly one
    Decoder.feed call with the concatenated scratch buffer."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1000)
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = []

    worker.feed_chunk(np.arange(400, dtype=np.float32))
    worker.feed_chunk(np.arange(400, dtype=np.float32) + 400)
    worker.feed_chunk(np.arange(400, dtype=np.float32) + 800)

    worker._decoder.feed.assert_called_once()
    fed = worker._decoder.feed.call_args.args[0]
    assert fed.size == 1200
    # Check the concatenation order — scratch MUST preserve temporal
    # order or the decode will be garbage.
    np.testing.assert_array_equal(
        fed, np.arange(1200, dtype=np.float64)
    )
    # Scratch should be empty after flush.
    assert worker._scratch_samples == 0


def test_explicit_flush_forces_decoder_feed(qapp) -> None:
    """``flush()`` on a partially filled scratch buffer should still
    push it to the decoder (used when capture stops mid-batch)."""
    worker = RxWorker(sample_rate=48_000, flush_samples=100_000)
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = []

    worker.feed_chunk(np.ones(500, dtype=np.float32))
    worker.flush()

    worker._decoder.feed.assert_called_once()
    assert worker._scratch_samples == 0


def test_flush_noop_when_scratch_is_empty(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1000)
    worker._decoder = MagicMock()

    worker.flush()

    worker._decoder.feed.assert_not_called()


def test_reset_clears_scratch_and_decoder(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=100_000)
    worker._decoder = MagicMock()

    worker.feed_chunk(np.ones(500, dtype=np.float32))
    worker.reset()

    assert worker._scratch_samples == 0
    assert worker._scratch == []
    worker._decoder.reset.assert_called_once()


# === event dispatch ===


def test_image_started_event_becomes_qt_signal(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = [
        ImageStarted(mode=Mode.ROBOT_36, vis_code=8)
    ]
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert log["image_started"] == [(Mode.ROBOT_36, 8)]
    assert log["image_complete"] == []
    assert log["error"] == []


def test_image_complete_event_becomes_qt_signal(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    worker._decoder = MagicMock()
    fake_image = Image.new("RGB", (320, 240), color=(10, 20, 30))
    worker._decoder.feed.return_value = [
        ImageStarted(mode=Mode.MARTIN_M1, vis_code=44),
        ImageComplete(image=fake_image, mode=Mode.MARTIN_M1, vis_code=44),
    ]
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert log["image_started"] == [(Mode.MARTIN_M1, 44)]
    assert len(log["image_complete"]) == 1
    img, mode, code = log["image_complete"][0]
    assert img is fake_image
    assert mode == Mode.MARTIN_M1
    assert code == 44


def test_decode_error_event_becomes_error_signal(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = [
        DecodeError(message="unsupported mode 99")
    ]
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert log["error"] == ["unsupported mode 99"]
    assert log["image_complete"] == []


def test_decoder_exception_surfaces_as_error_signal(qapp) -> None:
    """If Decoder.feed raises (shouldn't happen, but defence in depth),
    the worker emits ``error`` and keeps running."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    worker._decoder = MagicMock()
    worker._decoder.feed.side_effect = RuntimeError("numpy broke")
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert log["error"] and "numpy broke" in log["error"][0]
    # Scratch is still cleared so the next flush doesn't re-run over
    # the same bad chunk.
    assert worker._scratch_samples == 0


# === input validation ===


def test_rejects_2d_chunk_with_error_signal(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros((10, 2), dtype=np.float32))

    assert log["error"] and "1-D" in log["error"][0]
    assert worker._scratch_samples == 0


def test_empty_chunk_is_noop(qapp) -> None:
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    worker._decoder = MagicMock()
    log = _record_signals(worker)

    worker.feed_chunk(np.zeros(0, dtype=np.float32))

    worker._decoder.feed.assert_not_called()
    assert log["error"] == []


# === end-to-end with real Decoder ===


def test_end_to_end_with_real_encoded_image(qapp) -> None:
    """Feed a real Robot 36 encoded buffer in chunks and assert the
    worker emits ImageComplete. This is the sanity check that the
    batching plumbing actually produces a decode."""
    from sstv_app.core.encoder import encode
    from sstv_app.core.modes import MODE_TABLE

    fs = 48_000
    # 32x24 gradient (tiny, still decodes — encoder resizes up).
    ramp = np.linspace(0, 255, 32 * 24 * 3, dtype=np.uint8).reshape((24, 32, 3))
    original = Image.fromarray(ramp)
    samples = encode(original, Mode.ROBOT_36, sample_rate=fs)
    # int16 → float32 in [-1, 1]
    audio = samples.astype(np.float32) / 32768.0

    worker = RxWorker(sample_rate=fs, flush_samples=fs)  # 1s flush
    log = _record_signals(worker)

    # Feed the entire image in 4096-sample chunks.
    chunk_size = 4096
    for start in range(0, audio.size, chunk_size):
        worker.feed_chunk(audio[start : start + chunk_size])
    # Final flush picks up the tail.
    worker.flush()

    assert len(log["image_complete"]) >= 1
    img, mode, code = log["image_complete"][0]
    assert mode == Mode.ROBOT_36
    assert code == MODE_TABLE[Mode.ROBOT_36].vis_code
