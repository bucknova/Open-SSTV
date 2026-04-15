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
* ``output_stream.stop()`` calls ``sounddevice.sd.stop()``, which
  unblocks ``sd.wait()`` on the fast path.  On the chunked OutputStream
  path the stop_event check fires at every 0.1 s chunk boundary.

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
samples of audio (2 s at 48 kHz). This turns a 36 s Robot 36
transmission from ~1800 decode attempts into ~18, each of which
fails fast until the full image is present — leaving plenty of
headroom on a Pi-class machine.

``DecoderEvent`` values from ``Decoder.feed`` are translated into
Qt signals (``image_started``, ``image_complete``, ``error``) so UI
code can connect to them directly without importing the core
dataclasses.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal, Slot

_log = logging.getLogger(__name__)

from open_sstv.audio import output_stream
from open_sstv.audio.devices import AudioDevice
from open_sstv.core.cw import make_cw
from open_sstv.core.decoder import (
    DecodeError,
    Decoder,
    ImageComplete,
    ImageProgress,
    ImageStarted,
    decode_wav,
)
from open_sstv.core.encoder import DEFAULT_SAMPLE_RATE, encode
from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig, Rig
from open_sstv.radio.exceptions import RigError

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from PIL.Image import Image as PILImage


#: Silence inserted between the end of the SSTV image audio and the CW ID.
#: 500 ms gives the receiver AGC time to settle before the CW tone starts.
_CW_GAP_S: float = 0.500

#: Default delay between keying PTT and starting audio playback. Most
#: rigs need ~50–200 ms for the relay to settle and the SSB filter to
#: open. 200 ms is on the safe side; advanced users can override per-rig
#: in settings (Phase 3).
DEFAULT_PTT_DELAY_S = 0.2

#: How long to accumulate audio in ``RxWorker`` before flushing a
#: batch to ``Decoder.feed``.  The batch decoder reprocesses the entire
#: growing buffer on every flush (O(N) per flush → O(N²) total), so a
#: longer interval trades responsiveness for lower CPU load.  2 s is the
#: sweet spot: it halves the decode-attempt count vs. 1 s without
#: delaying the "image complete" signal by more than one extra second.
#: Tune this constant rather than hunting for the magic number in tests.
_DECODE_FLUSH_INTERVAL_S: float = 2.0

#: Derived flush threshold in samples at the default 48 kHz sample rate.
#: ``RxWorker`` recomputes a per-instance value from the constructor's
#: ``sample_rate`` parameter, so this default is only used when
#: ``flush_samples`` is not passed explicitly.
_RX_FLUSH_SAMPLES_DEFAULT: int = int(_DECODE_FLUSH_INTERVAL_S * DEFAULT_SAMPLE_RATE)

#: Hard upper bound on a single transmission. If encode + playback have
#: not finished within this many seconds the watchdog fires, forcing PTT
#: off and aborting playback. The longest SSTV mode we ship (Martin M1)
#: takes ~114 s; 300 s gives plenty of headroom while still protecting
#: against a stuck encoder or hung audio driver.
_MAX_TX_DURATION_S: float = 300.0

#: Default duration for the ALC/linearity test tone, in seconds.
_TEST_TONE_DURATION_S: float = 5.0

#: Two-tone test frequencies (Hz).  700 + 1900 Hz is the ARRL standard
#: for SSB ALC / intermodulation testing.
_TEST_TONE_FREQ_LO: float = 700.0
_TEST_TONE_FREQ_HI: float = 1900.0


