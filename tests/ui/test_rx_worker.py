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
    ImageProgress,
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
        "status_update": [],
    }
    worker.image_started.connect(
        lambda mode, code: log["image_started"].append((mode, code))
    )
    worker.image_complete.connect(
        lambda img, mode, code: log["image_complete"].append((img, mode, code))
    )
    worker.error.connect(lambda msg: log["error"].append(msg))
    worker.status_update.connect(lambda msg: log["status_update"].append(msg))
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


def test_default_flush_interval_incremental_idle_is_1s(qapp) -> None:
    """v0.2.6: while IDLE (hunting for VIS), the incremental path keeps
    the pre-v0.2.6 1 s cadence.  Calling ``detect_vis`` 10× more often
    on noisy pre-transmission audio compounds the chance of a
    noise-triggered unknown-VIS false positive, which in turn trims
    the buffer past ``vis_end`` and can mutilate the real VIS arriving
    moments later — breaking acoustic (speaker→mic) captures.
    """
    worker = RxWorker(sample_rate=48_000, incremental_decode=True)
    # decoder starts in IDLE; the "current" threshold should be 1 s.
    assert worker._current_flush_samples() == 48_000


def test_flush_interval_incremental_shortens_to_100ms_when_decoding(qapp) -> None:
    """v0.2.6: once VIS has locked and we're painting lines, the flush
    cadence drops to 0.1 s so the UI shows rows as they complete
    (MMSSTV-style).  VIS detection has already succeeded by this
    point, so the IDLE false-positive risk no longer applies.
    """
    worker = RxWorker(sample_rate=48_000, incremental_decode=True)
    worker._decoding = True  # simulate post-ImageStarted state
    assert worker._current_flush_samples() == 4_800


def test_default_flush_interval_batch_is_2s(qapp) -> None:
    """v0.2.6: the batch path — still the opt-in fallback — flushes
    every 2 s instead of 1 s to amortise its O(N²) reprocessing cost
    on long Scottie-family receives. Responsiveness is now owned by
    the incremental path, so the v0.1.25 '2 s → 1 s for paint-as-you-go'
    revert no longer applies here.  The batch path does not dual-mode
    between IDLE and DECODING since its cost is dominated by the
    reprocess, not the detection step.
    """
    worker = RxWorker(sample_rate=48_000, incremental_decode=False)
    assert worker._current_flush_samples() == 96_000
    worker._decoding = True
    assert worker._current_flush_samples() == 96_000  # unchanged


def test_default_flush_interval_scales_with_sample_rate(qapp) -> None:
    """The interval is a number of seconds; sample-count derives from
    the constructor sample_rate so non-48 kHz callers still get the
    right cadence.  44.1 kHz × 1.0 s (IDLE) = 44 100 samples;
    44.1 kHz × 0.1 s (DECODING) = 4 410 samples.
    """
    worker = RxWorker(sample_rate=44_100, incremental_decode=True)
    assert worker._current_flush_samples() == 44_100
    worker._decoding = True
    assert worker._current_flush_samples() == 4_410


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


# === decoder rebuild tests (H-02) ===


def test_set_weak_signal_rebuilds_decoder(qapp) -> None:
    """set_weak_signal() must replace _decoder with a fresh Decoder that has
    weak_signal=True, preserving the current incremental_decode setting.

    This validates the @Slot(bool) rebuild path.  Threading correctness is
    the responsibility of the queued signal connection set up in MainWindow;
    here we verify the rebuild semantics are right.
    """
    worker = RxWorker(sample_rate=48_000, incremental_decode=True)
    old_decoder = worker._decoder

    worker.set_weak_signal(True)

    assert worker._decoder is not old_decoder, "decoder must be replaced"
    assert worker._decoder._weak_signal is True
    # incremental_decode setting is preserved across the rebuild
    assert worker._decoder._incremental_decode is True


def test_set_weak_signal_preserves_incremental_false(qapp) -> None:
    """set_weak_signal() preserves incremental_decode=False if that was set."""
    worker = RxWorker(sample_rate=48_000, incremental_decode=False)
    worker.set_weak_signal(True)
    assert worker._decoder._incremental_decode is False


