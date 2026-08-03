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

import logging
import time
from unittest.mock import MagicMock, patch

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


# --- Critical-tier fixes: stop() must actually abort the chunked stream,
# and is_tx_active() must be true exactly while play_blocking is running ---


def test_stop_aborts_active_chunked_stream() -> None:
    """CRIT-4: ``output_stream.stop()`` was a no-op for the chunked-write
    path because ``sd.stop()`` only cancels ``sd.play`` streams.  Now
    ``stop()`` calls ``abort()`` on the active ``sd.OutputStream`` so a
    wedged ``stream.write()`` can be unblocked from another thread."""
    import threading

    class _AbortableStream:
        def __init__(self) -> None:
            self.writes: list[np.ndarray] = []
            self.aborted = False

        def __enter__(self) -> _AbortableStream:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def write(self, chunk: np.ndarray) -> None:
            self.writes.append(np.asarray(chunk).copy())

        def abort(self) -> None:
            self.aborted = True

    sr = 48000
    samples = np.zeros(sr // 10 * 5, dtype=np.int16)
    fake_stream = _AbortableStream()
    abort_seen = threading.Event()

    # Stop() runs on a different thread (typically GUI) while play_blocking
    # blocks the TX worker thread.  Have the periodic_check fire stop()
    # from the worker thread to simulate the Stop button firing.
    def fire_stop_after_one_chunk() -> None:
        # By the second invocation we've written at least one chunk —
        # call stop() and verify it propagates to abort().
        if len(fake_stream.writes) >= 1 and not abort_seen.is_set():
            output_stream.stop()
            abort_seen.set()

    with (
        patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream),
        patch("open_sstv.audio.output_stream.sd.stop"),
    ):
        stop_event = threading.Event()
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda written, total: (
                fire_stop_after_one_chunk()
            ),
            stop_event=stop_event,
        )

    assert fake_stream.aborted, "stop() must call stream.abort() on chunked path"


def test_stop_handles_no_active_stream() -> None:
    """stop() must be a clean no-op when nothing is playing."""
    with patch("open_sstv.audio.output_stream.sd.stop") as mock_stop:
        output_stream.stop()  # no play_blocking in flight
    # sd.stop is always called (handles the fast-path); the chunked
    # branch should be skipped without raising.
    mock_stop.assert_called_once()


def test_is_tx_active_tracks_play_blocking() -> None:
    """CRIT-1: ``is_tx_active()`` must be True while ``play_blocking`` is
    running so ``_pa_reset`` knows to refuse.  Idle → False; in-flight →
    True; after return → False."""
    assert output_stream.is_tx_active() is False

    sr = 48000
    samples = np.zeros(sr // 10 * 2, dtype=np.int16)
    saw_active: list[bool] = []
    fake_stream = _FakeStream()

    def record_active(written: int, total: int) -> None:
        saw_active.append(output_stream.is_tx_active())

    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=record_active,
        )

    assert saw_active and all(saw_active), (
        "is_tx_active() must report True while play_blocking is running"
    )
    assert output_stream.is_tx_active() is False, (
        "is_tx_active() must return to False after play_blocking returns"
    )


def test_is_tx_active_false_after_exception() -> None:
    """The TX-active counter must decrement even when play_blocking raises
    (e.g. PortAudioError on stream open) — otherwise a single failed TX
    would lock out _pa_reset forever."""
    sr = 48000
    samples = np.zeros(sr // 10, dtype=np.int16)

    with patch(
        "open_sstv.audio.output_stream.sd.OutputStream",
        side_effect=RuntimeError("simulated open failure"),
    ), pytest.raises(RuntimeError):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,  # force chunked path
        )

    assert output_stream.is_tx_active() is False


# --- Live gain (test-tone ALC calibration) ---

class _FakeStream:
    """Minimal sd.OutputStream stand-in that records every chunk it was
    handed. Supports the context-manager protocol so ``with
    sd.OutputStream(...)`` works under patch.
    """

    def __init__(self) -> None:
        self.writes: list[np.ndarray] = []

    def __enter__(self) -> _FakeStream:
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


# --- Periodic health check (serial-port ping for USB unplug detection) ---


def test_periodic_check_aborts_on_exception() -> None:
    """When periodic_check raises, stop_event is set and playback exits early."""
    import threading

    sr = 48000
    # 11 chunks so the 10th triggers the first check.
    samples = np.zeros(sr // 10 * 11, dtype=np.int16)
    stop_event = threading.Event()
    check_calls: list[int] = []

    fake_stream = _FakeStream()

    def _flaky_check() -> None:
        check_calls.append(1)
        raise OSError("serial port gone")

    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            stop_event=stop_event,
            periodic_check=_flaky_check,
        )

    assert len(check_calls) == 1, "check must fire exactly once before abort"
    assert stop_event.is_set(), "stop_event must be set when check raises"
    assert len(fake_stream.writes) <= 10, "playback must stop near the first check"


