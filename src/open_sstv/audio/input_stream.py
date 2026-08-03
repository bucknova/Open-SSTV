# SPDX-License-Identifier: GPL-3.0-or-later
"""Thread-safe RX audio capture pipeline.

This module bridges PortAudio's real-time callback thread to Qt's
signal/slot machinery. The design is the documented-safe pattern for
python-sounddevice plus long-running Qt workers:

* PortAudio callback (real-time thread) — copies ``indata`` into a
  ``queue.Queue.put_nowait`` call. **No DSP, no allocations beyond
  ``ndarray.copy()``, no Qt signal emission.** Blocking the callback
  for more than a frame period causes audible glitches; emitting Qt
  signals from non-Qt threads is technically legal but muddies the
  thread-affinity model, so we keep the callback minimal.

* ``InputStreamWorker`` (Qt worker thread) — ``moveToThread``'d onto
  its own ``QThread``. A ``QTimer`` on that thread drains the queue
  at a steady cadence and emits ``chunk_ready(np.ndarray)`` for each
  frame. Downstream consumers (the ``RxWorker``) connect via
  ``Qt.AutoConnection`` and receive chunks on their own threads.

The queue is bounded. Under normal load the UI consumer empties it
faster than PortAudio fills it; under a stall (GUI-thread freeze,
huge decode) we drop samples rather than grow the queue unbounded —
dropping a handful of 20 ms chunks is recoverable but leaking memory
across a multi-hour listening session is not.

Public API
----------

``InputStreamWorker(QObject)``
    Signals
    -------
    ``chunk_ready(object)`` — ``np.ndarray`` of ``float32`` mono samples.
    ``started()``            — emitted after the stream opens successfully.
    ``stopped()``            — emitted after the stream closes (clean or error).
    ``error(str)``           — emitted on stream construction / overrun / etc.

    Slots
    -----
    ``start(device, sample_rate, blocksize)`` — open the stream and begin
        capturing. ``device`` is an ``AudioDevice`` or a raw PortAudio index
        or ``None`` for the system default.
    ``stop()`` — close the stream and drain the queue.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, QTimer, Signal, Slot

_log = logging.getLogger(__name__)

from open_sstv.audio.devices import AudioDevice

if TYPE_CHECKING:
    from numpy.typing import NDArray


#: Default sample rate for SSTV capture. 48 kHz is the industry
#: standard on modern sound cards and matches what the encoder side
#: uses — sticking to it avoids any implicit resampling in the
#: decode path.
DEFAULT_SAMPLE_RATE: int = 48_000

#: PortAudio frames per callback invocation. 1024 at 48 kHz is
#: ~21 ms per callback, comfortably below our 150 ms line period
#: so a dropped chunk maps to at most a single noisy pixel row.
DEFAULT_BLOCKSIZE: int = 1024

#: Maximum number of chunks buffered between the callback and the
#: consumer. At 1024 frames/chunk and 48 kHz that's ~5.4 s of audio
#: — plenty of slack for a transient GUI-thread stall without
#: letting a real stall grow memory forever. Overflow drops the
#: *newest* chunk, matching PortAudio's own ``input overflow``
#: semantics, and increments a drop counter surfaced via ``error``.
_QUEUE_MAXSIZE: int = 256

#: How often the worker thread drains the queue and emits signals.
#: 50 ms keeps UI latency well below one SSTV line while staying
#: coarse enough that the timer itself isn't hot. With the default
#: blocksize each drain pulls ~2–3 chunks.
_POLL_INTERVAL_MS: int = 50


#: How long the device watchdog waits for fresh audio before declaring the
#: input device lost (steady-state, after the first chunk has arrived).
#: 3 s gives ample slack for a brief system-level stall (suspend/resume,
#: driver reset) while still catching a genuine unplug within a few
#: seconds of the event.
_DEVICE_WATCHDOG_MS: int = 3000

#: Cold-start grace period before the watchdog engages.  Some USB audio
#: devices and Bluetooth SCO links take 1.5–2.5 s between
#: ``sd.InputStream.start()`` returning and the first PortAudio callback
#: firing; under thermal throttling or competing audio clients on
#: PipeWire/macOS this can push past 3 s and trip the watchdog spuriously
#: (OP-11).  6 s of cold-start budget covers the measured worst cases
#: while still catching a genuine "device never came up" failure well
#: below a human-patience threshold.
_DEVICE_WATCHDOG_COLD_START_MS: int = 6000


class InputStreamWorker(QObject):
    """Run a PortAudio input stream on a Qt worker thread.

    Usage (from the GUI thread):

        thread = QThread()
        worker = InputStreamWorker()
        worker.moveToThread(thread)
        thread.started.connect(lambda: worker.start(device))
        worker.chunk_ready.connect(rx_worker.feed_chunk)
        thread.start()

    ``start`` and ``stop`` are declared as slots so they can be
    invoked from the GUI thread via a queued connection — that's how
    the MainWindow asks the capture to begin/end without blocking on
    the worker thread's PortAudio calls.

    Lifecycle expectations:

    * Exactly one ``start``/``stop`` pair per listening session.
    * Calling ``stop`` before ``start`` is a no-op.
    * Calling ``start`` twice raises via the ``error`` signal; the
      first stream keeps running. Callers that want to switch devices
      must stop first.
    """

    chunk_ready = Signal(object)  # np.ndarray[float32]
    started = Signal()
    stopped = Signal()
    error = Signal(str)
    stream_error = Signal(str)  # emitted on device-loss; triggers clean stop

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[NDArray[np.float32]] = queue.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._stream: sd.InputStream | None = None
        self._timer: QTimer | None = None
        self._watchdog: QTimer | None = None
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        #: PortAudio drop counter.  ``+= 1`` from the RT callback and the
        #: queue-full path is read-modify-write, which races with the
        #: worker-thread reset to 0 in ``start()`` and the read in
        #: ``stop()``.  Under CPython the GIL makes plain int reads atomic
        #: but the increment can still lose against a concurrent assignment
        #: (and free-threaded 3.13t lets the int box itself race).  Guarded
        #: by ``_drop_lock`` — cheap and makes the contract obvious.
        self._drop_lock = threading.Lock()
        self._dropped_chunks: int = 0
        # OP-11: first-chunk tracker for cold-start → steady-state
        # watchdog interval switch.  Set back to False on stop().
        self._first_chunk_seen: bool = False
        # True while stop() is executing so _on_pa_stream_finished can
        # distinguish a deliberate teardown from an unexpected device loss.
        self._stopping: bool = False
        # Set by device-loss paths so stop() and start() know to call
        # _pa_reset() — PortAudio caches device handles internally and will
        # return -10851 (Invalid Property Value) on the next stream-open
        # unless Pa_Terminate()+Pa_Initialize() have been called.
        self._device_lost: bool = False
        # H-2 (audit 4.7/v0.2.9): both the wall-clock watchdog and the
        # PortAudio finished_callback can detect the same unplug and try
        # to emit ``stream_error`` + schedule ``stop()``.  The ``stop()``
        # second-call is a no-op (self._stream is None) but the duplicate
        # ``stream_error`` toast in the UI is user-visible and confusing.
        # This flag is set by whichever path fires first and short-circuits
        # the other.  Cleared at ``start()`` time so each new session is
        # ready to detect a fresh device-loss event.
        #
        # H5 (v0.3 audit): the two paths run on different threads (the
        # watchdog on this worker thread, the finished callback on
        # PortAudio's internal thread), so the check-then-set must be
        # atomic — a bare bool let both paths win the race and emit a
        # double toast / double stop().  All access goes through
        # ``_claim_device_loss_emit`` under ``_device_loss_lock``.
        self._device_loss_emitted: bool = False
        self._device_loss_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    # === slots (invoked from other threads via queued connections) ===

    @Slot(object, int, int)
    def start(
        self,
        device: AudioDevice | int | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        """Open the PortAudio stream and begin polling the queue.

        ``device`` accepts either an ``AudioDevice`` (we pull
        ``.index`` off it), a raw PortAudio index, or ``None`` for
        the system default — mirroring ``output_stream.play_blocking``
        so the same device picker works for both directions.
        """
        if self._stream is not None:
            self.error.emit("Input stream already running; stop first")
            return

        device_index = (
            device.index if isinstance(device, AudioDevice) else device
        )
        self._sample_rate = sample_rate
        with self._drop_lock:
            self._dropped_chunks = 0
        self._stopping = False
        # H-2: reset the dedupe flag so the next unplug emits exactly one
        # ``stream_error`` regardless of which detection path fires first.
        with self._device_loss_lock:
            self._device_loss_emitted = False

        # Drain any stale chunks from a previous session before the
        # callback starts pushing new ones. Queue lives on the worker
        # thread so this is safe without a lock.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        # Always reset PortAudio immediately before opening the new stream so
        # the device list is guaranteed fresh.  The conditional _device_lost
        # gate was removed because disconnect can be detected via multiple
        # paths (RX watchdog, TX serial health check, _on_pa_stream_finished)
        # and any path that misses setting the flag leaves PortAudio stale,
        # producing -9986 / -9998 errors.  _pa_reset() is fast (~50 ms) and
        # unconditionally correct — terminate+initialize is the only reliable
        # way to flush PortAudio's internal device cache after a USB unplug.
        self._pa_reset()
        self._device_lost = False

        try:
            self._stream = sd.InputStream(
                samplerate=sample_rate,
                blocksize=blocksize,
                device=device_index,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
                finished_callback=self._on_pa_stream_finished,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 — surface anything to UI
            self._stream = None
            self.error.emit(f"Could not open input stream: {exc}")
            # Emit stopped so the UI can re-enable the Start button even
            # when the stream never opened (e.g. stale device index after
            # a USB replug).
            self.stopped.emit()
            return

        # Create the poll timer lazily so its thread affinity matches
        # whichever thread ``start`` was invoked on (i.e. the worker
        # thread via queued connection). A timer created in __init__
        # would stick to the thread that called the constructor (the
        # GUI thread), and its timeouts would fire there instead.
        self._timer = QTimer()
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._drain_queue)
        self._timer.start()

        # Device-loss watchdog: if no audio chunks arrive within
        # _DEVICE_WATCHDOG_MS the input device has likely been unplugged or
        # stopped by the OS. The timer is reset on every non-empty drain;
        # on expiry it emits stream_error and calls stop() so the UI returns
        # to the idle state instead of hanging in "Capturing" forever.
        #
        # Cold-start grace (OP-11): the first interval uses the longer
        # ``_DEVICE_WATCHDOG_COLD_START_MS`` because PortAudio callbacks
        # can take 1.5–2.5 s to fire on slow-to-open devices.  The
        # regular ``_DEVICE_WATCHDOG_MS`` kicks in after the first chunk
        # is drained in ``_drain_queue``.
        self._watchdog = QTimer()
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(_DEVICE_WATCHDOG_COLD_START_MS)
        self._watchdog.timeout.connect(self._on_watchdog_timeout)
        self._watchdog.start()
        # Tracks whether we've ever drained a chunk — used to switch the
        # watchdog interval from cold-start to steady-state.
        self._first_chunk_seen: bool = False

        self.started.emit()

    @Slot()
    def stop(self) -> None:
        """Stop the PortAudio stream and flush any buffered chunks.

        Idempotent: calling ``stop`` on an already-stopped worker is
        a no-op and does not emit ``stopped`` a second time.
        """
        if self._stream is None:
            return

        # Signal _on_pa_stream_finished that this teardown is deliberate so
        # it doesn't misinterpret the PA finished callback as a device loss.
        self._stopping = True

        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

        if self._watchdog is not None:
            self._watchdog.stop()
            self._watchdog.deleteLater()
            self._watchdog = None
        # OP-11: reset the cold-start flag so the next start() gets
        # another grace period.
        self._first_chunk_seen = False

        # Time these two individually.  Both are blocking PortAudio calls
        # with no timeout parameter, and on some Windows/MME setups they
        # take longer than closeEvent's 2 s budget — which is what surfaces
        # as "audio worker stop() did not complete in 2 s" on every quit.
        # Logging which call ran long, and for how long, makes that report
        # actionable instead of a mystery.
        try:
            _t0 = time.monotonic()
            self._stream.stop()
            _t1 = time.monotonic()
            self._stream.close()
            _t2 = time.monotonic()
            if (_t2 - _t0) > 1.0:
                _log.warning(
                    "input stream teardown slow: stop() %.0f ms, close() %.0f ms "
                    "(total %.1f s) — a slow audio backend (Windows MME is the "
                    "usual culprit) can push app shutdown past its 2 s budget",
                    (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, _t2 - _t0,
                )
            else:
                _log.debug(
                    "input stream teardown: stop() %.0f ms, close() %.0f ms",
                    (_t1 - _t0) * 1000, (_t2 - _t1) * 1000,
                )
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Error closing input stream: {exc}")
        finally:
            self._stream = None
            self._stopping = False

        # Leave _device_lost set so start() can see it and call _pa_reset()
        # right before opening the next stream.  Resetting PortAudio here in
        # stop() is too early — the OS can reassign device indices between
        # stop and the user clicking Start again, making the reset useless.

        # Emit any residual chunks so the consumer gets a clean
        # tail-flush before we report stopped. This matters for
        # decode_wav-style consumers that want to finish whatever
        # image was in-flight when the user clicked Stop.
        self._drain_queue()

        with self._drop_lock:
            dropped = self._dropped_chunks
        if dropped > 0:
            self.error.emit(
                f"Input overflow: dropped {dropped} chunks"
            )

        self.stopped.emit()

    # === internal ===

    def _audio_callback(
        self,
        indata: NDArray[np.float32],
        frames: int,  # noqa: ARG002 — PortAudio API
        time_info: object,  # noqa: ARG002
        status: sd.CallbackFlags,
    ) -> None:
        """PortAudio callback — runs on the real-time audio thread.

        The body is intentionally minimal: copy the buffer (PortAudio
        reuses it across callbacks) and shove it onto the queue. Any
        blocking operation here — including Python-level locks or Qt
        signal emission with a DirectConnection — risks audio
        glitches. We do **not** raise on ``CallbackFlags`` because
        PortAudio will abort the stream if we do; instead we record
        the drop in the queue-overflow counter and let ``stop`` or
        the next drain cycle surface it via the ``error`` signal.
        """
        if status.input_overflow or status.input_underflow:
            # PortAudio already dropped samples before they reached us.
            # Record as a drop and keep running.  Lock-guarded against
            # the worker thread's reset / read in start() / stop().
            with self._drop_lock:
                self._dropped_chunks += 1

        # Flatten to 1-D mono. ``channels=1`` in ``InputStream`` gives
        # us shape (frames, 1); pull out the column and copy so the
        # downstream consumer owns its buffer. ``.copy()`` is mandatory:
        # ``np.ascontiguousarray`` skips the copy when the slice is
        # already contiguous (which it is for a single-column array),
        # leaving a view into PortAudio's recycled buffer that gets
        # overwritten by the next callback before the consumer drains
        # the queue.
        #
        # M11: the per-callback ``.copy()`` allocates ~4 KB float32 on
        # the RT thread (CPython GIL).  Under contention with a big
        # Hilbert transform on the worker thread this is the most
        # likely cause of input_overflow drops on long PD modes.  A
        # ring of pre-allocated buffers rotated by index would avoid
        # the allocation but adds non-trivial complexity (must
        # synchronise the ring head against queue consumption to
        # avoid overwriting buffers still in flight, and the queue
        # has maxsize=256 chunks which would require an equally
        # large ring — comparable memory to the current per-callback
        # allocation).  Left as a known cost; revisit if real-world
        # measurements show the drop rate is unacceptable.
        chunk = indata[:, 0].copy()

        # Guard against teardown race: PortAudio's RT thread can fire
        # this callback after Python's GC has started clearing the
        # object's __dict__ (e.g. during app close or hot-swap). Using
        # getattr avoids an AttributeError if _queue is already gone.
        q = getattr(self, "_queue", None)
        if q is None:
            return
        try:
            q.put_nowait(chunk)
        except queue.Full:
            # Consumer is stalled — drop the newest chunk rather than
            # block the RT thread. The drop counter is surfaced via
            # ``error`` when we eventually stop or the next drain.
            with self._drop_lock:
                self._dropped_chunks += 1

    @Slot()
    def _drain_queue(self) -> None:
        """Pull every pending chunk off the queue and emit it.

        Runs on the worker thread (via ``QTimer.timeout``). The drain
        is non-blocking so a steady state where PortAudio fills the
        queue faster than the timer fires is still bounded: every
        drain empties the queue completely.
        """
        drained_any = False
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            self.chunk_ready.emit(chunk)
            drained_any = True

        # Reset the device watchdog whenever we got real audio data.
        # After the first drain switch from cold-start to steady-state
        # interval so a momentary post-warm-up stall isn't misread as a
        # device-lost event (OP-11).
        if drained_any and self._watchdog is not None:
            if not self._first_chunk_seen:
                self._first_chunk_seen = True
                self._watchdog.setInterval(_DEVICE_WATCHDOG_MS)
            self._watchdog.start()

    @Slot()
    def _on_watchdog_timeout(self) -> None:
        """No audio for _DEVICE_WATCHDOG_MS ms — treat the device as lost.

        M12: schedule ``stop()`` via a single-shot zero-delay QTimer
        instead of calling it synchronously.  ``stop()`` calls
        ``self._stream.stop()/close()`` which can take >100 ms on a
        wedged macOS Core Audio device — blocking the worker event
        loop and any further drain.  Posting via QTimer.singleShot(0)
        re-enters the event loop, so the watchdog slot returns
        immediately and the actual stop runs on the next loop tick
        (still on this same worker thread, so QTimer affinity is
        preserved).

        H-2: gate ``stream_error`` on ``_device_loss_emitted`` so the
        PortAudio finished_callback path (which can race with this
        timeout on the same unplug) cannot also fire the toast.
        """
        self._device_lost = True
        if self._claim_device_loss_emit():
            self.stream_error.emit(
                "Audio device disconnected — replug and click Start to recover"
            )
        QTimer.singleShot(0, self.stop)

    def _on_pa_stream_finished(self) -> None:
        """PortAudio finished callback — called on PortAudio's internal thread.

        Fires whenever the stream ends: on a normal ``stop()`` call *and*
        on an unexpected device loss (USB unplug, OS audio-subsystem reset).
        The ``_stopping`` flag distinguishes the two:

        * If ``True``, ``stop()`` is already in progress — do nothing.
        * If ``False``, the device was lost mid-session.  Set
          ``_device_lost`` so ``stop()`` knows to call ``_pa_reset()``,
          emit ``stream_error`` with a clear recovery message, and
          schedule ``stop()`` on the worker thread via a queued invocation
          so the QTimer cleanup runs on the correct thread.  The watchdog
          is cancelled by ``stop()`` before it fires, preventing a
          duplicate message.
        """
        if self._stopping:
            return
        self._device_lost = True
        # H-2: gate ``stream_error`` on ``_device_loss_emitted`` so if the
        # watchdog already fired on the same unplug, we don't show a
        # second toast.  The watchdog's QTimer cleanup in ``stop()`` is
        # not synchronous with this PortAudio thread, so a race window
        # exists where both paths see the same unplug; the atomic
        # test-and-set in ``_claim_device_loss_emit`` closes it.
        if self._claim_device_loss_emit():
            self.stream_error.emit(
                "Audio device disconnected — replug and click Start to recover"
            )
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self, "stop", Qt.ConnectionType.QueuedConnection)

    def _claim_device_loss_emit(self) -> bool:
        """Atomically claim the right to emit this session's loss toast.

        Returns ``True`` for exactly one caller per ``start()``; the
        watchdog (worker thread) and the PortAudio finished callback
        (PortAudio internal thread) both race here on the same unplug.
        """
        with self._device_loss_lock:
            if self._device_loss_emitted:
                return False
            self._device_loss_emitted = True
            return True

    def _pa_reset(self) -> None:
        """Force a full PortAudio re-initialization to clear stale device handles.

        After a USB audio device is hot-unplugged, PortAudio's internal host-API
        cache still points at the old (now invalid) device handle.  A subsequent
        ``sd.InputStream()`` open fails with PAErrorCode -10851 (Invalid Property
        Value) even when ``find_input_device_by_name`` has already resolved a
        fresh PortAudio index for the replugged device.  Calling
        ``Pa_Terminate()`` + ``Pa_Initialize()`` forces a full re-enumeration
        from the OS so the next stream open sees the device in its new state.

        Intra-process scope (H-7, audit 4.7/v0.2.9): this is a *process-wide*
        PortAudio operation.  Any other sounddevice streams in the same
        process (e.g. the TX output stream) are invalidated.  Open-SSTV's
        own TX/RX interlock prevents calling both simultaneously, but the
        user can still race the two paths (clicking RX Start while TX is
        mid-encode or mid-PTT-delay) — in that window terminating
        PortAudio would rip the host state out from under the live TX
        OutputStream and crash on the next callback.  The reset runs
        under ``run_if_tx_idle`` (M9, v0.3 audit) so the TX-idle check
        and the terminate/initialize are one atomic section — a TX
        starting concurrently blocks at its counter increment until the
        reset finishes (~100 ms) instead of racing it.  While TX is
        already active the reset is skipped (and logged); the caller
        proceeds without a fresh device cache, which is the right
        trade-off — at worst the user gets a -10851 if the device was
        just unplugged, which is recoverable; at best (the common case)
        nothing changed.

        Embedded-use caveat: if another component in the same Python
        process holds its own ``sd.OutputStream`` / ``sd.InputStream``
        (a co-resident ham-radio tool, a notebook, an IDE plugin), the
        ``is_tx_active`` interlock does *not* see it — only Open-SSTV's
        own TX activity.  A device-loss-recovery here can therefore kill
        an unrelated sibling stream.  This is a limitation of PortAudio's
        global host state, not of this code.  Open-SSTV ships as a
        standalone app, so the embedded case is mostly theoretical; the
        warning is here for anyone who later imports the package.

        Sounddevice-private-API defense (H-1, audit 4.7/v0.2.9):
        ``sd._terminate`` and ``sd._initialize`` are not part of the
        documented public sounddevice API (note the leading underscore).
        They have been stable through every release in our pinned range
        (``sounddevice>=0.4.6,<1``) but could disappear or be renamed in
        a future minor version.  The broad ``except Exception`` below is
        deliberately defensive: if the attributes are removed entirely,
        the attribute lookup itself raises ``AttributeError`` (a subclass
        of ``Exception``) and we degrade gracefully — the user gets a
        logged warning and a slightly stale device cache rather than a
        crash.  Do *not* tighten the except clause without verifying
        upstream's commitment to keeping the symbols around.
        """
        from open_sstv.audio.output_stream import run_if_tx_idle  # noqa: PLC0415

        def _do_reset() -> None:
            _log.info("PortAudio reset: terminating to clear stale device cache")
            try:
                sd._terminate()
            except AttributeError as exc:
                _log.warning(
                    "PortAudio _terminate() removed from sounddevice (%s) — "
                    "device-loss recovery will rely on a stream re-open rather "
                    "than a full host re-init.  Upgrade or pin sounddevice.",
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("PortAudio _terminate() failed: %s", exc)
            _log.info("PortAudio reset: re-initializing")
            try:
                sd._initialize()
            except AttributeError as exc:
                _log.warning(
                    "PortAudio _initialize() removed from sounddevice (%s) — "
                    "subsequent stream opens will use whatever host state "
                    "remains.  Upgrade or pin sounddevice.",
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("PortAudio _initialize() failed: %s", exc)

        if not run_if_tx_idle(_do_reset):
            _log.warning(
                "PortAudio reset SKIPPED — TX is currently active.  Resetting "
                "PortAudio while an OutputStream is live can crash the process; "
                "the device cache will be refreshed on the next RX start that "
                "doesn't overlap a transmission."
            )


__all__ = [
    "DEFAULT_BLOCKSIZE",
    "DEFAULT_SAMPLE_RATE",
    "InputStreamWorker",
]
