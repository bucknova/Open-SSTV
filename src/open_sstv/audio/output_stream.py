# SPDX-License-Identifier: GPL-3.0-or-later
"""TX audio playback.

Playback is driven entirely by PortAudio's *callback* API — the same
mechanism ``audio/input_stream.py`` already uses safely for RX, and the one
the old "fast path" (``sd.play()``) was always built on under the hood.

Why not the simpler-looking blocking ``stream.write()`` loop this module
used through v0.6.1: on a real system (Fedora + PipeWire), a "Stop" press
mid-transmission was found to reliably **segfault the whole process**.
Root cause, isolated with ``faulthandler`` and a clean-worktree comparison:
PortAudio's blocking ``OutputStream.write()``, once genuinely stuck on that
system's ALSA/PipeWire stack, could not be safely interrupted by *anything*
— ``stream.abort()`` from another thread returned without actually
unblocking the writer; escalating to ``stream.close()`` while that writer
was still blocked corrupted the heap inside PortAudio's ALSA XRUN-recovery
path (``malloc(): unsorted double linked list corrupted``); and even just
abandoning the wedged stream didn't help, since ``sounddevice`` registers
its own ``atexit`` hook (``Pa_Terminate()``) that closes every open stream
at interpreter shutdown, re-triggering the identical crash the next time
the app quit. Confirmed unrelated to PipeWire-sink routing — it reproduced
identically on the plain ALSA "default" device.

The callback API sidesteps the whole bug class *by construction*: this
module's own thread is never itself blocked inside a C-level write() call
on the stream, so there is nothing for another thread's ``abort()`` to fail
to unblock, and closing the stream from *this* thread (never cross-thread —
the ``with sd.OutputStream(...)`` block's own ``__exit__`` handles it) is
the one case PortAudio's docs actually promise is safe. Stress-tested with
real SSTV audio, ``abort()``/``stop()`` at ~15 varied timings, cross-thread,
with and without PipeWire-sink routing live: sub-millisecond returns, zero
wedges, zero crashes, every single trial.

A module-level ``stop()`` interrupts an in-flight playback (used for the
"Stop" button): ``sd.stop()`` for the callback-based fast path, or
``_active_stream.abort()`` — tracked in a module-level slot guarded by a
lock — for the chunked/routed path.

A second module-level guard tracks whether a TX is currently active so
``input_stream._pa_reset`` (which globally calls ``sd._terminate()`` +
``sd._initialize()``) can refuse to run while a TX OutputStream is live.
Without the guard, a user starting RX during TX could crash the process.

PTT timing — keying the radio, waiting a beat for the relay, *then*
playing — lives in the TX worker, not here. This module is intentionally
ignorant of radios so it can be reused by tests and CLI tools that have
no rig.

Public API:
    play_blocking(samples, sample_rate, device=None) -> None
    stop() -> None
    is_tx_active() -> bool
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import queue
import threading
import time
import warnings
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from open_sstv.audio.devices import AudioDevice
from open_sstv.audio.pipewire_route import (
    PipeWireSink,
    route_active_stream_to_sink,
    snapshot_sink_input_ids,
)

_log = logging.getLogger(__name__)

# sounddevice's own callback-wrapping code (``_array()``) builds the
# ``outdata``/``indata`` buffer via an in-place ``ndarray.shape = ...``
# assignment, deprecated since NumPy 2.5, and fires unconditionally on
# *every single* callback invocation — for a multi-minute transmission
# that's tens of thousands of identical warnings. Under Python's default
# filter these dedupe after the first (harmless in normal use). But a
# warning-*capturing* context (pytest's default per-test capture is
# exactly such a context) disables that dedup and processes every
# occurrence, and Python's warnings module is documented as not
# thread-safe: repeatedly hammering it from PortAudio's real-time callback
# thread (not a ``threading.Thread`` Python itself created, so GIL
# acquisition happens on every call) while a test runner instruments the
# main thread's warning state concurrently produced a real, reproducible
# heap-corruption segfault during a long real-audio ``gui``-marked
# integration test — confirmed via faulthandler and a clean-worktree
# comparison (v0.6.2 postmortem). A plain ``warnings.filterwarnings()``
# does *not* reliably help here: pytest's warning capture installs its own
# ``simplefilter("always")`` per test, overriding module-level filters —
# confirmed empirically (the warning still fired 13k+ times per run with a
# filter in place). So instead of filtering the symptom, patch out the
# cause: replace ``_array()`` with an equivalent that builds the same
# writable view via ``np.reshape`` (never deprecated, and — since the
# input is always a 1-D C-contiguous buffer being reshaped to 2-D — never
# copies, so it's the exact same zero-copy view over PortAudio's buffer
# the original returned). Defensive: only patches if the private function
# still has the exact signature this was written against; any mismatch
# (a future sounddevice release changing it) is silently skipped rather
# than risking a crash on import.
def _patch_sounddevice_array_deprecation() -> None:
    try:
        original = sd._array
        if list(inspect.signature(original).parameters) != [
            "buffer", "channels", "dtype",
        ]:
            return

        def _array(buffer, channels, dtype):
            return np.frombuffer(buffer, dtype=dtype).reshape(-1, channels)

        sd._array = _array
    except Exception:  # noqa: BLE001 — a failed patch must never break TX
        _log.debug(
            "could not patch sounddevice._array's deprecated shape "
            "assignment — leaving it as-is", exc_info=True,
        )


_patch_sounddevice_array_deprecation()

# Belt-and-suspenders for any code path the patch above doesn't reach
# (e.g. a sounddevice internal that already imported ``_array`` by
# reference before this module ran): outside of pytest's capture context
# this filter works normally and costs nothing.
warnings.filterwarnings(
    "ignore",
    message=r"Setting the shape on a NumPy array has been deprecated",
    category=DeprecationWarning,
)

# Module-level coordination for ``stop()`` and ``is_tx_active()``.
#
# ``_active_stream``: the live ``sd.OutputStream`` for the chunked/callback
# path (None when no TX is in flight or only the ``sd.play`` fast path is
# in use). ``_tx_active_count``: nesting-safe counter incremented at the
# top of ``play_blocking`` and decremented in its ``finally`` so concurrent
# / re-entrant callers are handled correctly. Both are guarded by
# ``_tx_state_lock``.
_tx_state_lock = threading.Lock()
_active_stream: sd.OutputStream | None = None
_active_device_desc: str = "?"
_tx_active_count: int = 0

#: Size of the handoff queue from the real-time audio callback to the
#: polling loop that fires ``progress_callback``/``chunk_callback``. Sized
#: generously relative to the driver's callback cadence (typically single-
#: digit-to-low-double-digit milliseconds per buffer) — if the polling loop
#: ever falls this far behind (e.g. a hung waterfall slot), dropping the
#: newest entry beats blocking the real-time audio thread, which would
#: itself risk an underrun/glitch.
_CALLBACK_QUEUE_SIZE = 64

#: How often the polling loop runs ``periodic_check``, in seconds of
#: wall-clock time. A module-level constant (rather than an inline
#: literal) so tests can shrink it to run the health-check path quickly
#: instead of waiting out a real second.
_HEALTH_CHECK_INTERVAL_S = 1.0

#: How long the polling loop's queue.get() blocks per iteration before
#: re-checking stop_event/periodic_check timing. Bounds stop-button /
#: health-check responsiveness; small enough to feel instant, large
#: enough not to busy-loop.
_POLL_INTERVAL_S = 0.05


def _describe_device(device: object, route_to_pipewire_sink: PipeWireSink | None = None) -> str:
    """Best-effort ``name (host API)`` for logs.  Never raises."""
    try:
        if route_to_pipewire_sink is not None:
            base = "system default"
            return f"{base}, routed to PipeWire sink {route_to_pipewire_sink.description!r}"
        if isinstance(device, AudioDevice):
            return f"{device.name!r} via {device.host_api}"
        if device is None:
            return "system default"
        info = sd.query_devices(device)
        api = sd.query_hostapis(int(info["hostapi"]))["name"]
        return f"{info['name']!r} via {api}"
    except Exception:  # noqa: BLE001 — diagnostics must never break TX
        return f"device={device!r}"


def is_tx_active() -> bool:
    """``True`` while any ``play_blocking`` call is in flight.

    Read by ``input_stream._pa_reset`` so it can refuse to terminate the
    PortAudio host while a TX OutputStream is alive (which would otherwise
    rip the underlying audio engine out from under the live stream and
    crash on the next callback).
    """
    with _tx_state_lock:
        return _tx_active_count > 0


def run_if_tx_idle(fn: Callable[[], None]) -> bool:
    """Run *fn* while holding the TX-state lock, iff no TX is in flight.

    M9 (v0.3 audit): ``_pa_reset`` used to do ``is_tx_active()`` →
    ``sd._terminate()`` as two separate steps, leaving a check-then-act
    window where a TX starting on another thread between them would have
    its OutputStream killed mid-open — a process crash.  Holding the
    lock across *fn* closes the window: a concurrent ``play_blocking``
    blocks at its counter increment until the reset completes (~100 ms),
    rather than racing it.

    Returns ``True`` if *fn* ran, ``False`` if TX was active.
    """
    with _tx_state_lock:
        if _tx_active_count > 0:
            return False
        fn()
        return True


def play_blocking(
    samples: np.ndarray,
    sample_rate: int,
    device: AudioDevice | int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    gain_provider: Callable[[], float] | None = None,
    periodic_check: Callable[[], None] | None = None,
    chunk_callback: Callable[[np.ndarray], None] | None = None,
    route_to_pipewire_sink: PipeWireSink | None = None,
) -> None:
    """Play a buffer of samples and block until playback finishes.

    Parameters
    ----------
    samples:
        1-D ``int16`` or ``float32`` array. SSTV encoders return ``int16``;
        we don't second-guess the dtype here so callers can hand off the
        encoder output unchanged.
    sample_rate:
        Sample rate the buffer was rendered at, in Hz. Must match what
        the encoder used (typically 48000).
    device:
        Output device to play through. ``None`` uses the system default.
        Accepts either an ``AudioDevice`` (we pull ``.index`` off it) or
        a raw PortAudio index, since the TX worker may have either.
    progress_callback:
        If provided, called as ``progress_callback(samples_written, total)``
        as each chunk of audio is actually output. Runs on the calling
        thread (not PortAudio's real-time thread — see the module
        docstring), so a little behind real time (bounded by the internal
        handoff queue's poll interval, ~50 ms) but never on the audio
        thread's critical path.
    stop_event:
        If provided and set, playback aborts early.
    gain_provider:
        Optional zero-arg callable returning the current linear gain
        (``1.0`` = unity). When set, every audio buffer is scaled by the
        *current* value before being handed to the device, so a user
        moving a gain slider during playback hears the change within one
        driver buffer. If ``None``, samples are used unmodified and
        callers are expected to have pre-scaled the buffer.  The provider
        runs on PortAudio's real-time callback thread, so it must be cheap
        and non-blocking; a simple attribute read is ideal.  Used by the
        test-tone path so ALC calibration is interactive.
    periodic_check:
        Optional zero-arg callable invoked every ~1 s during playback.
        Intended for hardware health checks — e.g. a serial-port ping to
        detect USB unplug mid-TX.  If it raises any exception, playback is
        aborted: ``stop_event`` is set and the stream is aborted
        immediately.  The callable is responsible for emitting any
        user-visible error signal before raising.  Runs on the calling
        thread, same as ``progress_callback``.
    chunk_callback:
        Optional callable invoked as ``chunk_callback(chunk)`` with each
        audio chunk (after gain scaling, before being handed to the
        device).  Used by TxWorker to feed the waterfall display.  Runs on
        the calling thread, same as ``progress_callback``.
    route_to_pipewire_sink:
        Optional ``PipeWireSink`` (see ``audio/pipewire_route.py``). When
        set, ``device`` is ignored and the stream always opens on the safe
        system default — PortAudio's JACK host API (the only way it
        exposes PipeWire's named sinks directly) has been verified to
        corrupt real audio, so we never target it. Instead, right after
        the stream starts, the newly-created PulseAudio sink-input for
        *this* stream is identified and moved onto the target sink via
        ``pactl move-sink-input`` — every other application's audio is
        unaffected. Forces the chunked/callback path (see below) even if
        no other keyword triggers it, since routing needs the open stream
        object to exist. A routing failure (pactl missing, timeout, etc.)
        is logged and playback simply continues on the system default —
        it never aborts the transmission.

    Raises
    ------
    ValueError
        If ``samples`` isn't 1-D or has length 0.
    sounddevice.PortAudioError
        For underlying PortAudio failures (device disappeared, sample rate
        not supported, etc.). Callers surface these to the UI as a
        non-modal status bar message.
    """
    if samples.ndim != 1:
        msg = f"samples must be 1-D mono, got shape {samples.shape}"
        raise ValueError(msg)
    if samples.size == 0:
        raise ValueError("samples buffer is empty")

    # A routed stream never targets the JACK-hostapi device directly (see
    # the ``route_to_pipewire_sink`` docstring above) — it always opens on
    # the safe system default, then gets moved onto the target sink after
    # the fact.
    device_index = (
        None
        if route_to_pipewire_sink is not None
        else (device.index if isinstance(device, AudioDevice) else device)
    )

    # Increment ``_tx_active_count`` for the whole lifetime of this call,
    # not just the chunked/callback path — ``sd.play`` also opens a stream
    # under the hood, so ``_pa_reset`` must refuse during the fast path too.
    global _tx_active_count
    with _tx_state_lock:
        _tx_active_count += 1
    try:
        if (
            route_to_pipewire_sink is None
            and progress_callback is None
            and stop_event is None
            and gain_provider is None
            and periodic_check is None
            and chunk_callback is None
        ):
            # Fast path: no progress reporting, no stop, no live gain, no
            # health check, no routing needed. Callback-based under the
            # hood already (PortAudio's native mode) — see module
            # docstring for why that matters.
            sd.play(samples, samplerate=sample_rate, device=device_index, blocking=True)
            sd.wait()
            return

        total = samples.size
        #: Mutated only from inside ``_audio_callback``, which PortAudio
        #: guarantees is never invoked concurrently with itself.
        position = 0
        finished = threading.Event()
        #: Handoff from the real-time audio callback to the polling loop
        #: below — see the module docstring and ``_CALLBACK_QUEUE_SIZE``.
        cb_queue: queue.Queue[tuple[np.ndarray | None, int, int]] = queue.Queue(
            maxsize=_CALLBACK_QUEUE_SIZE
        )
        want_callbacks = progress_callback is not None or chunk_callback is not None

        def _audio_callback(outdata, frames, _time_info, status) -> None:
            nonlocal position
            if status:
                # Non-fatal (e.g. an output underflow blip) — PortAudio
                # already handles the underlying recovery; just note it.
                _log.debug("TX audio callback status flags: %s", status)
            if stop_event is not None and stop_event.is_set():
                outdata.fill(0)
                raise sd.CallbackStop
            end = min(position + frames, total)
            n = end - position
            chunk = samples[position:end]
            if gain_provider is not None:
                gain = gain_provider()
                if gain != 1.0:
                    # Clip to the sample dtype's range so float math
                    # doesn't wrap on int16 overflow. We mirror the
                    # pre-scale path in workers.py for consistency.
                    if np.issubdtype(chunk.dtype, np.integer):
                        info = np.iinfo(chunk.dtype)
                        chunk = np.clip(
                            chunk.astype(np.float64) * gain,
                            info.min,
                            info.max,
                        ).astype(chunk.dtype)
                    else:
                        chunk = np.clip(
                            chunk.astype(np.float64) * gain,
                            -1.0,
                            1.0,
                        ).astype(chunk.dtype)
            outdata[:n, 0] = chunk
            if n < frames:
                outdata[n:, 0] = 0
            position = end
            if want_callbacks:
                # UI feedback falling behind isn't worth blocking (or
                # dropping samples from) the real-time audio thread over —
                # the polling loop will catch up on the next entries.
                with contextlib.suppress(queue.Full):
                    cb_queue.put_nowait(
                        (chunk.copy() if chunk_callback is not None else None, position, total)
                    )
            if position >= total:
                raise sd.CallbackStop

        global _active_stream, _active_device_desc
        desc = _describe_device(device, route_to_pipewire_sink)
        # Snapshot must happen *before* the stream starts — it's the
        # baseline ``route_active_stream_to_sink`` diffs against to find
        # the one new sink-input our own stream creates.
        before_sink_input_ids = (
            snapshot_sink_input_ids() if route_to_pipewire_sink is not None else None
        )
        with sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype=samples.dtype,
            device=device_index,
            callback=_audio_callback,
            finished_callback=finished.set,
        ) as stream:
            # Publish the stream so ``stop()`` can ``abort()`` it from
            # another thread. Unlike the blocking-write design this
            # replaced, this thread is never itself blocked inside a
            # C-level call on the stream, so there's nothing for a
            # cross-thread ``abort()`` to fail to unblock — see the module
            # docstring for the (considerable) history here.
            with _tx_state_lock:
                _active_stream = stream
                _active_device_desc = desc
            _log.debug(
                "TX playback opened: %s, %d samples @ %d Hz", desc, total, sample_rate
            )
            if route_to_pipewire_sink is not None and before_sink_input_ids is not None:
                # Best-effort: log-and-continue-on-default is the deliberate
                # failure mode, never raise or abort the transmission.
                route_active_stream_to_sink(route_to_pipewire_sink, before_sink_input_ids)
            try:
                _last_check = time.monotonic()
                while not finished.is_set():
                    if stop_event is not None and stop_event.is_set():
                        stream.abort()
                        break

                    # Periodic health check — e.g. a serial-port ping to
                    # detect USB unplug mid-TX.  The callable emits any
                    # user-visible error before raising; we just need to
                    # abort on any exception.
                    if (
                        periodic_check is not None
                        and time.monotonic() - _last_check >= _HEALTH_CHECK_INTERVAL_S
                    ):
                        _last_check = time.monotonic()
                        try:
                            periodic_check()
                        except Exception:  # noqa: BLE001
                            if stop_event is not None:
                                stop_event.set()
                            stream.abort()
                            break

                    try:
                        chunk, played, chunk_total = cb_queue.get(timeout=_POLL_INTERVAL_S)
                    except queue.Empty:
                        continue
                    if chunk_callback is not None and chunk is not None:
                        chunk_callback(chunk)
                    if progress_callback is not None:
                        progress_callback(played, chunk_total)

                # Drain whatever's left in the queue so the final
                # progress/chunk update isn't silently lost when we broke
                # out (or finished naturally) between poll iterations.
                while True:
                    try:
                        chunk, played, chunk_total = cb_queue.get_nowait()
                    except queue.Empty:
                        break
                    if chunk_callback is not None and chunk is not None:
                        chunk_callback(chunk)
                    if progress_callback is not None:
                        progress_callback(played, chunk_total)
            finally:
                with _tx_state_lock:
                    _active_stream = None
        # The ``with`` block's __exit__ has already called stream.stop()
        # then stream.close() here, from *this* thread — the one case
        # PortAudio's docs actually promise is safe (see module
        # docstring). No cross-thread close(), no escalation, no wedge
        # possible: this thread was never blocked inside a C-level write()
        # call to begin with.
    finally:
        with _tx_state_lock:
            _tx_active_count -= 1


def stop() -> None:
    """Abort an in-flight playback.

    Safe to call when nothing is playing.  Handles both code paths:

    * ``sd.stop()`` cancels the fast ``sd.play`` path.
    * ``_active_stream.abort()`` interrupts the chunked/callback path.

    The "Stop" button on the TX panel calls this; the TX worker then
    unwinds out of ``play_blocking`` and drops PTT.

    History (v0.6.2): this used to also escalate to ``stream.close()``
    from a throwaway thread if the writer hadn't made progress within a
    grace period, for a chunked-write design where ``abort()`` was only
    best-effort cross-thread. On a real Linux/PipeWire system that
    escalation — closing a stream from a different thread than the one
    genuinely still blocked inside ``stream.write()`` on it — corrupted
    the heap in PortAudio's ALSA backend and crashed the process. Moving
    ``play_blocking`` to PortAudio's callback API (see its module
    docstring) eliminates the underlying wedge entirely: stress-tested at
    ~15 varied abort timings, cross-thread, with and without PipeWire-sink
    routing, ``abort()`` here returns in well under a millisecond every
    time, with no escalation needed or present anymore.
    """
    sd.stop()
    with _tx_state_lock:
        stream = _active_stream
        desc = _active_device_desc
    if stream is None:
        return
    t0 = time.monotonic()
    try:
        stream.abort()
        _log.info(
            "output_stream.stop: abort() returned in %.1f ms (%s)",
            (time.monotonic() - t0) * 1000, desc,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("output_stream.stop: abort failed: %s (%s)", exc, desc)


__all__ = ["is_tx_active", "play_blocking", "run_if_tx_idle", "stop"]
