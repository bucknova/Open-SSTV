# SPDX-License-Identifier: GPL-3.0-or-later
"""TX audio playback.

For v1 we don't need true streaming output — TX is "render the whole image
to a buffer, then play it" — so this is a thin wrapper around
``sounddevice.play`` + ``sounddevice.wait``. The blocking variant
(``play_blocking``) is what the TX worker thread calls; the GUI thread
never touches it.

A module-level ``stop()`` interrupts an in-flight playback (used for the
"Stop" button).  For the legacy ``sd.play`` fast path, ``sd.stop()``
suffices.  For the chunked-write path (any caller that passes a
``stop_event``, ``progress_callback``, ``gain_provider``,
``periodic_check``, or ``chunk_callback``), we open ``sd.OutputStream``
directly and track it in a module-level slot guarded by a lock so
``stop()`` can call ``stream.abort()`` from any thread — discarding
buffered samples and unblocking the writer immediately.  Without this,
``stop()`` was a no-op on the chunked path and abort relied solely on
``stop_event`` polled between chunks (~100 ms latency), which could not
interrupt a wedged ``stream.write()`` call (USB stall, driver hang).

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

import logging
import threading
import time
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from open_sstv.audio.devices import AudioDevice

_log = logging.getLogger(__name__)

# Module-level coordination for ``stop()`` and ``is_tx_active()``.
#
# ``_active_stream``: the live ``sd.OutputStream`` for the chunked-write path
# (None when no TX is in flight or only the legacy ``sd.play`` path is used).
# ``_tx_active_count``: nesting-safe counter incremented at the top of
# ``play_blocking`` and decremented in its ``finally`` so concurrent /
# re-entrant callers are handled correctly.  Both are guarded by
# ``_tx_state_lock``.
_tx_state_lock = threading.Lock()
_active_stream: sd.OutputStream | None = None
_tx_active_count: int = 0

#: Monotonically increasing count of completed chunk writes.  ``stop()``
#: samples it, waits, and samples again: if it hasn't moved the writer is
#: still stuck inside one ``stream.write()`` and ``abort()`` did not take.
_write_seq: int = 0
#: Human-readable description of the device the live stream is writing to,
#: so a wedge report names the device and host API without the caller
#: having to plumb it through.
_active_device_desc: str = "?"

#: A single ~100 ms chunk taking longer than this to write means the device
#: is not draining at real-time speed — the leading indicator of the wedge
#: this module's escalation path exists to survive.
_SLOW_WRITE_WARN_S: float = 1.0
#: How long ``stop()`` gives ``abort()`` to actually unblock the writer
#: before escalating to ``close()``.
_ABORT_GRACE_S: float = 1.5


def _describe_device(device: object) -> str:
    """Best-effort ``name (host API)`` for logs.  Never raises."""
    try:
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
        after each chunk write. Runs on the calling thread.
    stop_event:
        If provided and set, playback aborts early.
    gain_provider:
        Optional zero-arg callable returning the current linear gain
        (``1.0`` = unity). When set, each ~0.1 s chunk is scaled by the
        *current* value before being written to the device, so a user
        moving a gain slider during playback hears the change within one
        chunk (<100 ms). If ``None``, samples are written unmodified and
        callers are expected to have pre-scaled the buffer.  The provider
        is called on the playback thread, so it must be cheap and
        non-blocking; a simple attribute read is ideal.  Used by the
        test-tone path so ALC calibration is interactive.
    periodic_check:
        Optional zero-arg callable invoked every ~1 s (every 10 chunks)
        during playback.  Intended for hardware health checks — e.g. a
        serial-port ping to detect USB unplug mid-TX.  If it raises any
        exception, playback is aborted: ``stop_event`` is set and the
        write loop exits immediately.  The callable is responsible for
        emitting any user-visible error signal before raising.  Runs on
        the calling thread.
    chunk_callback:
        Optional callable invoked as ``chunk_callback(chunk)`` with each
        audio chunk (after gain scaling, before writing to the device).
        Used by TxWorker to feed the waterfall display.  Runs on the
        calling thread — must be fast and non-blocking.

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

    device_index = device.index if isinstance(device, AudioDevice) else device

    # Increment ``_tx_active_count`` for the whole lifetime of this call,
    # not just the chunked write path — ``sd.play`` also opens a stream
    # under the hood, so ``_pa_reset`` must refuse during the fast path too.
    global _tx_active_count
    with _tx_state_lock:
        _tx_active_count += 1
    try:
        if (
            progress_callback is None
            and stop_event is None
            and gain_provider is None
            and periodic_check is None
            and chunk_callback is None
        ):
            # Fast path: no progress reporting, no stop, no live gain, no
            # health check needed.
            sd.play(samples, samplerate=sample_rate, device=device_index, blocking=True)
            sd.wait()
            return

        # Chunked write path: ~0.1 s chunks keep stop-button latency below
        # 100 ms and give smooth progress updates. Also the granularity at
        # which live gain is re-read — one chunk late at worst.
        chunk_size = int(sample_rate * 0.1)
        total = samples.size

        # How often to run the periodic health check (every N ~0.1 s chunks ≈ 1 s).
        _CHECK_INTERVAL = 10
        _check_counter = 0

        global _active_stream, _active_device_desc, _write_seq
        desc = _describe_device(device)
        with sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype=samples.dtype,
            device=device_index,
        ) as stream:
            # Publish the stream so ``stop()`` can ``abort()`` it from
            # another thread — that's the only way to unblock a wedged
            # ``stream.write()`` on a stalled USB driver.
            with _tx_state_lock:
                _active_stream = stream
                _active_device_desc = desc
            _log.debug(
                "TX playback opening: %s, %d samples @ %d Hz", desc, total, sample_rate
            )
            try:
                written = 0
                while written < total:
                    if stop_event is not None and stop_event.is_set():
                        break

                    # Periodic health check — e.g. a serial-port ping to
                    # detect USB unplug mid-TX.  The callable emits any
                    # user-visible error before raising; we just need to
                    # abort on any exception.
                    if periodic_check is not None:
                        _check_counter += 1
                        if _check_counter % _CHECK_INTERVAL == 0:
                            try:
                                periodic_check()
                            except Exception:  # noqa: BLE001
                                if stop_event is not None:
                                    stop_event.set()
                                break

                    end = min(written + chunk_size, total)
                    chunk = samples[written:end]
                    if gain_provider is not None:
                        gain = gain_provider()
                        if gain != 1.0:
                            # Clip to the sample dtype's range so float
                            # math doesn't wrap on int16 overflow. We
                            # mirror the pre-scale path in workers.py
                            # for consistency.
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
                    if chunk_callback is not None:
                        chunk_callback(chunk)
                    # Time every write.  A ~100 ms chunk that takes seconds
                    # means the device has stopped draining — the leading
                    # edge of the wedge where abort() may not reach us.
                    _w0 = time.monotonic()
                    stream.write(chunk.reshape(-1, 1))
                    _elapsed = time.monotonic() - _w0
                    with _tx_state_lock:
                        _write_seq += 1
                    if _elapsed > _SLOW_WRITE_WARN_S:
                        _log.warning(
                            "TX audio write stalled %.1f s on a %.0f ms chunk "
                            "(%s) — device is not draining in real time",
                            _elapsed, (end - written) / sample_rate * 1000, desc,
                        )
                    written = end
                    if progress_callback is not None:
                        progress_callback(written, total)
            finally:
                with _tx_state_lock:
                    _active_stream = None
    finally:
        with _tx_state_lock:
            _tx_active_count -= 1


def stop() -> None:
    """Abort an in-flight playback.

    Safe to call when nothing is playing.  Handles both code paths:

    * ``sd.stop()`` cancels the legacy ``sd.play`` fast path.
    * ``_active_stream.abort()`` discards the chunked-write buffer
      immediately, unblocking any ``stream.write()`` call that has
      wedged on a stalled USB driver.  Without this, ``stop()`` was a
      no-op on the chunked path (which is every TxWorker call), and
      abort relied solely on ``stop_event`` polled between chunks —
      fine in the common case but unable to interrupt a hung write.

    The "Stop" button on the TX panel calls this; the TX worker then
    unwinds out of ``play_blocking`` and drops PTT.

    H-3 (cross-thread safety, audit 4.7/v0.2.9): ``stop()`` is invoked
    from the GUI thread while ``stream.write()`` is running on the TX
    worker thread — i.e. ``stream.abort()`` is called cross-thread.
    PortAudio's documentation does *not* portably guarantee that
    ``Pa_AbortStream`` is safe to invoke from a thread other than the
    one that opened the stream.  Real-world behaviour:
        macOS Core Audio   — safe; abort interrupts the callback cleanly.
        Linux ALSA / Pulse — safe in practice across pyaudio/sounddevice.
        Windows WASAPI     — safe in practice.
        Windows MME / WDM-KS — historically reported sharp edges; the
            output-device filter at ``audio/devices.py`` excludes WDM-KS
            from the picker so the worst case is avoided.
    If a future bug surfaces as "Stop button does nothing on Windows
    MME", inspect this call first.  Long-term fix is to post a stop
    signal to the TX worker thread and have *it* call ``abort()`` from
    the stream-owning thread, but that adds a 0.1 s latency (one chunk
    boundary) for the cross-thread case, which the audit graded as a
    worse trade-off than the current direct call.
    """
    sd.stop()
    with _tx_state_lock:
        stream = _active_stream
        seq_before = _write_seq
        desc = _active_device_desc
    if stream is None:
        return
    t0 = time.monotonic()
    try:
        stream.abort()
        _log.info(
            "output_stream.stop: abort() returned in %.0f ms (%s)",
            (time.monotonic() - t0) * 1000, desc,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("output_stream.stop: abort failed: %s (%s)", exc, desc)
    # Escalation.  ``abort()`` is documented as safe from the stream-owning
    # thread only; called cross-thread it is best-effort, and on Windows MME
    # in particular it has been observed not to unblock a wedged write().
    # Watch from a throwaway thread (never block the caller — this runs on
    # the GUI thread and on the remote dead-man's-switch tick) and, if the
    # writer still hasn't advanced, close the stream outright as a stronger
    # lever.  Logged either way so a wedge is legible in the next report.
    threading.Thread(
        target=_escalate_if_stuck,
        args=(stream, seq_before, desc),
        name="sstv-tx-abort-escalate",
        daemon=True,
    ).start()


def _escalate_if_stuck(
    stream: sd.OutputStream, seq_before: int, desc: str
) -> None:
    """If ``abort()`` didn't unblock the writer, escalate to ``close()``."""
    time.sleep(_ABORT_GRACE_S)
    with _tx_state_lock:
        still_live = _active_stream is stream
        seq_now = _write_seq
    if not still_live:
        return  # playback unwound normally — nothing to escalate
    if seq_now != seq_before:
        # Writes are still completing, so the loop is alive and will notice
        # the stop flag at the next chunk boundary. Not a wedge.
        return
    _log.error(
        "TX audio appears WEDGED: no write progress %.1f s after abort() on %s. "
        "Escalating to stream.close(). PTT has already been dropped "
        "independently, so this is an audio-path problem, not a stuck "
        "transmitter. On Windows, prefer a WASAPI output device over MME.",
        _ABORT_GRACE_S, desc,
    )
    try:
        stream.close()
        _log.info("output_stream: escalated close() returned")
    except Exception as exc:  # noqa: BLE001 — last resort, never raise
        _log.error("output_stream: escalated close() also failed: %s", exc)


__all__ = ["is_tx_active", "play_blocking", "run_if_tx_idle", "stop"]