def test_set_incremental_decode_rebuilds_decoder(qapp) -> None:
    """set_incremental_decode(False) must replace _decoder with a Decoder
    that has incremental_decode=False, preserving weak_signal.
    """
    worker = RxWorker(sample_rate=48_000, incremental_decode=True, weak_signal=False)
    old_decoder = worker._decoder

    worker.set_incremental_decode(False)

    assert worker._decoder is not old_decoder, "decoder must be replaced"
    assert worker._decoder._incremental_decode is False
    # weak_signal setting is preserved
    assert worker._decoder._weak_signal is False


def test_set_incremental_decode_round_trip(qapp) -> None:
    """Toggling incremental_decode on and off replaces decoder each time."""
    worker = RxWorker(sample_rate=48_000, incremental_decode=True)
    d0 = worker._decoder

    worker.set_incremental_decode(False)
    d1 = worker._decoder
    assert d1 is not d0
    assert d1._incremental_decode is False

    worker.set_incremental_decode(True)
    d2 = worker._decoder
    assert d2 is not d1
    assert d2._incremental_decode is True


# === Robot 36 slant-correction skip (H-03) ===


def test_final_slant_skips_robot36_keeps_progressive(qapp) -> None:
    """With final_slant_correction=True and mode=ROBOT_36, decode_wav must
    NOT be called — the incremental and batch Robot 36 paths use different
    color pipelines and mixing them would silently degrade image quality.
    """
    worker = RxWorker(sample_rate=48_000, flush_samples=1, final_slant_correction=True)
    prog_image = Image.new("RGB", (320, 240), color=(10, 20, 30))
    event = ImageComplete(image=prog_image, mode=Mode.ROBOT_36, vis_code=8)
    raw_audio = np.zeros(100, dtype=np.float64)

    worker._decoder = MagicMock()
    worker._decoder.feed.return_value = [
        ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
        event,
    ]
    worker._decoder.consume_last_buffer.return_value = raw_audio

    log = _record_signals(worker)

    with patch("open_sstv.ui.workers.decode_wav") as mock_dw:
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

    mock_dw.assert_not_called()
    assert len(log["image_complete"]) == 1
    img, mode, code = log["image_complete"][0]
    assert img is prog_image
    assert mode == Mode.ROBOT_36


# ---------------------------------------------------------------------------
# v0.1.36 — RX decoder watchdog
# ---------------------------------------------------------------------------


