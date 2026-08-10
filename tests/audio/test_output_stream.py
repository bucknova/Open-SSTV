# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.audio.output_stream``.

The happy path here would mean opening a real PortAudio output stream,
which is hardware-dependent and flaky in CI. Instead ``_FakeCallbackStream``
below stands in for ``sd.OutputStream``: it drives the real ``callback``
function ``play_blocking`` builds — on its own thread, like PortAudio's
real-time thread would — so tests exercise the actual gain-scaling,
progress/chunk-callback handoff, stop, and periodic-check logic without
touching real hardware. Real-hardware playback is exercised by hand during
release smoke testing — see ``docs/release-checklist.md`` once it lands in
Phase 3.

v0.6.2 note: this suite was rewritten when ``play_blocking`` moved from a
blocking ``stream.write()`` loop to PortAudio's callback API (see
``output_stream.py``'s module docstring for why — a real Stop-button
segfault, root-caused and fixed). ``_FakeCallbackStream`` replaces the old
write-recording ``_FakeStream``; ``TestWedgeEscalation`` is gone along with
the escalate-to-``close()`` mechanism it tested, which no longer exists.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import numpy as np
import pytest
import sounddevice as sd

from open_sstv.audio import output_stream
from open_sstv.audio.devices import AudioDevice
from open_sstv.audio.pipewire_route import PipeWireSink


class _FakeCallbackStream:
    """Stand-in for ``sd.OutputStream(callback=..., finished_callback=...)``.

    Runs the real callback repeatedly on its own thread — like PortAudio's
    real-time thread would — recording every ``outdata`` buffer it was
    handed, until the callback raises ``sd.CallbackStop`` or ``abort()``/
    ``stop()`` is called. ``blocksize`` defaults to 4800 (0.1 s at 48 kHz)
    to match the granularity the old write-loop tests were written against.
    """

    def __init__(
        self,
        *,
        callback,
        finished_callback=None,
        blocksize: int = 4800,
        dtype=np.int16,
        pump_delay_s: float = 0.0005,
        **_kwargs,
    ) -> None:
        self._callback = callback
        self._finished_callback = finished_callback
        self._blocksize = blocksize
        self._dtype = dtype
        self._pump_delay_s = pump_delay_s
        self.writes: list[np.ndarray] = []
        self.aborted = False
        self.stopped = False
        self.closed = False
        self._stop_flag = threading.Event()
        self._pump_thread: threading.Thread | None = None

    def __enter__(self) -> _FakeCallbackStream:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
        self.close()

    def start(self) -> None:
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def _pump(self) -> None:
        try:
            while not self._stop_flag.is_set():
                outdata = np.zeros((self._blocksize, 1), dtype=self._dtype)
                try:
                    self._callback(outdata, self._blocksize, None, None)
                except sd.CallbackStop:
                    self.writes.append(outdata.copy())
                    break
                self.writes.append(outdata.copy())
                if self._pump_delay_s:
                    time.sleep(self._pump_delay_s)
        finally:
            if self._finished_callback is not None:
                self._finished_callback()

    def abort(self, ignore_errors: bool = True) -> None:
        self.aborted = True
        self._stop_flag.set()

    def stop(self, ignore_errors: bool = True) -> None:
        self.stopped = True
        self._stop_flag.set()
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2)

    def close(self, ignore_errors: bool = True) -> None:
        self.closed = True


def _patch_output_stream(blocksize: int = 4800, pump_delay_s: float = 0.0005):
    """Patch ``sd.OutputStream`` to construct ``_FakeCallbackStream``
    instances, returning ``(patcher, created)`` — ``created`` accumulates
    every instance built, since ``play_blocking`` opens a fresh stream per
    call and tests want to inspect the one that was actually used."""
    created: list[_FakeCallbackStream] = []

    def factory(**kwargs):
        fake = _FakeCallbackStream(
            blocksize=blocksize, pump_delay_s=pump_delay_s, **kwargs
        )
        created.append(fake)
        return fake

    return (
        patch("open_sstv.audio.output_stream.sd.OutputStream", side_effect=factory),
        created,
    )


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


