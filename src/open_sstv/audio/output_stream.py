# SPDX-License-Identifier: GPL-3.0-or-later
"""TX audio playback.

For v1 we don't need true streaming output — TX is "render the whole image
to a buffer, then play it" — so this is a thin wrapper around
``sounddevice.play`` + ``sounddevice.wait``. The blocking variant
(``play_blocking``) is what the TX worker thread calls; the GUI thread
never touches it.

A module-level ``stop()`` interrupts an in-flight playback (used for the
"Stop" button). Sounddevice stores the active stream as a global, so
``stop()`` and the playing thread coordinate through PortAudio rather than
through Python state we'd otherwise have to lock.

PTT timing — keying the radio, waiting a beat for the relay, *then*
playing — lives in the TX worker, not here. This module is intentionally
ignorant of radios so it can be reused by tests and CLI tools that have
no rig.

Public API:
    play_blocking(samples, sample_rate, device=None) -> None
    stop() -> None
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

import numpy as np
import sounddevice as sd

from open_sstv.audio.devices import AudioDevice


def play_blocking(
    samples: np.ndarray,
    sample_rate: int,
    device: AudioDevice | int | None = None,
    progress_callback: "Callable[[int, int], None] | None" = None,
    stop_event: "threading.Event | None" = None,
    gain_provider: "Callable[[], float] | None" = None,
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

    if progress_callback is None and stop_event is None and gain_provider is None:
        # Fast path: no progress reporting, no stop, no live gain.
        sd.play(samples, samplerate=sample_rate, device=device_index, blocking=True)
        sd.wait()
        return

    # Chunked write path: ~0.1 s chunks keep stop-button latency below
    # 100 ms and give smooth progress updates. Also the granularity at
    # which live gain is re-read — one chunk late at worst.
    chunk_size = int(sample_rate * 0.1)
    total = samples.size

    with sd.OutputStream(
        samplerate=sample_rate,
        channels=1,
        dtype=samples.dtype,
        device=device_index,
    ) as stream:
        written = 0
        while written < total:
            if stop_event is not None and stop_event.is_set():
                break
            end = min(written + chunk_size, total)
            chunk = samples[written:end]
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
            stream.write(chunk.reshape(-1, 1))
            written = end
            if progress_callback is not None:
                progress_callback(written, total)


def stop() -> None:
    """Abort an in-flight playback.

    Safe to call when nothing is playing — sounddevice treats it as a
    no-op. The "Stop" button on the TX panel calls this; the TX worker
    will then unwind out of ``play_blocking`` and drop PTT.
    """
    sd.stop()


__all__ = ["play_blocking", "stop"]