def _make_two_tone(sample_rate: int, duration_s: float) -> "NDArray[np.int16]":
    """Generate a two-tone test signal (700 Hz + 1900 Hz) as int16 PCM.

    The two equal-amplitude sine waves are summed and the result is scaled
    so the *peak* of the sum sits at −1 dBFS.  Each component therefore
    has an amplitude of ``0.5 × 10^(−1/20) ≈ 0.446`` of full scale.

    This is a calibration signal, not a linearity-critical SSTV image, so
    we want maximum drive into the radio.  At −1 dBFS peak the two-tone
    average power is −7 dBFS, which is enough to light ALC on the IC-7300
    and similar radios even with a conservatively set USB MOD Level.  The
    user's TX output-gain slider provides additional headroom control if
    needed.
    """
    n = int(sample_rate * duration_s)
    t = np.arange(n, dtype=np.float64) / sample_rate
    # Peak of two equal-amplitude sines can reach 2.0, so each is scaled
    # to half the −1 dBFS ceiling.
    amplitude = 0.5 * (10 ** (-1.0 / 20.0))  # ≈ 0.4467
    sig = np.sin(2.0 * np.pi * _TEST_TONE_FREQ_LO * t)
    sig += np.sin(2.0 * np.pi * _TEST_TONE_FREQ_HI * t)
    sig *= amplitude
    return (sig * 32767.0).astype(np.int16)


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
    transmission_progress = Signal(int, int)  # (samples_played, samples_total)
    transmission_complete = Signal()
    transmission_aborted = Signal()
    watchdog_fired = Signal()
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
        self._rig_lock = threading.Lock()
        self._output_device = output_device
        self._sample_rate = sample_rate
        self._ptt_delay_s = ptt_delay_s
        self._output_gain: float = 1.0
        self._cw_id_enabled: bool = False
        self._cw_callsign: str = ""
        self._cw_wpm: int = 20
        self._cw_tone_hz: int = 800
        self._tx_banner_enabled: bool = False
        self._tx_banner_callsign: str = ""
        self._tx_banner_bg_color: str = "#202020"
        self._tx_banner_text_color: str = "#FFFFFF"
        self._tx_banner_size: str = "small"
        self._stop_event = threading.Event()
        self._watchdog_triggered: bool = False

    def set_output_device(self, device: AudioDevice | int | None) -> None:
        """Change the output device at runtime (e.g. after settings save)."""
        self._output_device = device

    def set_output_gain(self, gain: float) -> None:
        """Set the software output gain (1.0 = unity). Thread-safe."""
        self._output_gain = gain

    def set_ptt_delay(self, delay_s: float) -> None:
        """Update the PTT-to-audio delay at runtime (e.g. after settings save)."""
        self._ptt_delay_s = delay_s

    def set_rig(self, rig: Rig) -> None:
        """Swap the rig backend at runtime (e.g. after connect/disconnect).

        Protected by a lock so a GUI-thread swap can't race with the
        worker-thread snapshot in ``transmit()``.
        """
        with self._rig_lock:
            self._rig = rig

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update the sample rate used for encoding and playback.

        Takes effect on the next ``transmit()`` call. Safe to call from
        any thread (plain int assignment is atomic under the GIL).
        """
        self._sample_rate = sample_rate

    def set_cw_id(
        self,
        enabled: bool,
        callsign: str,
        wpm: int = 20,
        tone_hz: int = 800,
    ) -> None:
        """Configure CW station ID appended to every SSTV transmission.

        Thread-safe: all fields are plain Python scalars/strings whose
        assignment is atomic under the GIL. Called from the GUI thread
        via ``MainWindow._apply_config()``.

        Parameters
        ----------
        enabled:
            Whether to append CW ID. When ``False`` (or when
            *callsign* is empty) the ID is silently skipped.
        callsign:
            Operator callsign in any case; ``make_cw`` uppercases it.
            If empty, CW ID is skipped with a warning log even when
            *enabled* is ``True``.
        wpm:
            Sending speed (15–30 WPM). Values outside range are safe
            but should be clamped upstream in ``AppConfig``.
        tone_hz:
            Sidetone frequency in Hz (400–1200).
        """
        self._cw_id_enabled = enabled
        self._cw_callsign = callsign.strip()
        self._cw_wpm = wpm
        self._cw_tone_hz = tone_hz

    def set_tx_banner(
        self,
        enabled: bool,
        callsign: str,
        bg_color: str = "#202020",
        text_color: str = "#FFFFFF",
        size: str = "small",
    ) -> None:
        """Configure the TX header banner stamped on every SSTV transmission.

        Thread-safe: all fields are plain Python scalars/strings.
        Called from the GUI thread via ``MainWindow._apply_config()``.

        Parameters
        ----------
        enabled:
            Whether to stamp the banner.  When ``False`` the image is
            passed to ``encode()`` unchanged.
        callsign:
            Operator callsign shown flush-right in the banner.  Empty
            string omits the callsign column.
        bg_color:
            CSS hex background colour, e.g. ``"#202020"``.
        text_color:
            CSS hex text colour, e.g. ``"#FFFFFF"``.
        size:
            Named size preset — ``"small"`` (default since v0.1.22),
            ``"medium"``, or ``"large"``.  Controls both strip height and
            font size proportionally.
        """
        self._tx_banner_enabled = enabled
        self._tx_banner_callsign = callsign.strip().upper()
        self._tx_banner_bg_color = bg_color
        self._tx_banner_text_color = text_color
        self._tx_banner_size = size

    def emergency_unkey(self) -> None:
        """Best-effort PTT-off for the shutdown path.

        Called from closeEvent if the TX thread doesn't join within the
        timeout.  Runs on the GUI thread; ignores all errors so we never
        block the exit path.
        """
        try:
            with self._rig_lock:
                self._rig.set_ptt(False)
        except Exception:  # noqa: BLE001
            pass

    @Slot(object, object)
    def transmit(self, image: "PILImage", mode: Mode) -> None:
        """Encode and transmit one image. Worker-thread entry point.

        Always emits exactly one of ``transmission_complete`` or
        ``transmission_aborted`` per call (or, on early encode/PTT
        failure, only ``error``).
        """
        self._stop_event.clear()
        self._watchdog_triggered = False

        # Snapshot the rig reference once so a mid-TX call to set_rig()
        # (e.g. user disconnects the radio) cannot swap the backend between
        # set_ptt(True) and set_ptt(False), which would leave the real rig
        # stuck keyed via a no-op ManualRig.set_ptt(False).
        with self._rig_lock:
            rig = self._rig

        # Start the watchdog before any blocking work.
        watchdog = threading.Timer(_MAX_TX_DURATION_S, self._watchdog_fire)
        watchdog.start()

        try:
            # --- Apply TX banner (if enabled) ---
            if self._tx_banner_enabled:
                from open_sstv import __version__
                from open_sstv.core.banner import apply_tx_banner, banner_size_params
                _bh, _fs = banner_size_params(self._tx_banner_size)
                image = apply_tx_banner(
                    image,
                    __version__,
                    self._tx_banner_callsign,
                    self._tx_banner_bg_color,
                    self._tx_banner_text_color,
                    banner_height=_bh,
                    font_size=_fs,
                )

            # --- Encode (CPU-bound, ~100 ms for the modes we ship) ---
            try:
                samples = encode(image, mode, sample_rate=self._sample_rate)
            except Exception as exc:  # noqa: BLE001 — surface anything to UI
                self.error.emit(f"Encode failed: {exc}")
                return

            # Apply software output gain
            if self._output_gain != 1.0:
                samples = np.clip(
                    samples.astype(np.float64) * self._output_gain,
                    -32768, 32767,
                ).astype(samples.dtype)

            # Append CW station ID: gap + CW tail keyed under the same PTT.
            # Test Tone skips this path entirely (see transmit_test_tone).
            if self._cw_id_enabled:
                if self._cw_callsign:
                    gap = np.zeros(
                        int(_CW_GAP_S * self._sample_rate), dtype=np.int16
                    )
                    cw = make_cw(
                        self._cw_callsign,
                        wpm=self._cw_wpm,
                        tone_hz=self._cw_tone_hz,
                        sample_rate=self._sample_rate,
                        peak_dbfs=-1.0,
                    )
                    if self._output_gain != 1.0:
                        cw = np.clip(
                            cw.astype(np.float64) * self._output_gain,
                            -32768, 32767,
                        ).astype(np.int16)
                    samples = np.concatenate([samples, gap, cw])
                else:
                    _log.warning(
                        "CW ID is enabled but callsign is empty — "
                        "skipping CW tail. Set callsign in Settings."
                    )

            result = self._run_tx(samples, rig)
        finally:
            # Cancel the watchdog whether we finished cleanly, were stopped,
            # or hit an error — it must not fire after transmit() returns.
            watchdog.cancel()

        if result is None:
            return  # PTT failed; error already emitted
        if self._stop_event.is_set():
            self.transmission_aborted.emit()
        elif result:
            self.transmission_complete.emit()
        # else: playback error — error signal already emitted, no complete/aborted

    @Slot()
    def transmit_test_tone(self) -> None:
        """Generate and transmit a two-tone test signal. Worker-thread entry point.

        Produces ``_TEST_TONE_DURATION_S`` seconds of 700 Hz + 1900 Hz at
        −1 dBFS peak.  Follows the identical PTT-key → ptt_delay → play →
        PTT-unkey sequence as ``transmit()``, including the watchdog, stop
        button, and gain controls.
        """
        self._stop_event.clear()
        self._watchdog_triggered = False

        with self._rig_lock:
            rig = self._rig

        watchdog = threading.Timer(_MAX_TX_DURATION_S, self._watchdog_fire)
        watchdog.start()

        try:
            samples = _make_two_tone(self._sample_rate, _TEST_TONE_DURATION_S)

            # Honour the user's output-gain setting just like regular TX.
            if self._output_gain != 1.0:
                samples = np.clip(
                    samples.astype(np.float64) * self._output_gain,
                    -32768, 32767,
                ).astype(samples.dtype)

            result = self._run_tx(samples, rig)
        finally:
            watchdog.cancel()

        if result is None:
            return
        if self._stop_event.is_set():
            self.transmission_aborted.emit()
        elif result:
            self.transmission_complete.emit()

    def _run_tx(
        self, samples: "NDArray", rig: "Rig"
    ) -> "bool | None":
        """Key PTT, play *samples*, unkey PTT.

        Returns
        -------
        True
            Playback completed cleanly (no stop, no error).
        False
            Playback was cut short by a stop request or a non-fatal audio
            error (``error`` signal already emitted in that case).
        None
            PTT key failed; ``error`` signal already emitted.  The caller
            should return immediately without emitting complete/aborted.
        """
        # --- Key the rig ---
        try:
            rig.set_ptt(True)
        except RigError as exc:
            # User explicitly wanted rig control and it failed — abort
            # before any audio leaves the soundcard.  ManualRig never
            # raises so this only fires for real backends.
            self.error.emit(f"Could not key rig: {exc}")
            return None

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
                    samples,
                    self._sample_rate,
                    device=self._output_device,
                    progress_callback=lambda played, total: self.transmission_progress.emit(played, total),
                    stop_event=self._stop_event,
                )
                playback_succeeded = not self._stop_event.is_set()
        except sd.PortAudioError:
            self.error.emit("Audio device disconnected during transmission.")
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Playback failed: {exc}")
        finally:
            # ALWAYS unkey, even on error or stop, so the rig never gets
            # left in a stuck-keyed state.
            try:
                rig.set_ptt(False)
            except RigError as exc:
                self.error.emit(f"Could not unkey rig: {exc}")

        return playback_succeeded

    def request_stop(self) -> None:
        """Abort an in-flight transmission. Safe to call from any thread.

        Sets the stop flag and yanks PortAudio out of ``sd.wait()`` so
        the playback unwinds immediately. The worker thread then drops
        PTT and emits ``transmission_aborted``.
        """
        self._stop_event.set()
        output_stream.stop()

    def wait_for_stop(self, timeout: float) -> bool:
        """Block until the stop flag is set or *timeout* seconds elapse.

        Returns ``True`` if the flag was set within the timeout, ``False``
        if the timeout expired first. Safe to call from any thread.

        Intended for ``closeEvent`` so the TX worker can unwind out of
        ``play_blocking`` before the owning ``QThread`` is quit.
        """
        return self._stop_event.wait(timeout=timeout)

    def _watchdog_fire(self) -> None:
        """Called by the watchdog timer when TX exceeds ``_MAX_TX_DURATION_S``.

        Safe to call from the timer's background thread: signals are Qt
        queued connections (delivered on the GUI thread) and
        ``request_stop`` is explicitly thread-safe.

        Emits ``watchdog_fired`` instead of ``error`` so the GUI can
        display a persistent watchdog message that isn't immediately
        clobbered by the subsequent ``transmission_aborted`` signal.
        """
        self._watchdog_triggered = True
        self.watchdog_fired.emit()
        self.request_stop()


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
    image_progress = Signal(object, object, int, int, int)  # (PIL.Image, Mode, vis_code, lines_decoded, lines_total)
    image_complete = Signal(object, object, int)  # (PIL.Image, Mode, vis_code)
    status_update = Signal(str)  # periodic progress text
    error = Signal(str)

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        flush_samples: int | None = None,
        weak_signal: bool = False,
        final_slant_correction: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sample_rate = sample_rate
        self._weak_signal = weak_signal
        self._final_slant_correction = final_slant_correction
        self._cancel_event = threading.Event()
        self._decoder = Decoder(sample_rate, weak_signal=weak_signal)
        self._decoder.set_cancel_event(self._cancel_event)
        self._scratch: list["NDArray[np.float64]"] = []
        self._scratch_samples: int = 0
        self._total_samples: int = 0
        self._decoding: bool = False
        self._input_gain: float = 1.0
        self._tx_active: bool = False
        self._flush_samples: int = (
            flush_samples
            if flush_samples is not None
            else _RX_FLUSH_SAMPLES_DEFAULT
        )

    def set_input_gain(self, gain: float) -> None:
        """Set the software input gain (1.0 = unity). Thread-safe."""
        self._input_gain = gain

    def set_weak_signal(self, enabled: bool) -> None:
        """Enable or disable weak-signal VIS detection mode. Thread-safe."""
        self._weak_signal = enabled
        self._decoder = Decoder(self._sample_rate, weak_signal=enabled)
        self._decoder.set_cancel_event(self._cancel_event)

    def set_final_slant_correction(self, enabled: bool) -> None:
        """Enable or disable final one-shot re-decode with slant correction.

        When *enabled* is ``True``, ``_dispatch`` runs ``decode_wav`` on the
        retained raw buffer after a complete image and uses the result if the
        mode matches.  This applies a global least-squares slant fit that can
        improve images from rigs with slight clock drift, but degrades images
        from weak/noisy signals where false-positive sync candidates corrupt
        the polyfit.  Off by default (progressive decode is used as-is).

        Thread-safe: plain bool assignment is atomic under the GIL.
        """
        self._final_slant_correction = enabled

    @Slot(bool)
    def set_tx_active(self, active: bool) -> None:
        """Gate the decoder while a transmission is in progress.

        When ``active`` is ``True``, ``feed_chunk`` discards all incoming
        audio so the radio's own transmitted signal is never fed into the
        decoder (self-decode through RF/audio loopback).

        When ``active`` becomes ``False`` (TX ended), the scratch buffer
        and decoder state are reset so the next RX attempt starts clean —
        no partial frame from the TX period bleeds through.

        Called via queued connection from the GUI thread so the flag flip
        always lands on this worker's event loop.
        """
        self._tx_active = active
        if not active:
            # Clear any audio that bled in from the TX period and start fresh.
            self.reset()

    def set_sample_rate(self, sample_rate: int) -> None:
        """Update the sample rate and reconstruct the internal Decoder.

        Should be called only when capture is not running — the new
        Decoder discards any in-flight buffered audio. The caller
        (``MainWindow._open_settings``) shows a status-bar notice asking
        the user to restart capture when the rate changes mid-session.
        """
        self._sample_rate = sample_rate
        self._decoder = Decoder(sample_rate, weak_signal=self._weak_signal)
        self._decoder.set_cancel_event(self._cancel_event)
        self._scratch.clear()
        self._scratch_samples = 0

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

        While TX is active (``_tx_active`` is ``True``) chunks are
        discarded silently so the radio's own transmitted signal is
        never fed into the decoder (self-decode prevention, bug R-2).
        """
        if self._tx_active:
            return

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

        if self._input_gain != 1.0:
            arr = arr * self._input_gain
        self._scratch.append(arr)
        self._scratch_samples += arr.size
        self._total_samples += arr.size

        if self._scratch_samples >= self._flush_samples:
            self._flush()

    def request_cancel(self) -> None:
        """Interrupt any in-flight decode. Safe to call from any thread.

        Sets the cancel event so the decoder bails at the next checkpoint
        (after bandpass, after Hilbert transform, after sync detection, or
        between pixel-decoder rows). The queued ``reset()`` slot clears
        the event once it executes, re-arming the decoder for the next
        transmission.

        This mirrors ``TxWorker.request_stop()``: a plain Python method
        (not a slot) that touches only a ``threading.Event``, making it
        safe to call from the GUI thread while the worker thread is busy.
        """
        self._cancel_event.set()

    @Slot()
    def reset(self) -> None:
        """Drop the scratch buffer and reset the decoder state.

        Called when the user clicks "Clear" or switches input device.
        After ``reset`` the next ``feed_chunk`` begins a fresh hunt
        for a VIS header.
        """
        self._scratch.clear()
        self._scratch_samples = 0
        self._total_samples = 0
        self._decoding = False
        self._decoder.reset()
        # Re-arm after any cancel that was in flight.  This runs on the
        # worker thread so it's sequentially after _flush() has returned.
        self._cancel_event.clear()

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

        if not events and not self._decoding:
            # No decode yet — show progress so the user knows we're alive.
            secs = self._total_samples / self._sample_rate
            self.status_update.emit(
                f"Listening… {secs:.0f}s buffered, waiting for signal."
            )

        decoded = False
        for event in events:
            self._dispatch(event)
            if isinstance(event, ImageStarted):
                self._decoding = True
            elif isinstance(event, ImageComplete):
                self._decoding = False
                decoded = True
        if decoded:
            self._total_samples = 0

    def _dispatch(self, event: object) -> None:
        if isinstance(event, ImageStarted):
            self.image_started.emit(event.mode, event.vis_code)
        elif isinstance(event, ImageProgress):
            self.image_progress.emit(
                event.image,
                event.mode,
                event.vis_code,
                event.lines_decoded,
                event.lines_total,
            )
        elif isinstance(event, ImageComplete):
            # Always free the retained raw audio buffer (memory, regardless of
            # whether we re-decode from it).
            raw = self._decoder.consume_last_buffer()
            final_image = event.image
            if self._final_slant_correction:
                # Opt-in: run a full single-pass re-decode with global
                # least-squares slant correction.  Helpful for clean signals
                # with clock drift; harmful for weak/noisy signals where
                # false-positive syncs corrupt the polyfit.
                try:
                    if raw is not None and isinstance(raw, np.ndarray) and raw.size > 0:
                        result = decode_wav(raw, self._sample_rate)
                        if result is not None and result.mode == event.mode:
                            final_image = result.image
                except Exception as exc:  # noqa: BLE001
                    _log.debug(
                        "re-decode (slant correction) failed, using progressive result: %s",
                        exc,
                        exc_info=True,
                    )
            self.image_complete.emit(final_image, event.mode, event.vis_code)
        elif isinstance(event, DecodeError):
            self.error.emit(event.message)


__all__ = ["DEFAULT_PTT_DELAY_S", "RxWorker", "TxWorker"]
