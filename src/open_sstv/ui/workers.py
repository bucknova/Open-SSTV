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
hands them to ``core.decoder.Decoder``. The worker accumulates chunks
locally and flushes to ``Decoder.feed`` on a cadence that depends on
which decode path is active *and* whether the decoder is IDLE or
DECODING.  On the incremental path we use 1 s while hunting for VIS
(to keep the unknown-VIS false-positive rate low on noisy acoustic
inputs) and drop to 0.1 s once VIS has locked (so the UI paints rows
as they complete, MMSSTV-style).  The batch fallback uses a flat 2 s
to amortise its O(N²) reprocessing cost.  See the
``_DECODE_FLUSH_INTERVAL_S_*`` constants below for the full rationale.

In DECODING state (``incremental_decode=True``, the default since v0.1.24)
the ``Decoder`` passes only new audio to the per-mode incremental backend,
so each flush is O(1 line period) regardless of transmission length. A
36 s Robot 36 or 110 s Scottie S1 stays comfortably ahead of real-time
on Pi-class hardware.

``DecoderEvent`` values from ``Decoder.feed`` are translated into
Qt signals (``image_started``, ``image_complete``, ``error``) so UI
code can connect to them directly without importing the core
dataclasses.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, QTimer, Signal, Slot

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
from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.radio.base import ManualRig, Rig
from open_sstv.radio.exceptions import RigError

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from PIL.Image import Image as PILImage


#: Silence inserted between the end of the SSTV image audio and the CW ID.
#: 500 ms gives the receiver AGC time to settle before the CW tone starts.
_CW_GAP_S: float = 0.500

#: Silent tail appended to the TCI TX buffer so the server's audio pipeline
#: drains completely before _play_via_tci returns and PTT drops.  AetherSDR's
#: measured pipeline delay is ~700–800 ms; 1.5 s gives comfortable headroom.
_TCI_PIPELINE_TAIL_S: float = 1.5

#: How many chunks to send between periodic rig-health checks in _play_via_tci.
#: At 100 ms/chunk this is a ~1 s poll interval, matching the rig poll timer.
_TCI_HEALTH_CHECK_INTERVAL: int = 10

#: Default delay between keying PTT and starting audio playback. Most
#: rigs need ~50–200 ms for the relay to settle and the SSB filter to
#: open. 200 ms is on the safe side; advanced users can override per-rig
#: in settings (Phase 3).
DEFAULT_PTT_DELAY_S = 0.2

#: How long to accumulate audio in ``RxWorker`` before flushing a
#: batch to ``Decoder.feed``.  Three values, selected dynamically per
#: flush based on (a) which decode path is active and (b) whether the
#: decoder is currently in IDLE (hunting VIS) or DECODING (painting
#: lines).
#:
#: **Incremental path, IDLE** — 1 s.  The 0.1 s "paint-as-you-go"
#: cadence below would call ``detect_vis`` 10× per second on a rolling
#: noisy pre-VIS buffer.  Each call is an independent chance for a
#: noise-triggered *unknown-VIS false positive*, and on that path
#: ``Decoder._feed_idle`` trims the buffer past ``vis_end`` — which can
#: mutilate the real VIS arriving moments later.  Keeping the IDLE
#: hunt at 1 s matches the pre-v0.2.6 behaviour and preserves acoustic
#: (speaker→mic) VIS detection.
#:
#: **Incremental path, DECODING** — 0.1 s.  Per-line work inside
#: ``Decoder.feed``, so total CPU is independent of how often we flush.
#: Short interval paints rows as they complete, matching the MMSSTV /
#: slowrx "watch it arrive" feel.  A 0.1 s cadence gives roughly one
#: flush per scan line on short modes (Robot 36 ~150 ms, PD-50
#: ~195 ms) and sub-line granularity on long modes (Pasokon P7
#: ~820 ms).  VIS has already locked by this point, so the IDLE
#: false-positive risk does not apply.
#:
#: **Batch fallback path** — 2 s, independent of IDLE/DECODING.
#: The decoder reprocesses the *entire* growing buffer on every
#: flush (O(N) per flush → O(N²) total), so the flush interval is
#: the dominant CPU knob.  A 2 s interval roughly halves the total
#: work on long Scottie-family receives compared with 1 s, at the
#: cost of one extra second of update latency.  Users on the batch
#: path are there as an opt-in diagnostic fallback, so we prioritise
#: throughput over cadence; anyone who wants a live preview uses the
#: default (incremental) path.
#:
#: Tune these constants rather than hunting for magic numbers in tests.
_DECODE_FLUSH_INTERVAL_S_INCREMENTAL_IDLE: float = 1.0
_DECODE_FLUSH_INTERVAL_S_INCREMENTAL_DECODING: float = 0.1
_DECODE_FLUSH_INTERVAL_S_BATCH: float = 2.0

#: Per-transmission RX decoder watchdog: total-elapsed multiplier.
#: If we've been in DECODING state for ``mode.total_duration_s × this``
#: seconds without completing, the signal has almost certainly faded
#: and the decoder is hunting for sync pulses that will never arrive.
#: 1.5 gives a Pasokon P7 ~609 s (10 min) — enough slack for the slow
#: modes while still bailing out on a stuck short-mode transmission.
_RX_WATCHDOG_TOTAL_MULTIPLIER: float = 1.5

#: Per-transmission RX decoder watchdog: total-elapsed floor.
#: The multiplier is generous but still trips too aggressively on short
#: modes (Robot 36 × 1.5 = 54 s).  Add a fixed floor so Robot 36 gets
#: at least this many seconds of patience on a noisy signal.
_RX_WATCHDOG_TOTAL_FLOOR_S: float = 15.0

#: Per-line RX decoder watchdog: N × ``spec.line_time_ms`` without a
#: new ``ImageProgress`` event ⇒ the signal has faded mid-image.
#: This multiplier alone gives sub-second timeouts on fast modes, so
#: the floor below always dominates in practice — the multiplier is
#: kept for documentation symmetry with the total-elapsed guard.
_RX_WATCHDOG_LINE_MULTIPLIER: float = 5.0

#: Minimum absolute "no-progress" timeout regardless of mode.
#: Default is 10 s — enough to ride out a brief signal dip without
#: keeping a decode alive so long that the resulting image is useless.
#: Users can raise this via Settings → Receive → No-progress timeout
#: if their propagation conditions have longer QSB fading cycles.
#: The total-elapsed watchdog (mode duration × 1.5, 15 s floor) still
#: fires independently regardless of this value.
_RX_WATCHDOG_LINE_FLOOR_S: float = 5.0

#: Wall-clock tick interval for the independent watchdog check, in
#: milliseconds.  The original v0.1.36 watchdog only ran inside
#: ``_flush``, which in turn only runs when audio chunks arrive.  If
#: the PortAudio stream goes quiet for any reason — USB device sleeps,
#: Bluetooth link drops, the OS suspends the audio subsystem briefly,
#: or simply a *very* deep signal fade where the driver produces long
#: stretches of exactly-zero samples that don't fill a flush buffer —
#: no flushes fire and the watchdog never ticks.  A dedicated QTimer
#: on the RxWorker thread ensures the watchdog is checked on wall-
#: clock time regardless of audio state.  2 s is snappy enough to
#: deliver timely resets while staying well under the line-budget
#: floor so there's no risk of doubling-up with flush-driven checks.
_RX_WATCHDOG_TICK_MS: int = 2000

#: After a watchdog trip, suppress the routine "Listening… Xs
#: buffered, waiting for signal." status updates for this many
#: seconds so the user has time to read the *"Decode timed out —
#: kept partial N/M lines."* message before it gets overwritten by
#: the next idle-state flush.  10 s is a comfortable reading
#: window without stalling real user feedback indefinitely.
_RX_POST_WATCHDOG_COOLDOWN_S: float = 10.0

#: Hard upper bound on the encode / banner-stamp / CW-append stage.
#: Encoding is CPU-bound and finishes in ~100 ms even for the longest
#: Multiplicative margin on top of the expected wall-clock playback
#: duration (PTT delay + samples / sample_rate).  20 % absorbs OS-level
#: audio jitter — driver buffer underruns, scheduling hiccups, brief
#: USB / Bluetooth stalls — without leaving so much slack that a
#: genuinely-stuck transmitter holds the rig keyed for minutes.
_PLAYBACK_WATCHDOG_MARGIN: float = 1.20

#: Lower bound on the playback watchdog.  Short modes (Robot 36 at
#: 36 s, the 5 s test tone) would otherwise have very tight margins
#: where a single audio underrun could trip a false positive: 36 s × 1.2
#: leaves only 7 s of headroom.  30 s is roughly the slowest plausible
#: end-to-end latency for any reasonable system and gives every short
#: TX a real safety budget.
_PLAYBACK_WATCHDOG_FLOOR_S: float = 30.0

#: How many times to attempt the post-TX PTT unkey.  The first attempt
#: uses the existing control link; each retry closes and re-opens the
#: backend first, because the dominant failure mode (USB-serial unplug,
#: rigctld/TCI connection drop mid-TX) kills the old socket/port and a
#: fresh ``open()`` is the only way set_ptt(False) can ever succeed.
#: A keyed radio with no operator at it is the worst failure mode this
#: app has, so we spend a few seconds trying before giving up.
_UNKEY_ATTEMPTS: int = 3

