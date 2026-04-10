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

import queue
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, QTimer, Signal, Slot

from sstv_app.audio.devices import AudioDevice

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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[NDArray[np.float32]] = queue.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._stream: sd.InputStream | None = None
        self._timer: QTimer | None = None
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._dropped_chunks: int = 0

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
        self._dropped_chunks = 0

        # Drain any stale chunks from a previous session before the
        # callback starts pushing new ones. Queue lives on the worker
        # thread so this is safe without a lock.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        try:
            self._stream = sd.InputStream(
                samplerate=sample_rate,
                blocksize=blocksize,
                device=device_index,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 — surface anything to UI
            self._stream = None
            self.error.emit(f"Could not open input stream: {exc}")
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

        self.started.emit()

    @Slot()
    def stop(self) -> None:
        """Stop the PortAudio stream and flush any buffered chunks.

        Idempotent: calling ``stop`` on an already-stopped worker is
        a no-op and does not emit ``stopped`` a second time.
        """
        if self._stream is None:
            return

        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Error closing input stream: {exc}")
        finally:
            self._stream = None

        # Emit any residual chunks so the consumer gets a clean
        # tail-flush before we report stopped. This matters for
        # decode_wav-style consumers that want to finish whatever
        # image was in-flight when the user clicked Stop.
        self._drain_queue()

        if self._dropped_chunks > 0:
            self.error.emit(
                f"Input overflow: dropped {self._dropped_chunks} chunks"
            )

        self.stopped.emit()

    # === internal ===

    def _audio_callback(
        self,
        indata: "NDArray[np.float32]",
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
            # Record as a drop and keep running.
            self._dropped_chunks += 1

        # Flatten to 1-D mono. ``channels=1`` in ``InputStream`` gives
        # us shape (frames, 1); pull out the column and copy so the
        # downstream consumer owns its buffer. ``.copy()`` is mandatory:
        # ``np.ascontiguousarray`` skips the copy when the slice is
        # already contiguous (which it is for a single-column array),
        # leaving a view into PortAudio's recycled buffer that gets
        # overwritten by the next callback before the consumer drains
        # the queue.
        chunk = indata[:, 0].copy()

        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            # Consumer is stalled — drop the newest chunk rather than
            # block the RT thread. The drop counter is surfaced via
            # ``error`` when we eventually stop or the next drain.
            self._dropped_chunks += 1

    @Slot()
    def _drain_queue(self) -> None:
        """Pull every pending chunk off the queue and emit it.

        Runs on the worker thread (via ``QTimer.timeout``). The drain
        is non-blocking so a steady state where PortAudio fills the
        queue faster than the timer fires is still bounded: every
        drain empties the queue completely.
        """
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            self.chunk_ready.emit(chunk)


__all__ = [
    "DEFAULT_BLOCKSIZE",
    "DEFAULT_SAMPLE_RATE",
    "InputStreamWorker",
]
