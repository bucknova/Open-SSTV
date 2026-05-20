# SPDX-License-Identifier: GPL-3.0-or-later
"""TCI audio input worker — drop-in replacement for InputStreamWorker.

Receives RX audio from a ``TciConnection`` (binary WebSocket frames using
the 64-byte TCI v2.0 header) and emits ``chunk_ready(np.ndarray[float32])``
on a 50 ms Qt timer, exactly matching the public signal/slot interface of
``InputStreamWorker``.

Negotiates float32 mono 48 kHz with the server before subscribing via
``audio_start:0;``.  ``TciConnection`` decodes the header, converts any
int16/float32 payload to float32, and downmixes stereo to mono before
invoking registered audio callbacks, so the chunk that arrives here is
always a clean float32 mono array ready to pass to RxWorker.

The audio callback runs on the WebSocket recv thread.  Like the PortAudio
RT callback, it must be fast and non-blocking — it pushes samples onto a
bounded queue and returns immediately.  The 50 ms QTimer drains the queue
on the worker thread and emits chunk_ready.
"""
from __future__ import annotations

import logging
import queue

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal, Slot

_log = logging.getLogger(__name__)

_QUEUE_MAXSIZE: int = 256
_POLL_INTERVAL_MS: int = 50


class TciInputStreamWorker(QObject):
    """Receive RX audio from a TCI server and feed it to RxWorker.

    Drop-in for ``InputStreamWorker``: same signals, same ``start``/``stop``
    slots.  The ``device``, ``sample_rate``, and ``blocksize`` arguments to
    ``start`` are accepted but ``device`` and ``blocksize`` are ignored —
    TCI delivers audio at the server-negotiated sample rate with its own
    block size.

    Usage::

        worker = TciInputStreamWorker(tci_rig)
        worker.moveToThread(thread)
        thread.started.connect(lambda: worker.start())
        worker.chunk_ready.connect(rx_worker.feed_chunk)
        thread.start()
    """

    chunk_ready = Signal(object)   # np.ndarray[float32]
    started = Signal()
    stopped = Signal()
    error = Signal(str)
    stream_error = Signal(str)     # emitted on connection loss

    def __init__(self, tci_rig: object, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rig = tci_rig  # TciRig — we use .connection
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._timer: QTimer | None = None
        self._running: bool = False
        self._dropped_chunks: int = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def sample_rate(self) -> int:
        return self._rig.connection.sample_rate

    # === slots ===

    @Slot(object, int, int)
    def start(
        self,
        device: object = None,  # noqa: ARG002 — not used for TCI
        sample_rate: int = 48_000,  # noqa: ARG002 — server dictates rate
        blocksize: int = 1024,  # noqa: ARG002
    ) -> None:
        """Subscribe to TCI RX audio and start the drain timer."""
        if self._running:
            self.error.emit("TCI input stream already running; stop first")
            return

        self._dropped_chunks = 0
        self._running = True

        # Drain stale chunks from a previous session.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        conn = self._rig.connection
        conn.register_audio_callback(self._audio_callback)
        try:
            # Negotiate format before subscribing: 48 kHz, float32, mono.
            # AetherSDR echoes each command back as confirmation.
            conn.send("audio_samplerate:48000;")
            conn.send("audio_stream_sample_type:float32;")
            conn.send("audio_stream_channels:1;")
            conn.send("audio_start:0;")
            # H6: announce the subscription so TxWorker doesn't double-
            # start (and double-leak) the RX stream on each transmission.
            conn.mark_rx_audio_subscribed(True)
        except Exception as exc:  # noqa: BLE001
            conn.unregister_audio_callback(self._audio_callback)
            self._running = False
            self.error.emit(f"TCI start audio failed: {exc}")
            self.stopped.emit()
            return

        self._timer = QTimer()
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._drain_queue)
        self._timer.start()

        self.started.emit()

    @Slot()
    def stop(self) -> None:
        """Unsubscribe from TCI audio and flush pending chunks."""
        if not self._running:
            return

        self._running = False

        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

        conn = self._rig.connection
        conn.unregister_audio_callback(self._audio_callback)
        try:
            conn.send("audio_stop:0;")
        except Exception:  # noqa: BLE001
            pass
        # H6: clear the subscription flag regardless of whether the stop
        # command actually reached the server — from the client's point
        # of view we are no longer interested in RX audio.
        conn.mark_rx_audio_subscribed(False)

        # M14: discard any chunks the recv thread enqueued just before
        # unregister.  Previously we called ``_drain_queue`` here, which
        # emitted ``chunk_ready`` for those stale samples and reseeded a
        # downstream RxWorker that ``_swap_audio_worker`` had already
        # reset — corrupting the next decode.  Audio that's <100 ms old
        # is not worth saving across a worker swap; drop the queue
        # silently instead.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        if self._dropped_chunks > 0:
            self.error.emit(
                f"TCI input overflow: dropped {self._dropped_chunks} chunks"
            )

        self.stopped.emit()

    # === internal ===

    def _audio_callback(self, samples: np.ndarray, sample_rate: int) -> None:  # noqa: ARG002
        """Called on the WebSocket recv thread for each binary audio frame.

        ``samples`` is already float32 mono, decoded by ``TciConnection``.
        Fast and non-blocking — mirrors the PortAudio RT callback contract.

        Mirrors the teardown guard from ``InputStreamWorker._audio_callback``:
        the recv thread can fire this callback after the worker's
        ``deleteLater`` has run (or during shutdown), at which point
        ``self._queue`` may already be cleared from ``__dict__`` by GC.
        ``getattr`` avoids an ``AttributeError`` that would otherwise be
        silently swallowed by ``TciConnection._dispatch_audio`` and mask
        the teardown race.
        """
        q = getattr(self, "_queue", None)
        if q is None:
            return
        try:
            q.put_nowait(samples)
        except queue.Full:
            self._dropped_chunks += 1

    @Slot()
    def _drain_queue(self) -> None:
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            self.chunk_ready.emit(chunk)


__all__ = ["TciInputStreamWorker"]