#: Pause between unkey attempts.  Long enough for a replugged USB-CAT
#: adapter to re-enumerate or a restarted rigctld to start listening;
#: short enough that the TX worker thread frees up promptly (closeEvent
#: only waits 3 s for this thread before falling back to
#: ``emergency_unkey``).
_UNKEY_RETRY_DELAY_S: float = 1.0

#: How often the background rig-health monitor pings the rig during TX.
#: Matches the ~1 s cadence the old inline periodic_check had.
_RIG_HEALTH_INTERVAL_S: float = 1.0

#: How long to wait for the health-monitor thread to finish at TX end.
#: One blocked CAT round-trip is at most ~2 s (rigctld socket timeout),
#: so 3 s lets a wedged ping drain before we touch the rig to unkey.
_RIG_HEALTH_JOIN_TIMEOUT_S: float = 3.0


class _RigHealthMonitor:
    """Background rig pinger so the TX audio loop never blocks on CAT I/O.

    H4 (v0.3 audit): the rig health check used to run *inline* in the
    playback write loop via ``periodic_check``.  A wedged rigctld daemon
    or stalled USB-CAT chain made ``get_ptt()`` block for its full
    socket timeout (2 s), pausing audio output mid-chunk — an audible
    buffer underrun in the transmitted image.  The ping now runs on its
    own daemon thread; the write loop's ``periodic_check`` merely reads
    the failure flag, which is instant.

    The monitor stops pinging after the first failure — once the link
    is dead there is nothing more to learn, and the unkey path needs
    the (serial) backend lock free.
    """

    def __init__(self, rig: Rig) -> None:
        self._rig = rig
        self._stop = threading.Event()
        self.failure: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, name="sstv-rig-health", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor to exit and wait for the ping to drain."""
        self._stop.set()
        self._thread.join(timeout=_RIG_HEALTH_JOIN_TIMEOUT_S)

    def _run(self) -> None:
        while not self._stop.wait(_RIG_HEALTH_INTERVAL_S):
            try:
                self._rig.get_ptt()
            except Exception as exc:  # noqa: BLE001
                # Anything raised here means the link is unusable for
                # monitoring — RigError/OSError on unplug, but also any
                # unexpected parse error.  Catching narrowly would let
                # an oddball exception kill this thread silently and
                # leave TX running unmonitored.
                self.failure = exc
                return


def _compute_playback_watchdog_s(
    samples_n: int, sample_rate: int, ptt_delay_s: float
) -> float:
    """Return the watchdog budget for playback of ``samples_n`` samples.

    Per-transmission watchdog formula introduced after the v0.1.27 audit:
    the budget is the actual expected wall-clock TX duration (PTT delay
    plus the encoded sample count divided by the sample rate) scaled by
    ``_PLAYBACK_WATCHDOG_MARGIN``, with a hard ``_PLAYBACK_WATCHDOG_FLOOR_S``
    floor for short transmissions.

    The previous design (single fixed 600 s constant) gave Pasokon P7
    coverage and Robot 36 ten minutes of stuck-rig exposure — a
    regulatory liability.  This formula scales automatically with mode
    duration, CW tail length, and PTT delay setting, so a stuck Robot 36
    aborts at ~73 s instead of 600 s and a Pasokon P7 still has its full
    ~500 s budget.
    """
    if sample_rate <= 0:
        # Degenerate input — fall back to the floor so we still bound
        # PTT exposure rather than dividing by zero.
        return _PLAYBACK_WATCHDOG_FLOOR_S
    expected_tx_s = ptt_delay_s + samples_n / sample_rate
    return max(_PLAYBACK_WATCHDOG_FLOOR_S, expected_tx_s * _PLAYBACK_WATCHDOG_MARGIN)

#: Default duration for the ALC/linearity test tone, in seconds.
_TEST_TONE_DURATION_S: float = 5.0

#: Two-tone test frequency *defaults* (Hz).  700 + 1900 Hz is the ARRL
#: standard for SSB ALC / intermodulation testing — what every operator
#: expects unless they explicitly want something else.  ``AppConfig.
#: test_tone_freq_lo`` / ``test_tone_freq_hi`` override these at runtime
#: (audit L-2); when neither is configured, ``_make_two_tone`` falls
#: back to these constants so the legacy callers and tests keep working.
_TEST_TONE_FREQ_LO: float = 700.0
_TEST_TONE_FREQ_HI: float = 1900.0


def _make_two_tone(
    sample_rate: int,
    duration_s: float,
    *,
    freq_lo: float = _TEST_TONE_FREQ_LO,
    freq_hi: float = _TEST_TONE_FREQ_HI,
) -> NDArray[np.int16]:
    """Generate a two-tone test signal as int16 PCM.

    Frequencies default to the ARRL twin-tone standard (700 + 1900 Hz)
    used for SSB linearity testing.  Callers (TxWorker) override from
    ``AppConfig.test_tone_freq_lo`` / ``test_tone_freq_hi`` so operators
    on narrower passbands can pick frequencies inside their audio band
    without code changes (audit L-2).

    The two equal-amplitude sine waves are summed and the result is
    scaled so the *peak* of the sum sits at −1 dBFS.  Each component
    therefore has an amplitude of ``0.5 × 10^(−1/20) ≈ 0.446`` of full
    scale.

    This is a calibration signal, not a linearity-critical SSTV image,
    so we want maximum drive into the radio.  At −1 dBFS peak the
    two-tone average power is −7 dBFS, which is enough to light ALC on
    the IC-7300 and similar radios even with a conservatively set USB
    MOD Level.  The user's TX output-gain slider provides additional
    headroom control if needed.
    """
    n = int(sample_rate * duration_s)
    t = np.arange(n, dtype=np.float64) / sample_rate
    # Peak of two equal-amplitude sines can reach 2.0, so each is scaled
    # to half the −1 dBFS ceiling.
    amplitude = 0.5 * (10 ** (-1.0 / 20.0))  # ≈ 0.4467
    sig = np.sin(2.0 * np.pi * freq_lo * t)
    sig += np.sin(2.0 * np.pi * freq_hi * t)
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
    #: Centralized exception channel.  Carries the originating slot name
    #: and the live ``Exception`` object so MainWindow can log a full
    #: traceback while still showing a short status-bar message.  Use this
    #: instead of swallowing failures with a bare ``pass`` — the UI handler
    #: is responsible for deciding whether to surface to the user.
    error_occurred = Signal(str, object)  # (slot_name, exception)
    #: v0.2.8: emitted once the exact image that will be transmitted is
    #: finalised (after any TX-banner compositing, before SSTV encoding).
    #: Carries the PIL image and the Mode so the UI can auto-save the
    #: actual transmitted bits — including the banner, if enabled —
    #: without reproducing the compositing logic.
    tx_image_prepared = Signal(object, object)  # (image, mode)
    #: Emits the watchdog budget (seconds) that fired so the UI can
    #: show "exceeded N s" without hardcoding the value or having to
    #: read internal worker state.  Value is the per-transmission
    #: budget computed by :func:`_compute_playback_watchdog_s` (or the
    #: fixed encode-stage budget if the encoder wedged).
    watchdog_fired = Signal(float)
    error = Signal(str)
    #: Emitted when the rig's serial/TCP link dies during TX (USB unplug
    #: mid-transmission).  Delivered to the GUI thread so ``_on_radio_disconnected``
    #: can update the radio panel immediately, without waiting for TX to finish.
    rig_disconnected = Signal()
    #: Emitted with each audio chunk during playback so the waterfall can
    #: show the TX signal.  Chunk is int16 PCM (post-gain, pre-device-write).
    tx_audio_chunk = Signal(object)

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
        # v0.3.13: removed the ``_v3_template_active`` gating field.
        # Previously, when a v0.3 template was composited into the image,
        # the banner stamp was skipped to avoid double-stamping over the
        # template's own text layers.  User feedback (Kevin/W0AEZ): banner
        # enabled in Settings should always stamp, regardless of template
        # — single source of truth for "do I want a banner".  Operators
        # who want template-only output can disable the banner in Settings.
        self._stop_event = threading.Event()
        self._watchdog_triggered: bool = False
        #: Monotonically-increasing transmission id, incremented at the
        #: top of ``transmit`` / ``transmit_test_tone``.  Captured by the
        #: watchdog when it's scheduled and re-checked when it fires —
        #: ``threading.Timer.cancel()`` is best-effort, so a watchdog that
        #: was already executing when cancel() was called still completes;
        #: without this guard a stale fire from a prior transmission could
        #: set ``_stop_event`` and call the global ``output_stream.stop()``
        #: just as the next ``transmit()`` is starting, racing the
        #: ``_stop_event.clear()`` at entry and aborting an unrelated TX
        #: (or cancelling a concurrent test-tone via the global sd.stop).
        #: Guarded by ``_tx_id_lock`` because the timer thread reads it
        #: while the worker thread writes it.
        self._tx_id_lock = threading.Lock()
        self._tx_id: int = 0
        #: Set while NO transmission is in flight (set at construction,
        #: cleared on entry to transmit/transmit_test_tone, re-set in
        #: their finally).  ``wait_for_idle`` waits on THIS — the v0.4.0
        #: audit (high #5) found closeEvent's grace period waiting on
        #: ``_stop_event``, which ``request_stop`` had just set, so the
        #: "let TX unwind" wait always returned immediately and the
        #: worker was still mid-unkey when the thread was quit.
        self._idle_event = threading.Event()
        self._idle_event.set()
        # When set, TX audio is routed via this TCI connection instead of
        # PortAudio.  Accepts any object with a send_tx_audio_chunk() method
        # (duck-typed so workers.py doesn't import from radio.tci).
        self._tci_connection: object | None = None
        # Sender-side waterfall gate: when False, tx_audio_chunk is not
        # emitted, avoiding a cross-thread signal per chunk (~10 Hz) during
        # a multi-minute TX when the waterfall window is hidden.
        self._waterfall_active: bool = False
        # L-2: two-tone test-signal frequencies.  Defaults match the
        # ARRL twin-tone standard; MainWindow updates these from
        # AppConfig when the Settings dialog saves.
        self._test_tone_freq_lo: float = _TEST_TONE_FREQ_LO
        self._test_tone_freq_hi: float = _TEST_TONE_FREQ_HI

    def set_test_tone_freqs(self, lo_hz: float, hi_hz: float) -> None:
        """Update the two-tone test frequencies (L-2).

        Called by MainWindow when the Settings dialog saves.  Range
        clamping is done in ``AppConfig.__post_init__`` so the values
        reaching this slot are already in [300, 3000] Hz and ordered
        ``lo < hi``.
        """
        self._test_tone_freq_lo = float(lo_hz)
        self._test_tone_freq_hi = float(hi_hz)

    def set_tci_connection(self, conn: object | None) -> None:
        """Set (or clear) the TCI connection used for TX audio output.

        When *conn* is not ``None``, ``transmit()`` sends audio via the TCI
        WebSocket instead of PortAudio.  *conn* must expose a
        ``send_tx_audio_chunk(samples_f32: np.ndarray)`` method matching
        ``TciConnection.send_tx_audio_chunk``.

        Call with ``None`` when the TCI rig is disconnected to revert to
        the PortAudio output path.
        """
        self._tci_connection = conn

    def set_waterfall_active(self, active: bool) -> None:
        """Enable or disable tx_audio_chunk emission for the waterfall.

        Called by MainWindow when the waterfall window is shown or hidden.
        Skipping the emit when the window is invisible avoids ~10 cross-thread
        signals per second through a multi-minute SSTV transmission.
        """
        self._waterfall_active = bool(active)

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

    @Slot(int)
    def set_sample_rate(self, sample_rate: int) -> None:
        """Update the sample rate used for encoding and playback.

        Takes effect on the next ``transmit()`` call. Decorated
        ``@Slot(int)`` so MainWindow can dispatch via a queued signal
        connection, sequencing the change onto the TX worker's own
        event loop (OP-09).  In practice the Settings dialog disables
        Settings during TX so a mid-TX rate change can't happen, but
        the queued slot makes the invariant explicit.
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
        """Best-effort direct PTT-off, independent of the playback thread.

        Two callers, both needing an unkey that does NOT wait on the TX
        worker unwinding out of ``play_blocking``:

        * closeEvent, if the TX thread doesn't join within the timeout;
        * the remote dead-man's-switch (``_remote_tx_unkey``), so a worker
          wedged in a blocking ``stream.write()`` — where ``request_stop``'s
          abort can't reach it — still can't leave the rig keyed.

        Thread-safe: takes ``_rig_lock`` to snapshot the backend and calls
        ``set_ptt(False)``, whose own serial lock serialises against any
        concurrent rig I/O.  Safe to call from the GUI thread, the tick
        thread, or a request thread.  A wedged worker holds no rig lock, so
        this never contends with it.  Ignores all errors so it never blocks
        the caller; failures still emit ``error_occurred`` so a debug build
        (or a test on the queue) records them — a stuck-keyed rig is a real
        safety concern regardless of who is unkeying.
        """
        try:
            with self._rig_lock:
                self._rig.set_ptt(False)
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit("emergency_unkey", exc)

    @Slot(object, object)
    def transmit(self, image: PILImage, mode: Mode) -> None:
        """Encode and transmit one image. Worker-thread entry point.

        Thin wrapper owning the idle flag: consumers of
        ``wait_for_idle`` (closeEvent, deferred rig teardown) need a
        guarantee that covers the WHOLE transmission — including the
        finally-block unkey retries — not just playback.
        """
        self._idle_event.clear()
        try:
            self._transmit_impl(image, mode)
        finally:
            self._idle_event.set()

    def _transmit_impl(self, image: PILImage, mode: Mode) -> None:
        """Encode and transmit one image (body of :meth:`transmit`).

        Always emits exactly one of ``transmission_complete`` or
        ``transmission_aborted`` per call (or, on early encode/PTT
        failure, only ``error``).

        H11: the original design had a two-stage watchdog — stage 1
        covering encode, stage 2 covering keyed playback.  Stage 1 was
        removed because PySSTV's ``encode()`` is a synchronous blocking
        call that doesn't honour ``_stop_event``, so the encode-stage
        timer firing didn't actually unblock anything — it just set
        the stop flag and waited for ``encode()`` to return naturally,
        at which point the post-encode check would notice and abort.
        Encoding is fast (~100 ms even for Pasokon P7) and not network-
        bound, so the lack of a timer here is acceptable: a truly
        wedged encoder would mean Python itself is hung, which the
        watchdog couldn't help with anyway.  Only the keyed-playback
        watchdog remains, since that's where stuck PTT actually matters.

        The post-encode ``_stop_event`` check is still useful: the user
        can press Stop during the (brief) encode window and we honour
        it without keying the rig.

        H4: a per-call ``tx_id`` is captured at the top of this method
        and passed to the playback watchdog so a stale watchdog firing
        from a prior transmission cannot race the next ``transmit()``
        call's ``_stop_event.clear()``.
        """
        self._stop_event.clear()
        self._watchdog_triggered = False
        # Advance the TX id so any in-flight watchdog from a previous
        # transmission becomes "stale" and no-ops if it fires now.
        with self._tx_id_lock:
            self._tx_id += 1
            this_tx_id = self._tx_id

        # Snapshot the rig reference once so a mid-TX call to set_rig()
        # (e.g. user disconnects the radio) cannot swap the backend between
        # set_ptt(True) and set_ptt(False), which would leave the real rig
        # stuck keyed via a no-op ManualRig.set_ptt(False).
        with self._rig_lock:
            rig = self._rig

        # === Encode + CW + gain (no watchdog; see docstring H11) ===
        try:
            # --- Apply TX banner (if enabled) ---
            # OP2-01: catch ValueError (content_height <= 0) so a too-small
            # image never silently escapes transmit() without emitting error.
            # v0.3.13: banner is no longer gated on v0.3 template state — if
            # the user enabled the banner in Settings, it stamps regardless
            # of what the TxPanel composited.  Templates with their own
            # header text may visually clash; operators choose between
            # banner and template by toggling the Settings checkbox.
            try:
                if self._tx_banner_enabled:
                    from open_sstv import __version__
                    from open_sstv.core.banner import apply_tx_banner, scaled_banner_params
                    _bh, _fs = scaled_banner_params(self._tx_banner_size, image.width)
                    image = apply_tx_banner(
                        image,
                        __version__,
                        self._tx_banner_callsign,
                        self._tx_banner_bg_color,
                        self._tx_banner_text_color,
                        banner_height=_bh,
                        font_size=_fs,
                    )
            except Exception as exc:  # noqa: BLE001
                self.error.emit(f"TX banner failed: {exc}")
                return

            # v0.2.8: announce the finalised image so the UI can auto-save
            # it.  Emitted *after* banner compositing so the saved copy is
            # byte-identical to what the encoder actually sees.  Regular
            # TX transmission_complete still fires at the end of playback
            # — this signal is purely informational for the auto-save path.
            self.tx_image_prepared.emit(image, mode)

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
                    # M9: ramp down the last 5 ms of SSTV before the silence
                    # gap.  PySSTV's encoder ends on a sync-tone tail at full
                    # amplitude; a hard zero-cut at the SSTV→silence boundary
                    # can leak a faint key-click into the RF passband at
                    # high TX gain.  Symmetric with make_cw's 5 ms key-click
                    # envelope: a half-cosine ramp from 1.0 → 0.0 over the
                    # last 5 ms.  Inaudible to the listener, eliminates the
                    # spectral edge.
                    _RAMP_MS = 5
                    ramp_n = int(self._sample_rate * _RAMP_MS / 1000)
                    if samples.size >= ramp_n and ramp_n > 0:
                        ramp = np.cos(
                            np.linspace(0.0, np.pi / 2, ramp_n, dtype=np.float64)
                        )
                        tail = samples[-ramp_n:].astype(np.float64) * ramp
                        samples = samples.copy()
                        samples[-ramp_n:] = tail.astype(samples.dtype)
                    samples = np.concatenate([samples, gap, cw])
                else:
                    _log.warning(
                        "CW ID is enabled but callsign is empty — "
                        "skipping CW tail. Set callsign in Settings."
                    )
        except BaseException:
            # Defensive: even if encode raised something exotic, make sure
            # we don't accidentally proceed into playback.  Specific
            # ValueError / Exception cases above already emitted an error
            # signal and returned; this catches anything that escapes.
            raise

        # User pressed Stop during the (brief) encode window — abort
        # without keying the rig.  Encode is fast so this is rare, but
        # honouring Stop here means no extra PTT carrier and no surprise
        # CW tail on a TX the user already cancelled.
        if self._stop_event.is_set():
            _log.debug("TX stop requested during encode — aborting")
            self.transmission_aborted.emit()
            return

        # === Per-transmission playback watchdog ===
        # M8: when using TCI, _play_via_tci appends _TCI_PIPELINE_TAIL_S of
        # silent samples to drain the server-side audio pipeline before
        # the caller drops PTT.  The actual paced playback is therefore
        # longer than samples.size / sample_rate; include the tail in
        # the budget so the watchdog can't false-fire on the silence.
        effective_samples = samples.size
        if self._tci_connection is not None:
            effective_samples += int(_TCI_PIPELINE_TAIL_S * self._sample_rate)
        playback_budget_s = _compute_playback_watchdog_s(
            effective_samples, self._sample_rate, self._ptt_delay_s
        )
        playback_watchdog = threading.Timer(
            playback_budget_s,
            self._watchdog_fire,
            args=[playback_budget_s, this_tx_id],
        )
        # L13: daemon=True so a still-pending watchdog from a TX cycle
        # caught mid-shutdown doesn't keep the interpreter alive past
        # closeEvent (the cancel below covers the normal path; this is
        # the belt-and-braces for the rare in-flight case).
        playback_watchdog.daemon = True
        playback_watchdog.start()
        try:
            result = self._run_tx(samples, rig)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"TX failed: {exc}")
            self.transmission_aborted.emit()
            return
        finally:
            playback_watchdog.cancel()

        if result is None:
            return  # PTT failed; error already emitted
        if self._stop_event.is_set():
            _log.debug("TX stopped by request — aborting")
            self.transmission_aborted.emit()
        elif result:
            self.transmission_complete.emit()
        else:
            # Playback error — error signal already emitted; still signal
            # the GUI that TX ended so it can reset to idle.
            _log.debug("TX _run_tx returned False (playback did not complete)")
            self.transmission_aborted.emit()

    @Slot()
    def transmit_test_tone(self) -> None:
        """Generate and transmit a two-tone test signal. Worker-thread entry point.

        Thin wrapper owning the idle flag — see :meth:`transmit`.
        """
        self._idle_event.clear()
        try:
            self._test_tone_impl()
        finally:
            self._idle_event.set()

    def _test_tone_impl(self) -> None:
        """Two-tone test signal (body of :meth:`transmit_test_tone`).

        Produces ``_TEST_TONE_DURATION_S`` seconds of 700 Hz + 1900 Hz at
        −1 dBFS peak.  Follows the identical PTT-key → ptt_delay → play →
        PTT-unkey sequence as ``transmit()``, including the watchdog, stop
        button, and gain controls.

        Test tone has no encode stage and a fixed 5 s duration, so it
        only needs the playback watchdog (the floor in
        ``_compute_playback_watchdog_s`` ensures it gets a sensible 30 s
        budget rather than the literal 6 s the formula would otherwise
        produce).
        """
        self._stop_event.clear()
        self._watchdog_triggered = False
        with self._tx_id_lock:
            self._tx_id += 1
            this_tx_id = self._tx_id

        with self._rig_lock:
            rig = self._rig

        # Generate the test signal upfront so we can size the watchdog
        # against the actual sample count.  We deliberately do NOT apply
        # self._output_gain here — _run_tx(live_gain=True) re-reads the
        # gain each playback chunk so the slider behaves as a live ALC-
        # calibration knob during the 5 s tone, matching the user-facing
        # promise in the README and the Settings tooltip.  Regular SSTV
        # TX keeps the pre-scale path (stable envelope for the whole
        # image — see transmit()).
        samples = _make_two_tone(
            self._sample_rate,
            _TEST_TONE_DURATION_S,
            freq_lo=self._test_tone_freq_lo,
            freq_hi=self._test_tone_freq_hi,
        )

        playback_budget_s = _compute_playback_watchdog_s(
            samples.size, self._sample_rate, self._ptt_delay_s
        )
        watchdog = threading.Timer(
            playback_budget_s,
            self._watchdog_fire,
            args=[playback_budget_s, this_tx_id],
        )
        # L13: daemon=True so test-tone watchdogs caught mid-shutdown
        # don't block interpreter exit.  See the matching comment on
        # the SSTV TX path's playback_watchdog.
        watchdog.daemon = True
        watchdog.start()

        try:
            result = self._run_tx(samples, rig, live_gain=True)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Test tone TX failed: {exc}")
            self.transmission_aborted.emit()
            return
        finally:
            watchdog.cancel()

        if result is None:
            return
        if self._stop_event.is_set():
            self.transmission_aborted.emit()
        elif result:
            self.transmission_complete.emit()
        else:
            self.transmission_aborted.emit()

    def _play_via_tci(
        self,
        samples: NDArray,
        *,
        live_gain: bool = False,
        periodic_check: Callable[[], None] | None = None,
    ) -> bool:
        """Stream *samples* to the TCI server as TX audio binary frames.

        Mirrors the PortAudio chunked-write loop in ``play_blocking``:
        100 ms chunks, real-time pacing via ``time.sleep``, stop-event
        check at each chunk boundary, optional live-gain scaling, optional
        periodic health check, and waterfall chunk emission.

        Returns ``True`` if all samples were sent without interruption,
        ``False`` if the stop event was set or an error occurred.
        """
        conn = self._tci_connection  # snapshot — may be cleared by GUI thread
        if conn is None:
            self.error.emit("TCI connection unavailable for TX audio.")
            return False

        # H5: validate that the TCI server is operating at the same audio
        # sample rate the TX path is generating at.  TCI's audio_samplerate
        # is a global setting that affects both RX and TX, so if the user
        # has a non-48-kHz RX bandwidth configured (e.g. AetherSDR with
        # 96 kHz I/Q) and our TxWorker is at the default 48 kHz, the
        # server will play our samples at whatever its rate is — silently
        # pitching SSTV off and producing slanted images at the receiver.
        # Refuse rather than silently corrupt; the user can either change
        # the TX rate to match, or change the SDR's audio rate to 48 kHz.
        try:
            server_rate = int(conn.sample_rate)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            server_rate = 0
        if server_rate and server_rate != self._sample_rate:
            msg = (
                f"TCI server audio rate ({server_rate} Hz) does not match "
                f"TX generation rate ({self._sample_rate} Hz).  Reconfigure "
                "the SDR to 48 kHz audio, or set the app's sample rate to "
                "match the SDR — TX is refused to avoid silently pitching "
                "the SSTV signal off-frequency."
            )
            _log.error("TCI TX rate mismatch: %s", msg)
            self.error.emit(msg)
            return False

        # H6: only start the server-side audio stream if RX hasn't already
        # subscribed it.  Previously this was unconditional, which meant a
        # TX-only flow (operator never started RX, or TX before the first
        # RX) leaked one server-side RX stream per transmission running
        # forever after TX ended — wasted bandwidth on the SDR server.
        # We remember whether *we* started it so the matching audio_stop
        # below only fires when paired with an audio_start we sent.
        started_rx_for_tx = False
        try:
            already_subscribed = bool(
                conn.is_rx_audio_subscribed()  # type: ignore[attr-defined]
            )
        except Exception:  # noqa: BLE001
            # Older TciConnection without the helper — fall back to the
            # legacy idempotent send so TX still works.
            already_subscribed = False
        if not already_subscribed:
            try:
                conn.send("audio_start:0;")  # type: ignore[attr-defined]
                conn.mark_rx_audio_subscribed(True)  # type: ignore[attr-defined]
                started_rx_for_tx = True
            except AttributeError:
                # Pre-H6 TciConnection: no mark_rx_audio_subscribed helper.
                # Send the start anyway — at worst we leak one stream as
                # before (same behaviour as the pre-H6 path).
                try:
                    conn.send("audio_start:0;")  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    _log.error("TCI audio_start failed: %s", exc)
                    self.error.emit(f"TCI audio_start failed: {exc}")
                    return False
            except Exception as exc:  # noqa: BLE001
                _log.error("TCI audio_start failed: %s", exc)
                self.error.emit(f"TCI audio_start failed: {exc}")
                return False

        chunk_size = int(self._sample_rate * 0.1)  # 100 ms per chunk

        # int16 PCM → float32 [-1, 1] for TCI.  Gain is already baked in
        # by transmit() for the normal path; live_gain re-applies here for
        # the test-tone path (where pre-scaling is skipped).
        samples_f32 = samples.astype(np.float32) / 32768.0

        # Append a silent tail so the TCI server's audio pipeline drains
        # completely before we return and the caller drops PTT.  Unlike
        # PortAudio (which blocks until the DAC finishes), the send loop below
        # returns as soon as the last chunk is *sent*, not when it is *played*.
        # The server buffers audio and plays it with some pipeline delay D
        # (measured at ~700–800 ms for AetherSDR).  Without the tail, PTT
        # drops D ms before the last CW element finishes, turning the last
        # dah into a dit (Y → C, etc.).
        # Silence carries no RF so it does not matter if the tail itself gets
        # cut when PTT drops — only the content before it needs to play fully.
        tail_n = int(_TCI_PIPELINE_TAIL_S * self._sample_rate)
        samples_f32 = np.concatenate(
            [samples_f32, np.zeros(tail_n, dtype=np.float32)]
        )
        content_total = len(samples) # original sample count for progress reporting
        total = len(samples_f32)

        _check_counter = 0
        written = 0

        # Absolute-deadline pacing: track when each batch of samples should
        # have finished playing and sleep only for what's left.  A plain
        # time.sleep(chunk_duration) accumulates Windows timer granularity
        # (~15 ms per tick) as an unbounded lag; the absolute approach
        # self-corrects each overshoot so the TCI server's buffer never
        # drains mid-element — which would corrupt CW timing.
        tx_start = time.perf_counter()

        try:
            while written < total:
                if self._stop_event.is_set():
                    break

                if periodic_check is not None:
                    _check_counter += 1
                    if _check_counter % _TCI_HEALTH_CHECK_INTERVAL == 0:
                        try:
                            periodic_check()
                        except Exception:  # noqa: BLE001
                            if not self._stop_event.is_set():
                                self._stop_event.set()
                            break

                end = min(written + chunk_size, total)
                chunk_f32 = samples_f32[written:end]

                if live_gain:
                    gain = self._output_gain
                    if gain != 1.0:
                        chunk_f32 = np.clip(chunk_f32 * gain, -1.0, 1.0).astype(np.float32)

                try:
                    conn.send_tx_audio_chunk(  # type: ignore[attr-defined]
                        chunk_f32,
                        sample_rate=self._sample_rate,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.error("TCI TX audio chunk error: %s", exc)
                    self.error.emit(f"TCI TX audio error: {exc}")
                    return False

                if self._waterfall_active:
                    self.tx_audio_chunk.emit(chunk_f32)
                written = end
                self.transmission_progress.emit(
                    min(written, content_total), content_total
                )

                # Sleep until the samples just sent should have finished playing.
                # Using an absolute target (tx_start + samples_so_far / rate)
                # means any single sleep overshoot is corrected next iteration
                # instead of compounding across the whole transmission.
                target = tx_start + written / self._sample_rate
                gap = target - time.perf_counter()
                if gap > 0:
                    time.sleep(gap)

            return not self._stop_event.is_set()
        finally:
            # H6: if we started the server-side RX stream just for this TX
            # (no real RX subscription was active), tear it down now so
            # the server doesn't keep streaming RX audio nobody listens to.
            # Runs on all exit paths — early return, exception, or normal
            # completion — so the cleanup is symmetric with the startup.
            if started_rx_for_tx:
                try:
                    conn.send("audio_stop:0;")  # type: ignore[attr-defined]
                    conn.mark_rx_audio_subscribed(False)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    _log.warning("TCI audio_stop after TX failed: %s", exc)

    def _run_tx(
        self,
        samples: NDArray,
        rig: Rig,
        live_gain: bool = False,
    ) -> bool | None:
        """Key PTT, play *samples*, unkey PTT.

        Parameters
        ----------
        samples:
            The fully-rendered int16 buffer to play.  Regular SSTV TX
            pre-scales this by ``self._output_gain`` before calling so
            the ALC sees a stable envelope for the duration of the image.
        rig:
            Backend to key/unkey.  ``ManualRig`` skips the hardware call.
        live_gain:
            If ``True``, ``self._output_gain`` is re-read per playback
            chunk inside ``play_blocking`` and applied on-the-fly.  Used
            by the test-tone path so moving the TX gain slider during
            calibration is audible within ~100 ms.  Callers that enable
            this MUST NOT pre-scale *samples* or the gain will apply
            twice.

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

        # H4: the actual CAT ping runs on a background thread so a wedged
        # daemon/USB chain can't stall the audio write loop (which used
        # to ping inline and underrun mid-image).  ManualRig needs no
        # monitoring — its get_ptt() is a no-op that never raises.
        health_monitor: _RigHealthMonitor | None = None
        if not isinstance(rig, ManualRig):
            health_monitor = _RigHealthMonitor(rig)
            health_monitor.start()

        def _rig_health_check() -> None:
            """Re-raise the monitor's failure, if any. Never blocks.

            Called every ~1 s by the playback loops between chunk writes.
            We emit signals before re-raising so the GUI thread updates
            the radio panel immediately rather than waiting for TX to finish.
            """
            if health_monitor is None or health_monitor.failure is None:
                return
            exc = health_monitor.failure
            self.error.emit(f"Rig disconnected during transmission: {exc}")
            self.rig_disconnected.emit()
            raise exc

        try:
            # M7: use _stop_event.wait() instead of bare time.sleep so a
            # Stop click during the PTT settle window is honoured within
            # milliseconds rather than holding the rig keyed for the full
            # delay (typically 200 ms, up to 2 s on slow relays).  Returns
            # True if the event was set during the wait, False on timeout.
            self._stop_event.wait(self._ptt_delay_s)
            if self._stop_event.is_set():
                # Stop pressed during the PTT delay window, before any audio.
                _log.debug("TX stop requested during PTT delay — skipping playback")
            elif self._tci_connection is not None:
                _log.info(
                    "TX via TCI: samples=%d rate=%d dtype=%s",
                    len(samples),
                    self._sample_rate,
                    samples.dtype,
                )
                playback_succeeded = self._play_via_tci(
                    samples,
                    live_gain=live_gain,
                    periodic_check=_rig_health_check,
                )
            else:
                _log.info(
                    "TX via PortAudio: device=%r samples=%d rate=%d dtype=%s",
                    self._output_device,
                    len(samples),
                    self._sample_rate,
                    samples.dtype,
                )
                output_stream.play_blocking(
                    samples,
                    self._sample_rate,
                    device=self._output_device,
                    progress_callback=lambda played, total: self.transmission_progress.emit(played, total),
                    stop_event=self._stop_event,
                    gain_provider=(
                        (lambda: self._output_gain) if live_gain else None
                    ),
                    periodic_check=_rig_health_check,
                    chunk_callback=(
                        (lambda chunk: self.tx_audio_chunk.emit(chunk))
                        if self._waterfall_active else None
                    ),
                )
                playback_succeeded = not self._stop_event.is_set()
        except sd.PortAudioError as exc:
            # An abort (Stop / dead-man's-switch) calls stream.abort(), which
            # makes the in-flight write() raise PortAudioError.  That's the
            # intended stop path, not a device fault — don't cry wolf.
            if self._stop_event.is_set():
                _log.info("TX playback aborted by stop request (%s)", exc)
            else:
                _log.error("TX PortAudioError: %s", exc)
                msg = str(exc)
                if "Blocking API not supported" in msg or "WDM-KS" in msg:
                    self.error.emit(
                        "Output device does not support playback (Windows WDM-KS). "
                        "Open Settings → Audio and choose a WASAPI or MME output device."
                    )
                else:
                    self.error.emit(
                        "Audio output device error during transmission — "
                        "check Settings → Audio."
                    )
        except Exception as exc:  # noqa: BLE001
            if self._stop_event.is_set():
                _log.info("TX playback aborted by stop request (%s)", exc)
            else:
                _log.error("TX playback exception: %s", exc, exc_info=True)
                self.error.emit(f"Playback failed: {exc}")
        finally:
            # Stop the health monitor BEFORE unkeying so its in-flight
            # CAT ping can't interleave with set_ptt on the same
            # serial/socket link.
            if health_monitor is not None:
                health_monitor.stop()
            # ALWAYS unkey, even on error or stop, so the rig never gets
            # left in a stuck-keyed state.  Retries with a backend
            # reconnect between attempts — see _unkey_with_retry.
            self._unkey_with_retry(rig)

        return playback_succeeded

    def _unkey_with_retry(self, rig: Rig) -> None:
        """Drop PTT, reconnecting the backend between failed attempts.

        A USB-serial unplug or a rigctld/TCI connection drop mid-TX
        makes a plain ``set_ptt(False)`` fail — the old socket/port is
        dead, so no number of retries on the *existing* link can ever
        unkey the radio.  Each retry therefore closes and re-opens the
        backend first: a rigctld/TCI reconnect succeeds as soon as the
        daemon/server is reachable again, and a serial open succeeds
        once the port re-enumerates.

        Catches Exception (not just RigError) so a raw OSError /
        termios.error from the dead link is retried and reported rather
        than escaping ``_run_tx``.  Emits a single ``error`` only after
        all attempts fail, with an explicit stuck-PTT warning so the
        operator knows to check the radio.
        """
        first_exc: Exception | None = None
        for attempt in range(_UNKEY_ATTEMPTS):
            if attempt:
                try:
                    rig.close()
                except Exception:  # noqa: BLE001
                    # The link is already dead — close() failing is
                    # expected; open() below is what matters.
                    pass
                try:
                    rig.open()
                except Exception as exc:  # noqa: BLE001
                    # Reconnect failed (port still gone, daemon still
                    # down).  Log it but keep the ORIGINAL set_ptt
                    # failure as the reported cause — that's the
                    # diagnosis the operator needs.
                    _log.warning(
                        "PTT unkey attempt %d/%d: backend reconnect "
                        "failed: %s",
                        attempt + 1,
                        _UNKEY_ATTEMPTS,
                        exc,
                    )
                    time.sleep(_UNKEY_RETRY_DELAY_S)
                    continue
            try:
                rig.set_ptt(False)
            except Exception as exc:  # noqa: BLE001
                first_exc = first_exc or exc
                _log.warning(
                    "PTT unkey attempt %d/%d failed: %s",
                    attempt + 1,
                    _UNKEY_ATTEMPTS,
                    exc,
                )
                if attempt + 1 < _UNKEY_ATTEMPTS:
                    time.sleep(_UNKEY_RETRY_DELAY_S)
                continue
            if attempt:
                _log.warning(
                    "PTT unkey succeeded on attempt %d after backend "
                    "reconnect",
                    attempt + 1,
                )
            return
        self.error.emit(
            f"Could not unkey rig after {_UNKEY_ATTEMPTS} attempts: "
            f"{first_exc} — the radio may still be TRANSMITTING. Check "
            "the rig and unkey manually (and consider enabling your "
            "rig's TX time-out timer as a backstop)."
        )

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

        NOTE: this waits on the stop *request*, not on the worker
        having unwound — a caller that needs "TX is actually finished,
        PTT dropped, rig released" wants :meth:`wait_for_idle`.
        """
        return self._stop_event.wait(timeout=timeout)

    def wait_for_idle(self, timeout: float) -> bool:
        """Block until no transmission is in flight, or *timeout* elapses.

        Returns ``True`` when the worker is idle — ``transmit`` /
        ``transmit_test_tone`` fully unwound, including the
        finally-block unkey retries — and ``False`` if the timeout
        expired with a transmission still running.  Safe to call from
        any thread; ``timeout=0`` is a non-blocking idle check.

        v0.4.0 audit high #5: closeEvent previously "waited" on the
        stop flag it had just set, which returned instantly and let the
        owning QThread be quit while the worker was mid-unkey.
        """
        return self._idle_event.wait(timeout=timeout)

    def _watchdog_fire(self, duration_s: float = 0.0, tx_id: int = 0) -> None:
        """Called by a watchdog timer when TX exceeds its allowed budget.

        ``duration_s`` is the budget the firing timer was created with —
        the per-transmission playback budget computed from sample count +
        PTT delay.  Passed through to the ``watchdog_fired`` signal so
        the UI can show the actual figure rather than guessing.

        ``tx_id`` is the transmission id captured when the timer was
        scheduled.  H4: if it no longer matches ``self._tx_id`` the firing
        timer belongs to a transmission that has already ended (either
        completed normally, was cancelled, or was overtaken by a newer
        ``transmit()`` call).  ``threading.Timer.cancel()`` is best-effort
        — a timer thread already running at cancel time still completes
        — so without this guard a stale fire would set ``_stop_event``
        and call the global ``output_stream.stop()`` just as the next
        transmission was starting, racing the ``_stop_event.clear()`` at
        entry and aborting an unrelated TX (or cancelling a concurrent
        test-tone via the global ``sd.stop``).

        Safe to call from the timer's background thread: signals are Qt
        queued connections (delivered on the GUI thread) and
        ``request_stop`` is explicitly thread-safe.

        Emits ``watchdog_fired`` instead of ``error`` so the GUI can
        display a persistent watchdog message that isn't immediately
        clobbered by the subsequent ``transmission_aborted`` signal.
        """
        with self._tx_id_lock:
            current = self._tx_id
        if tx_id != 0 and tx_id != current:
            _log.debug(
                "stale TX watchdog fire ignored (timer tx_id=%d, current=%d)",
                tx_id, current,
            )
            return
        self._watchdog_triggered = True
        self.watchdog_fired.emit(float(duration_s))
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
    #: Emitted just before ``image_complete`` when a full image has decoded.
    #: Carries the raw PCM audio (float64 ndarray at ``sample_rate``) that
    #: produced the image so it can be saved as a WAV for re-decode.
    #: ``None`` is never emitted — the signal fires only when a buffer exists.
    rx_audio_ready = Signal(object, object, int, int)  # (audio_f64, Mode, vis_code, sample_rate)
    status_update = Signal(str)  # periodic progress text
    error = Signal(str)
    #: Centralized exception channel — same contract as TxWorker's
    #: equivalent.  See TxWorker.error_occurred docstring.
    error_occurred = Signal(str, object)  # (slot_name, exception)
    #: Emitted after ``reset()`` finishes on the worker thread.  MainWindow
    #: uses this to order "reset → start_capture" across the two worker
    #: threads (OP-05): without it, a fresh chunk from an already-open
    #: audio stream could be fed into the decoder before the reset slot ran.
    reset_done = Signal()
    #: Emitted with each audio chunk (after input gain) so the waterfall can
    #: display incoming RX audio.  Chunk is float64.
    waterfall_chunk = Signal(object)

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        flush_samples: int | None = None,
        weak_signal: bool = False,
        watchdog_timeout_s: int | None = None,
        final_slant_correction: bool = False,
        incremental_decode: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sample_rate = sample_rate
        self._weak_signal = weak_signal
        self._watchdog_line_floor_s: float = (
            float(watchdog_timeout_s)
            if watchdog_timeout_s is not None
            else _RX_WATCHDOG_LINE_FLOOR_S
        )
        self._final_slant_correction = final_slant_correction
        self._incremental_decode = incremental_decode
        self._cancel_event = threading.Event()
        self._decoder = Decoder(
            sample_rate,
            weak_signal=weak_signal,
            incremental_decode=incremental_decode,
        )
        self._decoder.set_cancel_event(self._cancel_event)
        self._scratch: list[NDArray[np.float64]] = []
        self._scratch_samples: int = 0
        self._total_samples: int = 0
        self._decoding: bool = False
        self._input_gain: float = 1.0
        self._tx_active: bool = False
        # Flush cadence is selected dynamically per flush via
        # ``_current_flush_samples`` — see the block comment on the
        # ``_DECODE_FLUSH_INTERVAL_S_*`` constants above for the full
        # rationale.  We pre-compute the three sample counts so the
        # hot path in ``feed_chunk`` is a single attribute read + an
        # integer comparison.
        #
        # Legacy ``flush_samples`` override (explicit int) still wins
        # for back-compat with tests that pin a tight flush size.
        self._flush_samples_override: int | None = flush_samples
        self._flush_samples_incremental_idle: int = int(
            _DECODE_FLUSH_INTERVAL_S_INCREMENTAL_IDLE * sample_rate
        )
        self._flush_samples_incremental_decoding: int = int(
            _DECODE_FLUSH_INTERVAL_S_INCREMENTAL_DECODING * sample_rate
        )
        self._flush_samples_batch: int = int(
            _DECODE_FLUSH_INTERVAL_S_BATCH * sample_rate
        )
        # Legacy public attribute: many existing tests read this
        # directly to assert the configured threshold.  It now
        # reflects the IDLE threshold of the active path, which is
        # the one that governs the first flush after construction.
        self._flush_samples: int = self._current_flush_samples()
        # v0.1.36: RX decoder watchdog state — see _check_rx_watchdog
        # for the full logic.  ``_decoding_start_time`` is set when an
        # ``ImageStarted`` event fires; ``_last_progress_time`` updates
        # on every ``ImageProgress``.  ``_decoding_mode`` / ``_decoding_vis``
        # / ``_last_progress_image`` / ``_last_progress_lines`` /
        # ``_decoding_lines_total`` carry the data we need to synthesise
        # a partial ``ImageComplete`` if the watchdog trips.
        self._decoding_start_time: float = 0.0
        self._last_progress_time: float = 0.0
        self._decoding_mode: Mode | None = None
        self._decoding_vis: int = 0
        self._decoding_lines_total: int = 0
        self._last_progress_image: PILImage | None = None
        self._last_progress_lines: int = 0
        # v0.2.1: independent wall-clock tick for the watchdog.
        # Created lazily on the worker thread — see
        # ``_ensure_watchdog_timer`` for why.
        self._watchdog_timer: QTimer | None = None
        # v0.2.2: wall-clock timestamp of the most recent watchdog
        # trip.  Used by ``_flush`` to suppress routine "Listening…"
        # status updates for a short cooldown so the user can read
        # the timeout message before it gets overwritten.
        self._watchdog_trip_time: float = 0.0

    def set_input_gain(self, gain: float) -> None:
        """Set the software input gain (1.0 = unity). Thread-safe."""
        self._input_gain = gain

    @Slot(bool)
    def set_weak_signal(self, enabled: bool) -> None:
        """Enable or disable weak-signal VIS detection mode.

        Decorated ``@Slot(bool)`` so the GUI thread can dispatch this
        via a queued signal connection, guaranteeing the decoder rebuild
        happens on the worker's own event loop (never racing with
        ``feed_chunk``).
        """
        self._weak_signal = enabled
        self._decoder = Decoder(
            self._sample_rate,
            weak_signal=enabled,
            incremental_decode=self._incremental_decode,
        )
        self._decoder.set_cancel_event(self._cancel_event)
        # OP2-16: discard pre-toggle audio so the new decoder doesn't see
        # stale samples from the old decoder's time window.
        self._scratch.clear()
        self._scratch_samples = 0

    @Slot(int)
    def set_watchdog_timeout(self, seconds: int) -> None:
        """Set the no-progress watchdog floor (seconds).

        Takes effect on the next watchdog tick; does not affect a decode
        already in progress until the next ``_check_rx_watchdog`` call.
        """
        self._watchdog_line_floor_s = float(max(1, seconds))

    @Slot(bool)
    def set_incremental_decode(self, enabled: bool) -> None:
        """Enable or disable the per-line incremental decoder.

        Rebuilds the internal ``Decoder`` so the change takes effect on the
        next incoming VIS.  Any partial decode in flight is discarded with
        the old Decoder instance — callers should toggle this between
        transmissions, not mid-RX.

        Decorated ``@Slot(bool)`` so the GUI thread can dispatch this via a
        queued signal, guaranteeing the rebuild happens on the worker thread.
        """
        self._incremental_decode = enabled
        self._decoder = Decoder(
            self._sample_rate,
            weak_signal=self._weak_signal,
            incremental_decode=enabled,
        )
        self._decoder.set_cancel_event(self._cancel_event)
        # OP2-16: discard pre-toggle audio (matches set_sample_rate pattern).
        self._scratch.clear()
        self._scratch_samples = 0

    @Slot(bool)
    def set_final_slant_correction(self, enabled: bool) -> None:
        """Enable or disable final one-shot re-decode with slant correction.

        When *enabled* is ``True``, ``_dispatch`` runs ``decode_wav`` on the
        retained raw buffer after a complete image and uses the result if the
        mode matches.  This applies a global least-squares slant fit that can
        improve images from rigs with slight clock drift, but degrades images
        from weak/noisy signals where false-positive sync candidates corrupt
        the polyfit.  Off by default (progressive decode is used as-is).

        Decorated ``@Slot(bool)`` (OP-09) so MainWindow can dispatch via a
        queued signal connection, keeping the "all config changes run on
        the worker's own thread" invariant true and consistent with the
        sibling ``set_weak_signal`` / ``set_incremental_decode`` slots.
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

    @Slot(int)
    def set_sample_rate(self, sample_rate: int) -> None:
        """Update the sample rate and reconstruct the internal Decoder.

        Should be called only when capture is not running — the new
        Decoder discards any in-flight buffered audio. The caller
        (``MainWindow._apply_config``) shows a status-bar notice asking
        the user to restart capture when the rate changes mid-session.

        Decorated ``@Slot(int)`` so the GUI thread can dispatch this via a
        queued signal, guaranteeing the rebuild happens on the worker thread.

        OP-12: ``_total_samples`` is also zeroed so the "Xs buffered"
        status label isn't briefly off-by-rate after a mid-session change.
        """
        self._sample_rate = sample_rate
        self._decoder = Decoder(
            sample_rate,
            weak_signal=self._weak_signal,
            incremental_decode=self._incremental_decode,
        )
        self._decoder.set_cancel_event(self._cancel_event)
        self._scratch.clear()
        self._scratch_samples = 0
        self._total_samples = 0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    # === slots ===

    @Slot(object)
    def feed_chunk(self, chunk: NDArray) -> None:
        """Buffer one audio chunk; flush to the decoder on a cadence.

        Safe to invoke via queued connection from the audio worker
        thread. The chunk is copied into float64 eagerly (the rest of
        the DSP pipeline runs in float64) so the caller is free to
        reuse its buffer after the signal returns.

        While TX is active (``_tx_active`` is ``True``) chunks are
        discarded silently so the radio's own transmitted signal is
        never fed into the decoder (self-decode prevention, bug R-2).

        v0.2.1: the wall-clock watchdog timer is created here on
        first call so it picks up the worker-thread affinity instead
        of the constructor-time GUI thread.
        """
        # v0.2.1: start the wall-clock watchdog tick on first feed.
        self._ensure_watchdog_timer()

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
        self.waterfall_chunk.emit(arr)
        self._scratch.append(arr)
        self._scratch_samples += arr.size
        self._total_samples += arr.size

        # Flush threshold is chosen per-call so it tracks the
        # IDLE → DECODING transition: we want cheap frequent flushes
        # while painting lines, but infrequent (stable) flushes while
        # hunting for VIS on noisy pre-transmission audio.
        if self._scratch_samples >= self._current_flush_samples():
            self._flush()

    def _current_flush_samples(self) -> int:
        """Return the flush threshold (samples) appropriate for the
        current path and state.

        * Explicit ``flush_samples`` constructor override wins.
        * Batch path uses one constant regardless of state (its
          O(N²) reprocessing cost is the dominant factor).
        * Incremental path uses 1 s while IDLE to match pre-v0.2.6
          VIS-hunt cadence (avoids multiplying the unknown-VIS
          false-positive rate by 10× — that path trims the buffer
          past ``vis_end`` and can mutilate real VIS arriving moments
          later) and 0.1 s while DECODING to paint lines as they
          complete.
        """
        if self._flush_samples_override is not None:
            return self._flush_samples_override
        if not self._incremental_decode:
            return self._flush_samples_batch
        return (
            self._flush_samples_incremental_decoding
            if self._decoding
            else self._flush_samples_incremental_idle
        )

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

        Emits ``reset_done`` after the state machine is reset, so callers
        that need to order a subsequent action (e.g. starting a new audio
        capture) against the reset can connect to that signal.  Without
        this ordering hook, the "start capture" request can race the
        reset slot on the worker's own queue (OP-05).
        """
        self._scratch.clear()
        self._scratch_samples = 0
        self._total_samples = 0
        self._decoding = False
        self._decoder.reset()
        # v0.1.36: clear watchdog state so a previous (possibly
        # stalled) decoding session doesn't leak into the next one.
        self._reset_watchdog_state()
        # v0.2.2: also drop any post-trip cooldown so the user's
        # explicit Clear immediately lets "Listening…" updates resume.
        self._watchdog_trip_time = 0.0
        # v0.2.1: ensure the wall-clock tick is running so the
        # watchdog can fire even if audio flow pauses after reset
        # (e.g. user clicks Clear then doesn't send audio for a
        # while).  Idempotent — the second+ call is a no-op.
        self._ensure_watchdog_timer()
        # Re-arm after any cancel that was in flight.  This runs on the
        # worker thread so it's sequentially after _flush() has returned.
        self._cancel_event.clear()
        self.reset_done.emit()

    @Slot()
    def flush(self) -> None:
        """Force an immediate flush of any buffered audio to the decoder.

        Exposed for the ``stopped`` signal path so the tail of an
        in-flight transmission isn't discarded when the user stops
        capture mid-image. Idempotent.
        """
        if self._scratch_samples > 0:
            self._flush()

    @Slot()
    def shutdown(self) -> None:
        """Stop and release the wall-clock watchdog QTimer.

        Must be invoked on this worker's own thread (queued) before the
        host thread's event loop is quit.  The watchdog timer is created
        lazily in ``_ensure_watchdog_timer`` with RX-thread affinity and
        has no explicit stop path elsewhere; without this slot, the timer
        is still active when ``_rx_thread.quit()`` returns and the later
        destructor on the GUI thread prints::

            QObject::killTimer: Timers cannot be stopped from another thread
            QObject::~QObject: Timers cannot be stopped from another thread

        Idempotent: calling ``shutdown`` a second time is a no-op.
        """
        if self._watchdog_timer is not None:
            self._watchdog_timer.stop()
            self._watchdog_timer.deleteLater()
            self._watchdog_timer = None

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

        # OP-22 defensive guard: ``Decoder.feed`` auto-resets to IDLE
        # after emitting one ``ImageComplete`` so at most one per feed
        # is the contract.  If a future change violates that, the
        # second complete would reach ``_dispatch`` with
        # ``consume_last_buffer()`` already drained to None and would
        # silently emit the progressive image instead of the (possibly
        # slant-corrected) re-decode.  Fail loudly instead.
        complete_count = sum(1 for e in events if isinstance(e, ImageComplete))
        assert complete_count <= 1, (
            f"Decoder.feed returned {complete_count} ImageComplete events "
            "in a single flush — the Decoder contract is at most one per "
            "feed (it auto-resets to IDLE).  Investigate core.decoder "
            "before weakening this assertion."
        )

        if not events and not self._decoding:
            # No decode yet — show progress so the user knows we're alive.
            # v0.2.2: suppress for a short cooldown after a watchdog
            # trip so the user has time to read the timeout message
            # before this overwrites it.
            cooldown_active = (
                self._watchdog_trip_time > 0.0
                and (time.monotonic() - self._watchdog_trip_time)
                < _RX_POST_WATCHDOG_COOLDOWN_S
            )
            if not cooldown_active:
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

        # v0.1.36: check the per-transmission watchdog *after* event
        # dispatch so ``_last_progress_time`` reflects any progress
        # in this flush.  If the signal has faded (no new lines in N
        # line periods) or we've been decoding way past the expected
        # mode duration, synthesise a partial completion from the
        # last progress image we saw and reset to IDLE.
        if self._decoding:
            self._check_rx_watchdog()

    def _check_rx_watchdog(self) -> None:
        """Check the "decode stalled / signal faded" watchdog and
        reset the decoder if tripped.

        Two independent conditions; either trips the watchdog:

        1. **Total elapsed** exceeds ``mode.total_duration_s × 1.5``
           with a ``_RX_WATCHDOG_TOTAL_FLOOR_S`` floor — the whole
           transmission would normally be done by now.

        2. **No new progress** for
           ``max(_RX_WATCHDOG_LINE_FLOOR_S, 5 × line_time_ms)`` — lines
           have stopped arriving mid-image.  The floor is user-
           configurable (default 5 s) via Settings → Receive so
           operators can tune it for their propagation conditions.

        On trip, emit a truncated ``image_complete`` carrying whatever
        lines we managed to decode so the user still gets the partial
        image in their gallery, then call ``_decoder.reset()`` and
        return the worker to IDLE for the next VIS.
        """
        if self._decoding_mode is None:
            # Shouldn't happen — _decoding is True but no mode recorded.
            # Defensive: just clear the flag.
            self._decoding = False
            return

        now = time.monotonic()
        spec = MODE_TABLE.get(self._decoding_mode)
        if spec is None:
            return

        total_budget_s = max(
            _RX_WATCHDOG_TOTAL_FLOOR_S,
            spec.total_duration_s * _RX_WATCHDOG_TOTAL_MULTIPLIER,
        )
        line_budget_s = max(
            self._watchdog_line_floor_s,
            _RX_WATCHDOG_LINE_MULTIPLIER * spec.line_time_ms / 1000.0,
        )

        elapsed_total = now - self._decoding_start_time
        elapsed_line = now - self._last_progress_time

        total_trip = elapsed_total > total_budget_s
        line_trip = elapsed_line > line_budget_s

        if not (total_trip or line_trip):
            return

        reason = (
            f"no progress for {elapsed_line:.0f} s"
            if line_trip
            else f"elapsed {elapsed_total:.0f} s exceeds "
                 f"{total_budget_s:.0f} s budget"
        )
        _log.info(
            "RX watchdog tripped on %s (%s); resetting decoder",
            self._decoding_mode.value,
            reason,
        )

        # Try to surface whatever partial image we've accumulated so
        # the user still gets something in the gallery.  The image is
        # truncated to the mode's native resolution (already is —
        # every progressive image is full-sized with black rows for
        # the un-decoded tail).
        if self._last_progress_image is not None and self._last_progress_lines > 0:
            self.image_complete.emit(
                self._last_progress_image,
                self._decoding_mode,
                self._decoding_vis,
            )
            self.status_update.emit(
                f"Decode timed out ({reason}) — kept partial "
                f"{self._last_progress_lines}/{self._decoding_lines_total} lines."
            )
        else:
            self.status_update.emit(
                f"Decode timed out ({reason}) — no lines were decoded."
            )

        # Drop any buffered audio in the Decoder and return to IDLE.
        # The RxWorker's own _scratch buffer was already drained at
        # the start of this flush, so just reset the Decoder state.
        self._decoder.reset()
        self._decoding = False
        self._reset_watchdog_state()
        self._total_samples = 0
        # v0.2.2: remember when this trip happened so the next few
        # idle-state flushes suppress the routine "Listening…"
        # status update — otherwise the timeout message is
        # overwritten before the user can read it.
        self._watchdog_trip_time = now

    def _reset_watchdog_state(self) -> None:
        """Clear the per-transmission watchdog tracking fields.
        Called when decoding completes cleanly, when the watchdog
        trips, and when the RxWorker is reset."""
        self._decoding_start_time = 0.0
        self._last_progress_time = 0.0
        self._decoding_mode = None
        self._decoding_vis = 0
        self._decoding_lines_total = 0
        self._last_progress_image = None
        self._last_progress_lines = 0

    def _ensure_watchdog_timer(self) -> None:
        """Create the wall-clock watchdog ticker on first use.

        The timer must live on the RxWorker's own thread (not the GUI
        thread that constructed ``RxWorker``) so its ``timeout`` slot
        runs on the decode thread where all watchdog state is owned.
        Constructing it in ``__init__`` would bind it to the GUI
        thread; creating it lazily on the first slot-invocation on
        the worker thread picks up the correct thread affinity.  Same
        pattern as ``InputStreamWorker`` uses for its drain timer.
        """
        if self._watchdog_timer is not None:
            return
        self._watchdog_timer = QTimer()
        self._watchdog_timer.setInterval(_RX_WATCHDOG_TICK_MS)
        self._watchdog_timer.timeout.connect(self._on_watchdog_tick)
        self._watchdog_timer.start()

    @Slot()
    def _on_watchdog_tick(self) -> None:
        """QTimer tick that runs the watchdog check on wall-clock
        time regardless of whether audio is still flowing.

        The original flush-driven watchdog (v0.1.36) only ran when a
        chunk arrived — fine for deep fades that still have driver-
        level noise floor coming through, but insufficient when the
        audio stream goes genuinely quiet (USB sleep, Bluetooth
        drop, brief OS audio-subsystem suspend).  This tick
        guarantees a watchdog check every ``_RX_WATCHDOG_TICK_MS``
        independent of audio flow.
        """
        if self._decoding:
            self._check_rx_watchdog()

    def _dispatch(self, event: object) -> None:
        if isinstance(event, ImageStarted):
            # v0.1.36: start the per-transmission watchdog timer.
            # ``_last_progress_time`` is also seeded so the no-progress
            # budget starts counting from VIS detection, not from the
            # first decoded line — which is the right behaviour when
            # the first line takes unusually long on a marginal signal.
            now = time.monotonic()
            self._decoding_start_time = now
            self._last_progress_time = now
            self._decoding_mode = event.mode
            self._decoding_vis = event.vis_code
            self._last_progress_image = None
            self._last_progress_lines = 0
            # spec.height is the sync-pulse count (halved for PD modes);
            # use display_height for the user-facing line count.
            spec = MODE_TABLE.get(event.mode)
            self._decoding_lines_total = (
                spec.display_height if spec is not None else 0
            )
            self.image_started.emit(event.mode, event.vis_code)
        elif isinstance(event, ImageProgress):
            # v0.1.36: record the latest partial image + line count so
            # the watchdog trip path has something to emit if the
            # signal fades before ImageComplete.
            self._last_progress_time = time.monotonic()
            self._last_progress_image = event.image
            self._last_progress_lines = event.lines_decoded
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
            if raw is not None and isinstance(raw, np.ndarray) and raw.size > 0:
                # M13: ``raw`` is consumed by two paths from here — the
                # ``rx_audio_ready`` slot writes it to disk on the GUI
                # thread, and the slant-correction branch below feeds
                # the same ndarray to ``decode_wav`` on the worker
                # thread.  Today both consumers only read (np.clip +
                # multiply allocates new arrays) so no race, but if
                # either is ever modified to filter in-place the shared
                # reference would corrupt the other.  Emit a copy so
                # the disk-write path can't observe in-flight mutations
                # from a future decode_wav rewrite.
                self.rx_audio_ready.emit(
                    raw.copy(), event.mode, event.vis_code, self._sample_rate
                )
            final_image = event.image
            if self._final_slant_correction:
                # Opt-in: run a full single-pass re-decode with global
                # least-squares slant correction.  Helpful for clean signals
                # with clock drift; harmful for weak/noisy signals where
                # false-positive syncs corrupt the polyfit.
                #
                # Robot 36 is explicitly excluded: the incremental path uses
                # the slowrx color pipeline (direct integer YCbCr→RGB matrix)
                # while decode_wav/_decode_robot36 uses the older median+PIL
                # path.  Substituting the batch result would silently swap
                # pipelines, producing visibly different colors.  Users who
                # need slant correction for Robot 36 should use a hardware or
                # software sample-rate lock instead.
                if event.mode == Mode.ROBOT_36:
                    _log.debug(
                        "slant-correction re-decode skipped for Robot 36 "
                        "(batch and incremental paths use different color pipelines)"
                    )
                elif self._cancel_event.is_set():
                    # Stop / RX-reset was requested while the worker was
                    # mid-dispatch.  ``decode_wav`` doesn't honour the
                    # cancel event itself, and a Pasokon P7 re-decode
                    # takes several seconds — running it here would
                    # block the worker thread past the cancel and make
                    # the Stop button feel unresponsive.  Skip and use
                    # the progressive result.
                    _log.debug(
                        "slant-correction re-decode skipped — "
                        "cancel requested before dispatch"
                    )
                else:
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
                        # Surface through the centralized error channel so
                        # the UI can offer a "slant correction failed —
                        # using progressive result" hint instead of users
                        # silently wondering why the toggle "did nothing".
                        self.error_occurred.emit("dispatch.slant_correction", exc)
            self.image_complete.emit(final_image, event.mode, event.vis_code)
            # v0.1.36: clean completion — clear watchdog state so the
            # next VIS starts with a fresh deadline.
            self._reset_watchdog_state()
        elif isinstance(event, DecodeError):
            self.error.emit(event.message)


__all__ = ["DEFAULT_PTT_DELAY_S", "RxWorker", "TxWorker"]
