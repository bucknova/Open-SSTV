# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.ui.workers.RxWorker``.

Covers the batching / flushing logic plus DecoderEvent → Qt signal
translation. Uses a real ``Decoder`` with a tiny flush interval so
the tests run in milliseconds rather than driving a full 36 s audio
buffer through PySSTV.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.decoder import (
    DecodeError,
    ImageComplete,
    ImageStarted,
)
from open_sstv.core.modes import Mode
from open_sstv.ui.workers import RxWorker

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
    from open_sstv.core.encoder import encode
    from open_sstv.core.modes import MODE_TABLE

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


# === final slant-correction toggle (v0.1.18) ===


def _make_complete_event(mode: Mode = Mode.MARTIN_M1, vis_code: int = 44) -> tuple:
    """Return (ImageComplete event, fake progressive PIL image)."""
    prog_image = Image.new("RGB", (320, 256), color=(10, 20, 30))
    return ImageComplete(image=prog_image, mode=mode, vis_code=vis_code), prog_image


def _setup_worker_for_dispatch(flag: bool) -> tuple:
    """Create an RxWorker with mocked decoder ready to dispatch one ImageComplete.

    Returns (worker, log, raw_audio_array).
    """
    worker = RxWorker(sample_rate=48_000, flush_samples=1, final_slant_correction=flag)
    event, prog_image = _make_complete_event()
    raw_audio = np.zeros(100, dtype=np.float64)

    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = [
        ImageStarted(mode=Mode.MARTIN_M1, vis_code=44),
        event,
    ]
    worker._decoder.consume_last_buffer.return_value = raw_audio

    log = _record_signals(worker)
    return worker, log, raw_audio, prog_image


def test_final_slant_off_does_not_call_decode_wav(qapp) -> None:
    """With apply_final_slant_correction=False, decode_wav must never be called.

    The progressive image from the ImageComplete event must be emitted as-is.
    """
    worker, log, raw_audio, prog_image = _setup_worker_for_dispatch(flag=False)

    with patch("open_sstv.ui.workers.decode_wav") as mock_decode_wav:
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

    mock_decode_wav.assert_not_called()
    # consume_last_buffer IS called regardless (memory management)
    worker._decoder.consume_last_buffer.assert_called_once()
    # Progressive image is emitted unchanged
    assert len(log["image_complete"]) == 1
    img, mode, code = log["image_complete"][0]
    assert img is prog_image
    assert mode == Mode.MARTIN_M1


def test_final_slant_on_calls_decode_wav_and_uses_result(qapp) -> None:
    """With apply_final_slant_correction=True, decode_wav is called and its
    result replaces the progressive image when the mode matches.
    """
    worker, log, raw_audio, prog_image = _setup_worker_for_dispatch(flag=True)

    final_image = Image.new("RGB", (320, 256), color=(200, 100, 50))

    decode_result = MagicMock()
    decode_result.mode = Mode.MARTIN_M1
    decode_result.image = final_image

    with patch("open_sstv.ui.workers.decode_wav", return_value=decode_result) as mock_dw:
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

    mock_dw.assert_called_once_with(raw_audio, 48_000)
    worker._decoder.consume_last_buffer.assert_called_once()
    # Re-decoded image replaces the progressive one
    assert len(log["image_complete"]) == 1
    img, mode, code = log["image_complete"][0]
    assert img is final_image
    assert mode == Mode.MARTIN_M1


def test_final_slant_on_mode_mismatch_falls_back_to_progressive(qapp) -> None:
    """If decode_wav returns a result with a different mode, the progressive
    image is kept rather than emitting a wrong-mode image.
    """
    worker, log, raw_audio, prog_image = _setup_worker_for_dispatch(flag=True)

    decode_result = MagicMock()
    decode_result.mode = Mode.ROBOT_36  # different from MARTIN_M1 in the event
    decode_result.image = Image.new("RGB", (320, 240))

    with patch("open_sstv.ui.workers.decode_wav", return_value=decode_result):
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert len(log["image_complete"]) == 1
    img, _mode, _code = log["image_complete"][0]
    assert img is prog_image  # progressive preserved


def test_final_slant_on_decode_wav_exception_falls_back(qapp) -> None:
    """If decode_wav raises, the progressive image is used and no error
    signal is emitted (the exception is logged at DEBUG, not surfaced to UI).
    """
    worker, log, raw_audio, prog_image = _setup_worker_for_dispatch(flag=True)

    with patch("open_sstv.ui.workers.decode_wav", side_effect=RuntimeError("oops")):
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

    assert log["error"] == []  # no UI error
    assert len(log["image_complete"]) == 1
    img, _mode, _code = log["image_complete"][0]
    assert img is prog_image


def test_set_final_slant_correction_toggles_behavior(qapp) -> None:
    """set_final_slant_correction() changes behavior mid-session without
    needing to reconstruct the worker.
    """
    worker = RxWorker(sample_rate=48_000, flush_samples=1, final_slant_correction=False)
    assert worker._final_slant_correction is False
    worker.set_final_slant_correction(True)
    assert worker._final_slant_correction is True
    worker.set_final_slant_correction(False)
    assert worker._final_slant_correction is False
