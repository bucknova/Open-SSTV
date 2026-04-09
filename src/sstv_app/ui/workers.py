# SPDX-License-Identifier: GPL-3.0-or-later
"""``QThread`` workers for long-running RX and TX tasks.

The DSP loop and the audio playback both block, so they live on dedicated
``QThread`` instances and communicate with the GUI thread exclusively via
Qt signals (queued connections, which Qt makes thread-safe automatically).
We deliberately avoid asyncio/qasync — no concurrent socket fan-out, so a
worker-thread-per-task model is the right fit and ``pytest-qt`` Just Works.

Phase 1 ships only ``TxWorker``. ``RxWorker`` lands in Phase 2.

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
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from sstv_app.audio import output_stream
from sstv_app.audio.devices import AudioDevice
from sstv_app.core.encoder import DEFAULT_SAMPLE_RATE, encode
from sstv_app.core.modes import Mode
from sstv_app.radio.base import ManualRig, Rig
from sstv_app.radio.exceptions import RigError

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


#: Default delay between keying PTT and starting audio playback. Most
#: rigs need ~50–200 ms for the relay to settle and the SSB filter to
#: open. 200 ms is on the safe side; advanced users can override per-rig
#: in settings (Phase 3).
DEFAULT_PTT_DELAY_S = 0.2


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


__all__ = ["DEFAULT_PTT_DELAY_S", "TxWorker"]