def test_periodic_check_stops_before_end() -> None:
    """Playback must abort early (not write all chunks) when check raises."""
    import threading

    sr = 48000
    # 30 chunks so check at chunk 10 leaves 20 unwritten.
    samples = np.zeros(sr // 10 * 30, dtype=np.int16)
    stop_event = threading.Event()
    fake_stream = _FakeStream()

    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            stop_event=stop_event,
            periodic_check=lambda: (_ for _ in ()).throw(OSError("gone")),
        )

    assert len(fake_stream.writes) <= 10


def test_periodic_check_not_called_on_fast_path() -> None:
    """The fast sd.play/wait path is used when periodic_check is None
    and there is no progress/stop/gain either."""
    sr = 48000
    samples = np.zeros(sr // 10, dtype=np.int16)

    with (
        patch("open_sstv.audio.output_stream.sd.play") as mock_play,
        patch("open_sstv.audio.output_stream.sd.wait"),
    ):
        output_stream.play_blocking(samples, sr)

    mock_play.assert_called_once()


def test_periodic_check_not_called_when_none() -> None:
    """When periodic_check=None, the check counter is never incremented
    and playback completes all chunks normally."""
    import threading

    sr = 48000
    samples = np.zeros(sr // 10 * 5, dtype=np.int16)
    stop_event = threading.Event()
    fake_stream = _FakeStream()

    with patch("open_sstv.audio.output_stream.sd.OutputStream", return_value=fake_stream):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            stop_event=stop_event,
        )

    assert len(fake_stream.writes) == 5
    assert not stop_event.is_set()


# === wedge survival (v0.6.1) ===========================================
#
# A FlexRadio user on Windows/MME hit a TX audio path that stopped
# draining; abort() called cross-thread did not unblock the writer, so the
# worker thread leaked. stop() now escalates to close() when the writer
# makes no progress, and says so in the log.


class TestWedgeEscalation:
    def test_escalates_to_close_when_writer_makes_no_progress(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        from open_sstv.audio import output_stream as os_mod

        monkeypatch.setattr(os_mod, "_ABORT_GRACE_S", 0.05)
        stream = MagicMock()
        monkeypatch.setattr(os_mod, "_active_stream", stream)
        monkeypatch.setattr(os_mod, "_active_device_desc", "'DAX TX' via MME")
        monkeypatch.setattr(os_mod, "_write_seq", 7)  # frozen: no progress
        monkeypatch.setattr(os_mod.sd, "stop", lambda: None)

        with caplog.at_level(logging.ERROR):
            os_mod.stop()
            time.sleep(0.3)  # let the escalation thread run

        stream.abort.assert_called_once()
        stream.close.assert_called_once(), "a wedged writer must escalate to close()"
        assert any("WEDGED" in r.message for r in caplog.records)

    def test_no_escalation_when_writes_are_progressing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow-but-alive device must not have its stream closed."""
        from open_sstv.audio import output_stream as os_mod

        monkeypatch.setattr(os_mod, "_ABORT_GRACE_S", 0.05)
        stream = MagicMock()
        monkeypatch.setattr(os_mod, "_active_stream", stream)
        monkeypatch.setattr(os_mod, "_write_seq", 1)
        monkeypatch.setattr(os_mod.sd, "stop", lambda: None)

        os_mod.stop()
        # Writer advances during the grace window → still alive.
        os_mod._write_seq = 2
        time.sleep(0.3)

        stream.abort.assert_called_once()
        stream.close.assert_not_called()

    def test_no_escalation_when_playback_unwound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If play_blocking finished normally, don't touch the old stream."""
        from open_sstv.audio import output_stream as os_mod

        monkeypatch.setattr(os_mod, "_ABORT_GRACE_S", 0.05)
        stream = MagicMock()
        monkeypatch.setattr(os_mod, "_active_stream", stream)
        monkeypatch.setattr(os_mod, "_write_seq", 3)
        monkeypatch.setattr(os_mod.sd, "stop", lambda: None)

        os_mod.stop()
        os_mod._active_stream = None  # playback unwound cleanly
        time.sleep(0.3)

        stream.close.assert_not_called()

    def test_stop_is_safe_with_no_active_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from open_sstv.audio import output_stream as os_mod

        monkeypatch.setattr(os_mod, "_active_stream", None)
        monkeypatch.setattr(os_mod.sd, "stop", lambda: None)
        os_mod.stop()  # must not raise
