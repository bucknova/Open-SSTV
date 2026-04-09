# SPDX-License-Identifier: GPL-3.0-or-later
"""``QThread`` workers for long-running RX and TX tasks.

The DSP loop and the audio playback both block, so they live on dedicated
``QThread`` instances and communicate with the GUI thread exclusively via
Qt signals (queued connections, which Qt makes thread-safe automatically).
We deliberately avoid asyncio/qasync — no concurrent socket fan-out, so a
worker-thread-per-task model is the right fit and ``pytest-qt`` Just Works.

Phase 1 shipped ``TxWorker``; Phase 2 step 17 adds ``RxWorker``.

TxWorker
========

The TX flow is "encode the whole image to a buffer, key the rig, play the
buffer, unkey the rig" — a single linear sequence per transmission. The
worker exposes ``transmit(image, mode)`` as a ``@Slot`` so the UI can
connect ``tx_panel.transmit_requested`` directly to it; Qt's auto-connect
becomes a QueuedConnection across the thread boundary, so the call lands
on the worker thread without any explicit ``QMetaObject.invokeMethod``.

Stopping mid-transmission is the only tricky bit. The worker is blocked
inside ``play_blocking`` when "Stop" is clicked, so a queued slot call to
``request_stop`` would never run. Instead ``request_stop`` is a plain
Python method that's safe to call from any thread:

* ``threading.Event.set()`` is thread-safe.
* ``output_stream.stop()`` calls ``sounddevice.sd.stop()``, which is
  documented as thread-safe and unblocks the playback thread out of
  ``sd.wait()`` immediately.

When playback unwinds, the worker checks the stop flag, drops PTT, and
emits ``transmission_aborted`` instead of ``transmission_complete``.

Error policy
------------

A failed ``set_ptt(True)`` aborts the transmission *before* any audio is
played — if the user explicitly wanted rig control and it failed, they
do **not** want a surprise transmission on whatever frequency the rig
happens to be sitting on. ``ManualRig.set_ptt`` is a no-op so this path
is silent on the manual-PTT side.

A failed ``play_blocking`` (lost audio device, etc.) is reported as an
error but does not block the unkey: the ``finally`` clause always runs
``set_ptt(False)`` so we never leave the rig in a stuck-keyed state.

RxWorker
========

The RX flow is the inverse of TX: chunks stream in from
``InputStreamWorker.chunk_ready`` on a worker thread and the worker
hands them to ``core.decoder.Decoder``. The decoder's ``feed`` call
runs ``decode_wav`` over the accumulated buffer every time, which is
O(buffer) and therefore prohibitive if called on every ~20 ms audio
chunk. The worker absorbs that by accumulating chunks locally and
only flushing to ``Decoder.feed`` every ``_RX_FLUSH_SAMPLES_DEFAULT``
samples of audio (1 s at 48 kHz). This turns a 36 s Robot 36
transmission from ~1800 decode attempts into ~36, each of which
fails fast until the full image is present — leaving plenty of
headroom on a Pi-class machine.

``DecoderEvent`` values from ``Decoder.feed`` are translated into
Qt signals (``image_started``, ``image_complete``, ``error``) so UI
code can connect to them directly without importing the core
dataclasses.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from sstv_app.audio import output_stream
from sstv_app.audio.devices import AudioDevice
from sstv_app.core.decoder import (
    DecodeError,
    Decoder,
    ImageComplete,
    ImageStarted,
)
from sstv_app.core.encoder import DEFAULT_SAMPLE_RATE, encode
from sstv_app.core.modes import Mode
from sstv_app.radio.base import ManualRig, Rig
from sstv_app.radio.exceptions import RigError

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from PIL.Image import Image as PILImage


#: Default delay between keying PTT and starting audio playback. Most
#: rigs need ~50–200 ms for the relay to settle and the SSB filter to
#: open. 200 ms is on the safe side; advanced users can override per-rig
#: in settings (Phase 3).
DEFAULT_PTT_DELAY_S = 0.2

#: How many samples to accumulate in ``RxWorker`` before flushing a
#: batch to ``Decoder.feed``. 1 s at 48 kHz — see the module docstring
#: for why we throttle at all. Lowering this makes RX more responsive
#: to short-image modes but multiplies decode attempts; raising it
#: delays the "image complete" signal by up to one flush interval
#: past the actual end of the transmission.
_RX_FLUSH_SAMPLES_DEFAULT: int = 48_000


class TxWorker(QObject):
    """Render an image to SSTV audio and play it on a worker thread.

    All five signals are emitted from the worker thread; Qt's auto-connect
    will queue them onto whatever thread the receiving slot belongs to.

    Signals
    -------
    transmission_started():
        Emitted after encoding finishes and PTT has been keyed
        successfully — i.e. the rig is now actively transmitting.
    transmission_complete():
        Emitted after a clean playback + unkey.
    transmission_aborted():
        Emitted when ``request_stop`` was called before playback finished.
    error(str):
        Emitted for any failure (encode, PTT, playback, or unkey). The
        TX worker continues to a clean shutdown — error doesn't replace
        complete/aborted, it's an additional signal the UI surfaces.
    """

    transmission_started = Signal()
    transmission_complete = Signal()
    transmission_aborted = Signal()
    error = Signal(str)

    def __init__(
        self,
        rig: Rig | None = None,
        output_device: AudioDevice | int | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        ptt_delay_s: float = DEFAULT_PTT_DELAY_S,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rig: Rig = rig if rig is not None else ManualRig()
        self._output_device = output_device
        self._sample_rate = sample_rate
        self._ptt_delay_s = ptt_delay_s
        self._stop_event = threading.Event()

    @Slot(object, object)
    def transmit(self, image: "PILImage", mode: Mode) -> None:
        """Encode and transmit one image. Worker-thread entry point.

        Always emits exactly one of ``transmission_complete`` or
        ``transmission_aborted`` per call (or, on early encode/PTT
        failure, only ``error``).
        """
        self._stop_event.clear()

        # --- Encode (CPU-bound, ~100 ms for the modes we ship) ---
        try:
            samples = encode(image, mode, sample_rate=self._sample_rate)
        except Exception as exc:  # noqa: BLE001 — surface anything to UI
            self.error.emit(f"Encode failed: {exc}")
            return

        # --- Key the rig ---
        try:
            self._rig.set_ptt(True)
        except RigError as exc:
            # User explicitly wanted rig control and it failed — abort
            # before any audio leaves the soundcard. ManualRig never
            # raises so this only fires for real backends.
            self.error.emit(f"Could not key rig: {exc}")
            return

        self.transmission_started.emit()

        # --- Play the buffer ---
        playback_succeeded = False
        try:
            time.sleep(self._ptt_delay_s)
            if self._stop_event.is_set():
                # Stop pressed during the PTT delay window, before any audio.
                pass
            else:
                output_stream.play_blocking(
                    samples, self._sample_rate, device=self._output_device
                )
                playback_succeeded = True
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Playback failed: {exc}")
        finally:
            # ALWAYS unkey, even on error or stop, so the rig never gets
            # left in a stuck-keyed state.
            try:
                self._rig.set_ptt(False)
            except RigError as exc:
                self.error.emit(f"Could not unkey rig: {exc}")

        if self._stop_event.is_set():
            self.transmission_aborted.emit()
        elif playback_succeeded:
            self.transmission_complete.emit()
        # else: an error has already been emitted via the playback failure path

    def request_stop(self) -> None:
        """Abort an in-flight transmission. Safe to call from any thread.

        Sets the stop flag and yanks PortAudio out of ``sd.wait()`` so
        the playback unwinds immediately. The worker thread then drops
        PTT and emits ``transmission_aborted``.
        """
        self._stop_event.set()
        output_stream.stop()


class RxWorker(QObject):
    """Consume audio chunks and emit decoded SSTV images.

    Lives on a worker thread (``moveToThread``). The GUI connects
    ``InputStreamWorker.chunk_ready`` to ``feed_chunk`` and listens
    for the image-event signals below.

    Signals
    -------
    image_started(Mode, int):
        Emitted when a full VIS header has been decoded. The second
        argument is the raw 8-bit VIS code (handy for the status bar).
    image_complete(object, Mode, int):
        Emitted when a full image has been sliced out of the audio.
        The first argument is a ``PIL.Image.Image`` — we pass it via
        ``object`` rather than a ``QImage`` so the worker stays free
        of GUI-side pixel format conversions.
    error(str):
        Emitted for any decode failure (malformed VIS, unsupported
        mode, 2-D feed). The worker keeps running; callers surface
        errors as non-modal status bar messages.
    """

    image_started = Signal(object, int)  # (Mode, vis_code)
    image_complete = Signal(object, object, int)  # (PIL.Image, Mode, vis_code)
    error = Signal(str)

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        flush_samples: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sample_rate = sample_rate
        self._decoder = Decoder(sample_rate)
        self._scratch: list["NDArray[np.float64]"] = []
        self._scratch_samples: int = 0
        self._flush_samples: int = (
            flush_samples
            if flush_samples is not None
            else _RX_FLUSH_SAMPLES_DEFAULT
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    # === slots ===

    @Slot(object)
    def feed_chunk(self, chunk: "NDArray") -> None:
        """Buffer one audio chunk; flush to the decoder on a cadence.

        Safe to invoke via queued connection from the audio worker
        thread. The chunk is copied into float64 eagerly (the rest of
        the DSP pipeline runs in float64) so the caller is free to
        reuse its buffer after the signal returns.
        """
        try:
            arr = np.asarray(chunk, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            self.error.emit(f"Bad chunk dtype: {exc}")
            return
        if arr.ndim != 1:
            self.error.emit(f"Expected 1-D chunk, got {arr.ndim}-D")
            return
        if arr.size == 0:
            return

        self._scratch.append(arr)
        self._scratch_samples += arr.size

        if self._scratch_samples >= self._flush_samples:
            self._flush()

    @Slot()
    def reset(self) -> None:
        """Drop the scratch buffer and reset the decoder state.

        Called when the user clicks "Clear" or switches input device.
        After ``reset`` the next ``feed_chunk`` begins a fresh hunt
        for a VIS header.
        """
        self._scratch.clear()
        self._scratch_samples = 0
        self._decoder.reset()

    @Slot()
    def flush(self) -> None:
        """Force an immediate flush of any buffered audio to the decoder.

        Exposed for the ``stopped`` signal path so the tail of an
        in-flight transmission isn't discarded when the user stops
        capture mid-image. Idempotent.
        """
        if self._scratch_samples > 0:
            self._flush()

    # === internal ===

    def _flush(self) -> None:
        if not self._scratch:
            return
        if len(self._scratch) == 1:
            joined = self._scratch[0]
        else:
            joined = np.concatenate(self._scratch)
        self._scratch.clear()
        self._scratch_samples = 0

        try:
            events = self._decoder.feed(joined)
        except Exception as exc:  # noqa: BLE001 — anything surfaces to UI
            self.error.emit(f"Decoder exception: {exc}")
            return

        for event in events:
            self._dispatch(event)

    def _dispatch(self, event: object) -> None:
        if isinstance(event, ImageStarted):
            self.image_started.emit(event.mode, event.vis_code)
        elif isinstance(event, ImageComplete):
            self.image_complete.emit(event.image, event.mode, event.vis_code)
        elif isinstance(event, DecodeError):
            self.error.emit(event.message)


__all__ = ["DEFAULT_PTT_DELAY_S", "RxWorker", "TxWorker"]
