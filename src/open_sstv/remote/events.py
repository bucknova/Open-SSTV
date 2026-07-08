# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process publish/subscribe hub for the remote live event stream.

The view plane is one-directional — the app *pushes* RX progress and
gallery updates to browsers, which never push back — so the transport is
**Server-Sent Events** (SSE) over the Phase 1 stdlib server, not
WebSockets.  This hub is the fan-out behind it:

- The app (on the Qt GUI thread, from :class:`RxWorker` signal bridges)
  calls :meth:`publish` with a small JSON-able event.
- Each connected SSE client runs in its own request thread and holds a
  :class:`~queue.Queue` obtained from :meth:`subscribe`; it blocks on that
  queue and writes each event out as an SSE frame.

Thread-safe by construction: publishing just enqueues onto each
subscriber's queue.  A slow or wedged client cannot grow memory without
bound — its queue is capped and the *oldest* event is dropped to make
room (live progress is disposable; the next frame supersedes it).
"""
from __future__ import annotations

import logging
import queue
import threading

_log = logging.getLogger(__name__)

#: Per-subscriber backlog cap.  RX progress is ~a few events/sec; 64
#: leaves comfortable headroom while bounding a stalled client's memory.
_MAX_QUEUE = 64


class EventHub:
    """Fan-out of live events to all connected SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, object]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, object]]:
        """Register a new subscriber and return its event queue."""
        q: queue.Queue[dict[str, object]] = queue.Queue(maxsize=_MAX_QUEUE)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, object]]) -> None:
        """Drop a subscriber (client disconnected).  Safe to call twice."""
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict[str, object]) -> None:
        """Enqueue *event* onto every subscriber, dropping oldest if full.

        Never blocks and never raises — a misbehaving subscriber can't
        stall the publisher (the Qt GUI thread).
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Discard the stalest event to make room for the newest —
                # live progress that no one drained is already obsolete.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except queue.Empty:  # pragma: no cover - raced with a drain
                    pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


__all__ = ["EventHub"]