# --- PipeWire sink routing (pactl move-sink-input, not a direct JACK-hostapi
# device) — see audio/pipewire_route.py for why this exists. ---


class TestPipeWireSinkRouting:
    _TARGET = PipeWireSink(id=175, name="Radio_null", description="Radio")

    def test_forces_device_none_on_fast_path_even_with_conflicting_device(self) -> None:
        """route_to_pipewire_sink must win over a stale/conflicting device=
        — the whole point is to never target the JACK-hostapi device index
        directly, which has been verified to corrupt real audio."""
        samples = np.zeros(100, dtype=np.int16)
        conflicting_device = AudioDevice(
            index=7, name="Radio", host_api="PipeWire",
            max_input_channels=2, max_output_channels=2,
            default_sample_rate=48000.0,
        )
        patcher, created = _patch_output_stream()
        with (
            patch("open_sstv.audio.output_stream.sd.play") as mock_play,
            patch("open_sstv.audio.output_stream.sd.wait"),
            patcher,
            patch(
                "open_sstv.audio.output_stream.snapshot_sink_input_ids",
                return_value=set(),
            ),
            patch(
                "open_sstv.audio.output_stream.route_active_stream_to_sink",
                return_value=True,
            ) as mock_route,
        ):
            output_stream.play_blocking(
                samples, 48000, device=conflicting_device,
                route_to_pipewire_sink=self._TARGET,
            )
        # route_to_pipewire_sink alone forces the chunked path (see next
        # test), so the fast path (sd.play) must NOT have been used here.
        mock_play.assert_not_called()
        mock_route.assert_called_once()
        assert len(created) == 1

    def test_forces_chunked_path_with_no_other_callbacks(self) -> None:
        """Routing needs the open OutputStream object and time for the
        sink-input to register — must not take the sd.play fast-path
        shortcut even when no progress/gain/stop/health-check callback was
        given."""
        samples = np.zeros(100, dtype=np.int16)
        patcher, created = _patch_output_stream()
        with (
            patch("open_sstv.audio.output_stream.sd.play") as mock_play,
            patcher,
            patch(
                "open_sstv.audio.output_stream.snapshot_sink_input_ids",
                return_value=set(),
            ),
            patch(
                "open_sstv.audio.output_stream.route_active_stream_to_sink",
                return_value=True,
            ),
        ):
            output_stream.play_blocking(
                samples, 48000, route_to_pipewire_sink=self._TARGET
            )
        mock_play.assert_not_called()
        assert len(created[0].writes) == 1

    def test_snapshot_taken_before_stream_opens(self) -> None:
        """The before/after diff routing relies on is only valid if the
        snapshot happens before the stream (and its sink-input) exists."""
        samples = np.zeros(100, dtype=np.int16)
        call_order: list[str] = []

        def fake_snapshot():
            call_order.append("snapshot")
            return {1, 2}

        def open_wrapper(**kwargs):
            call_order.append("open")
            return _FakeCallbackStream(**kwargs)

        with (
            patch(
                "open_sstv.audio.output_stream.snapshot_sink_input_ids",
                side_effect=fake_snapshot,
            ),
            patch(
                "open_sstv.audio.output_stream.sd.OutputStream",
                side_effect=open_wrapper,
            ),
            patch(
                "open_sstv.audio.output_stream.route_active_stream_to_sink",
                return_value=True,
            ) as mock_route,
        ):
            output_stream.play_blocking(
                samples, 48000, route_to_pipewire_sink=self._TARGET
            )
        assert call_order == ["snapshot", "open"]
        # The exact snapshot taken before open must be what's handed to
        # the router, not a fresh one taken after.
        mock_route.assert_called_once_with(self._TARGET, {1, 2})

    def test_routing_failure_does_not_raise_or_abort_playback(self) -> None:
        """A failed route (pactl missing, timeout, ambiguous, ...) must
        degrade to Open-SSTV's existing behaviour — play on the system
        default — never abort the transmission."""
        sr = 48000
        samples = np.zeros(sr // 10 * 3, dtype=np.int16)
        patcher, created = _patch_output_stream()
        with (
            patcher,
            patch(
                "open_sstv.audio.output_stream.snapshot_sink_input_ids",
                return_value=set(),
            ),
            patch(
                "open_sstv.audio.output_stream.route_active_stream_to_sink",
                return_value=False,
            ),
        ):
            output_stream.play_blocking(
                samples, sr, route_to_pipewire_sink=self._TARGET
            )
        # All 3 chunks still got written despite the routing failure.
        assert len(created[0].writes) == 3

    def test_device_param_ignored_in_favor_of_system_default(self) -> None:
        """sd.OutputStream must open with device=None (system default)
        when routing — never the raw JACK-hostapi index a caller might
        still be holding onto."""
        sr = 48000
        samples = np.zeros(sr // 10, dtype=np.int16)
        patcher, created = _patch_output_stream()
        with (
            patcher,
            patch(
                "open_sstv.audio.output_stream.snapshot_sink_input_ids",
                return_value=set(),
            ),
            patch(
                "open_sstv.audio.output_stream.route_active_stream_to_sink",
                return_value=True,
            ),
        ):
            output_stream.play_blocking(
                samples, sr, device=99, route_to_pipewire_sink=self._TARGET
            )
        assert len(created) == 1


def test_stop_calls_sd_stop() -> None:
    with patch("open_sstv.audio.output_stream.sd.stop") as mock_stop:
        output_stream.stop()
    mock_stop.assert_called_once()


# --- Critical-tier fixes: stop() must actually abort the chunked/callback
# stream, and is_tx_active() must be true exactly while play_blocking runs ---


def test_stop_aborts_active_chunked_stream() -> None:
    """CRIT-4: ``output_stream.stop()`` must actually abort the callback
    stream — the "Stop" button calls this and the TX worker relies on it
    to unwind out of ``play_blocking`` promptly."""
    sr = 48000
    samples = np.zeros(sr * 3, dtype=np.int16)  # plenty of frames available
    patcher, created = _patch_output_stream()
    stop_event = threading.Event()
    fired = threading.Event()

    def fire_stop_once(_played: int, _total: int) -> None:
        if not fired.is_set():
            fired.set()
            output_stream.stop()

    with patcher, patch("open_sstv.audio.output_stream.sd.stop"):
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=fire_stop_once,
            stop_event=stop_event,
        )

    assert created[0].aborted, "stop() must call stream.abort() on the chunked path"


def test_stop_handles_no_active_stream() -> None:
    """stop() must be a clean no-op when nothing is playing."""
    with patch("open_sstv.audio.output_stream.sd.stop") as mock_stop:
        output_stream.stop()  # no play_blocking in flight
    mock_stop.assert_called_once()


def test_is_tx_active_tracks_play_blocking() -> None:
    """CRIT-1: ``is_tx_active()`` must be True while ``play_blocking`` is
    running so ``_pa_reset`` knows to refuse.  Idle → False; in-flight →
    True; after return → False."""
    assert output_stream.is_tx_active() is False

    sr = 48000
    samples = np.zeros(sr // 10 * 2, dtype=np.int16)
    saw_active: list[bool] = []
    patcher, _created = _patch_output_stream()

    def record_active(_written: int, _total: int) -> None:
        saw_active.append(output_stream.is_tx_active())

    with patcher:
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


def test_play_blocking_applies_live_gain_per_chunk() -> None:
    """Regression: the test-tone TX gain slider used to only affect the
    next tone because ``transmit_test_tone`` pre-scaled the whole buffer.
    ``gain_provider`` is re-read for each buffer so slider drags are
    audible within one driver period. This test fakes a 4-chunk playback
    and verifies each chunk is scaled by the *then-current* provider value.
    """
    sr = 48000
    # 0.4 s of full-scale DC so scaling is easy to check. Four 0.1 s
    # chunks at 48 kHz -> 4 chunks of 4800 samples (matches the fake's
    # default blocksize).
    samples = np.full(sr // 10 * 4, 10_000, dtype=np.int16)

    # Provider returns 0.5, 1.0, 1.5, 2.0 in order.
    gains = iter([0.5, 1.0, 1.5, 2.0])

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,  # force chunked path
            gain_provider=lambda: next(gains),
        )

    fake_stream = created[0]
    assert len(fake_stream.writes) == 4
    peak_by_chunk = [int(np.abs(w).max()) for w in fake_stream.writes]
    assert peak_by_chunk == [5000, 10000, 15000, 20000]


def test_play_blocking_gain_provider_clips_int16_overflow() -> None:
    """With a sample near int16 max and gain > 1, scaled output must
    clip to the dtype's range instead of wrapping negative.
    """
    sr = 48000
    samples = np.full(sr // 10, 30_000, dtype=np.int16)  # one chunk

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            gain_provider=lambda: 2.0,  # would overflow to 60_000 without clip
        )

    fake_stream = created[0]
    assert len(fake_stream.writes) == 1
    assert fake_stream.writes[0].max() == np.iinfo(np.int16).max  # 32767
    # And crucially, no wrap-around to negative.
    assert fake_stream.writes[0].min() >= 0


def test_play_blocking_gain_provider_unity_is_passthrough() -> None:
    """When the provider returns 1.0 the chunk should be written
    unmodified (no clip, values pass straight through).
    """
    sr = 48000
    samples = np.full(sr // 10, 12345, dtype=np.int16)

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            gain_provider=lambda: 1.0,
        )

    fake_stream = created[0]
    assert len(fake_stream.writes) == 1
    np.testing.assert_array_equal(fake_stream.writes[0].ravel(), samples)


# --- Periodic health check (serial-port ping for USB unplug detection) ---
#
# The health check now runs on a wall-clock cadence in the polling loop
# (``_HEALTH_CHECK_INTERVAL_S``) rather than "every N chunks" — shrink the
# interval so tests don't have to wait out a real second.


def test_periodic_check_aborts_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """When periodic_check raises, stop_event is set and playback aborts
    promptly instead of running to completion."""
    monkeypatch.setattr(output_stream, "_HEALTH_CHECK_INTERVAL_S", 0.02)
    sr = 48000
    # Comfortably more audio than the check interval needs to fire at least
    # once — if the abort didn't work, this would otherwise play to the end.
    samples = np.zeros(sr * 5, dtype=np.int16)
    stop_event = threading.Event()
    check_calls: list[int] = []

    def _flaky_check() -> None:
        check_calls.append(1)
        raise OSError("serial port gone")

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            stop_event=stop_event,
            periodic_check=_flaky_check,
        )

    assert len(check_calls) >= 1
    assert stop_event.is_set()
    assert created[0].aborted


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
    """When periodic_check=None, playback completes all chunks normally
    without ever invoking a health check."""
    sr = 48000
    samples = np.zeros(sr // 10 * 5, dtype=np.int16)
    stop_event = threading.Event()

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            stop_event=stop_event,
        )

    assert len(created[0].writes) == 5
    assert not stop_event.is_set()


# --- chunk_callback (waterfall feed) ---


def test_chunk_callback_receives_every_chunk() -> None:
    sr = 48000
    samples = np.full(sr // 10 * 3, 777, dtype=np.int16)
    seen: list[np.ndarray] = []

    patcher, created = _patch_output_stream()
    with patcher:
        output_stream.play_blocking(
            samples,
            sr,
            progress_callback=lambda *_: None,
            chunk_callback=seen.append,
        )

    assert len(created[0].writes) == 3
    assert len(seen) == 3
    for chunk in seen:
        assert chunk.ndim == 1
        assert int(chunk[0]) == 777