class TestRxWatchdog:
    """The per-transmission RX watchdog resets the decoder when a
    signal fades mid-image or a decode runs far past the mode's
    expected duration.  Before v0.1.36, a signal that dropped out
    after VIS detection left the decoder stuck in ``DECODING`` state
    forever — the user had to click Clear manually.  Now the
    watchdog trips after either (a) total elapsed > mode duration ×
    1.5 (floor 15 s) or (b) no new line for 5 × line period (floor
    5 s), emits the partial image to the gallery, and returns the
    decoder to IDLE.
    """

    def test_watchdog_trips_on_no_progress(self, qapp) -> None:
        """If no new ImageProgress arrives for ~5 × line period and
        the line-floor (5 s), the watchdog synthesises a complete
        event with the last partial image and resets the decoder."""
        import time

        from open_sstv.ui.workers import _RX_WATCHDOG_LINE_FLOOR_S

        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))

        # Prime the decoder so the worker thinks it's DECODING
        worker._decoder = MagicMock()
        worker._decoder.feed.side_effect = [
            [
                ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
                ImageProgress(
                    image=prog_image,
                    mode=Mode.ROBOT_36,
                    vis_code=8,
                    lines_decoded=120,
                    lines_total=240,
                ),
            ],
            [],  # subsequent flushes — no progress
        ]
        log = _record_signals(worker)

        # First flush: ImageStarted + ImageProgress → watchdog armed
        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        assert worker._decoding is True
        assert worker._last_progress_lines == 120

        # Backdate the progress timestamp past the no-progress budget
        # so the next flush trips the watchdog.
        worker._last_progress_time = time.monotonic() - (
            _RX_WATCHDOG_LINE_FLOOR_S + 10.0
        )

        # Second flush: no new events → watchdog should trip
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

        # Partial image surfaced to the gallery
        assert len(log["image_complete"]) == 1
        img, mode, vis = log["image_complete"][0]
        assert img is prog_image
        assert mode == Mode.ROBOT_36
        assert vis == 8

        # Watchdog state cleared, decoder reset
        assert worker._decoding is False
        assert worker._decoding_mode is None
        worker._decoder.reset.assert_called_once()

    def test_watchdog_trips_on_total_elapsed(self, qapp) -> None:
        """Catches the case where lines trickle in but the total
        transmission time has blown past the expected mode duration
        (fault tolerance against a decoder stuck in a bad sync grid
        that keeps sliding)."""
        import time

        from open_sstv.ui.workers import _RX_WATCHDOG_TOTAL_MULTIPLIER

        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))
        worker._decoder = MagicMock()
        worker._decoder.feed.side_effect = [
            [
                ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
                ImageProgress(
                    image=prog_image,
                    mode=Mode.ROBOT_36,
                    vis_code=8,
                    lines_decoded=50,
                    lines_total=240,
                ),
            ],
            [],
        ]
        log = _record_signals(worker)
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

        # Backdate the START timestamp past the total budget.  Keep
        # the last-progress time fresh so only the total-elapsed
        # branch trips.
        from open_sstv.core.modes import MODE_TABLE

        total_budget = MODE_TABLE[Mode.ROBOT_36].total_duration_s * _RX_WATCHDOG_TOTAL_MULTIPLIER
        worker._decoding_start_time = time.monotonic() - (total_budget + 10.0)
        worker._last_progress_time = time.monotonic()  # fresh

        worker.feed_chunk(np.zeros(10, dtype=np.float32))

        assert len(log["image_complete"]) == 1
        assert worker._decoding is False

    def test_watchdog_does_not_trip_during_normal_decode(self, qapp) -> None:
        """A healthy in-progress decode with recent line events must
        not trip the watchdog."""
        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))
        worker._decoder = MagicMock()
        worker._decoder.feed.side_effect = [
            [
                ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
                ImageProgress(
                    image=prog_image,
                    mode=Mode.ROBOT_36,
                    vis_code=8,
                    lines_decoded=100,
                    lines_total=240,
                ),
            ],
            [
                ImageProgress(
                    image=prog_image,
                    mode=Mode.ROBOT_36,
                    vis_code=8,
                    lines_decoded=200,
                    lines_total=240,
                ),
            ],
        ]
        log = _record_signals(worker)

        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        worker.feed_chunk(np.zeros(10, dtype=np.float32))

        # No spurious ImageComplete from the watchdog.  Progress
        # events fire normally.
        assert len(log["image_complete"]) == 0
        assert worker._decoding is True

    def test_watchdog_state_cleared_on_normal_complete(self, qapp) -> None:
        """A clean ImageComplete event clears the watchdog state so
        the next VIS starts with a fresh deadline."""
        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))
        worker._decoder = MagicMock()
        worker._decoder.feed.return_value = [
            ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
            ImageComplete(
                image=prog_image,
                mode=Mode.ROBOT_36,
                vis_code=8,
            ),
        ]
        worker._decoder.consume_last_buffer.return_value = None

        worker.feed_chunk(np.zeros(10, dtype=np.float32))

        assert worker._decoding_mode is None
        assert worker._decoding_start_time == 0.0
        assert worker._last_progress_image is None

    def test_watchdog_state_cleared_on_reset(self, qapp) -> None:
        """Explicit reset() clears watchdog state so a stalled
        session doesn't leak into the next one."""
        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        worker._decoding_mode = Mode.ROBOT_36
        worker._decoding_start_time = 1234.5
        worker._last_progress_lines = 50

        worker.reset()

        assert worker._decoding_mode is None
        assert worker._decoding_start_time == 0.0
        assert worker._last_progress_lines == 0

    def test_timeout_message_not_overwritten_by_listening_during_cooldown(
        self, qapp
    ) -> None:
        """v0.2.2: after a watchdog trip, the next idle-state flush
        used to immediately emit the routine "Listening…" status,
        which clobbered the timeout message before the user could
        read it.  The cooldown gate keeps idle-state chatter
        suppressed for a short window so the user can actually see
        the timeout message.
        """
        import time

        from open_sstv.ui.workers import _RX_WATCHDOG_LINE_FLOOR_S

        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))

        # Arm the decoder into DECODING, then trip the watchdog.
        worker._decoder = MagicMock()
        worker._decoder.feed.side_effect = [
            [
                ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
                ImageProgress(
                    image=prog_image,
                    mode=Mode.ROBOT_36,
                    vis_code=8,
                    lines_decoded=120,
                    lines_total=240,
                ),
            ],
            [],  # idle-state flush *after* trip
        ]
        log = _record_signals(worker)

        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        worker._last_progress_time = time.monotonic() - (
            _RX_WATCHDOG_LINE_FLOOR_S + 10.0
        )
        worker.feed_chunk(np.zeros(10, dtype=np.float32))  # trips watchdog

        # Right after trip: only the timeout message in status_update
        status_texts_after_trip = [
            t for t in log["status_update"]
            if "timed out" in t.lower()
        ]
        assert len(status_texts_after_trip) == 1, (
            "watchdog trip should emit exactly one 'timed out' "
            f"status; got: {log['status_update']}"
        )

        # Further idle flushes inside the cooldown window must NOT
        # emit "Listening…" or the timeout message is clobbered.
        worker._decoder.feed.side_effect = [[], [], []]
        pre_count = len(log["status_update"])
        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        post_count = len(log["status_update"])
        new_texts = log["status_update"][pre_count:post_count]
        listening = [t for t in new_texts if "Listening" in t]
        assert listening == [], (
            "Listening… status should be suppressed during the "
            f"post-trip cooldown; got: {new_texts}"
        )

    def test_wall_clock_tick_fires_watchdog_even_without_audio(
        self, qapp
    ) -> None:
        """v0.2.1: the wall-clock watchdog tick must fire the
        watchdog check even when no audio is flowing.  Covers the
        user-reported bug where a Martin M2 decode stuck at 93 %
        for 5+ minutes without the watchdog tripping — root cause
        was that the v0.1.36 watchdog only ran inside ``_flush``,
        which only ran when audio chunks arrived.  If PortAudio
        goes quiet (USB sleep, Bluetooth drop, deep fade with
        exactly-zero samples) no flushes fire and the watchdog
        never ticks.
        """
        import time

        from open_sstv.ui.workers import _RX_WATCHDOG_LINE_FLOOR_S

        worker = RxWorker(sample_rate=48_000, flush_samples=1)
        prog_image = Image.new("RGB", (320, 240), (10, 20, 30))

        # Feed one chunk so the timer is created and the decoder
        # transitions into DECODING state.
        worker._decoder = MagicMock()
        worker._decoder.feed.return_value = [
            ImageStarted(mode=Mode.ROBOT_36, vis_code=8),
            ImageProgress(
                image=prog_image,
                mode=Mode.ROBOT_36,
                vis_code=8,
                lines_decoded=120,
                lines_total=240,
            ),
        ]
        log = _record_signals(worker)
        worker.feed_chunk(np.zeros(10, dtype=np.float32))
        assert worker._decoding is True
        assert worker._watchdog_timer is not None

        # Backdate last_progress_time past the no-progress budget
        # so the very next tick should trip.  No more ``feed_chunk``
        # calls — simulating a completely silent audio stream.
        worker._last_progress_time = time.monotonic() - (
            _RX_WATCHDOG_LINE_FLOOR_S + 10.0
        )

        # Fire the tick directly (equivalent to what QTimer does).
        worker._on_watchdog_tick()

        # Partial image surfaced even without any new flush.
        assert len(log["image_complete"]) == 1
        assert worker._decoding is False
