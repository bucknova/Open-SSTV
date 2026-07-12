# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.events — EventHub pub/sub fan-out and backpressure."""
from __future__ import annotations

import queue

from open_sstv.remote.events import EventHub


class TestFanOut:
    def test_publish_reaches_every_subscriber(self) -> None:
        hub = EventHub()
        a, b = hub.subscribe(), hub.subscribe()
        hub.publish({"type": "rx.started"})
        assert a.get_nowait() == {"type": "rx.started"}
        assert b.get_nowait() == {"type": "rx.started"}

    def test_unsubscribe_stops_delivery(self) -> None:
        hub = EventHub()
        a = hub.subscribe()
        hub.unsubscribe(a)
        hub.publish({"type": "x"})
        assert a.qsize() == 0

    def test_unsubscribe_is_idempotent(self) -> None:
        hub = EventHub()
        a = hub.subscribe()
        hub.unsubscribe(a)
        hub.unsubscribe(a)  # must not raise

    def test_subscriber_count(self) -> None:
        hub = EventHub()
        assert hub.subscriber_count == 0
        a = hub.subscribe()
        assert hub.subscriber_count == 1
        hub.unsubscribe(a)
        assert hub.subscriber_count == 0

    def test_publish_with_no_subscribers_is_noop(self) -> None:
        EventHub().publish({"type": "x"})  # must not raise

    def test_subscribe_cap_rejects_beyond_limit(self) -> None:
        hub = EventHub()
        a = hub.subscribe(max_subscribers=2)
        b = hub.subscribe(max_subscribers=2)
        c = hub.subscribe(max_subscribers=2)  # over the cap
        assert a is not None and b is not None
        assert c is None
        assert hub.subscriber_count == 2
        # A slot frees up when one unsubscribes.
        hub.unsubscribe(a)
        assert hub.subscribe(max_subscribers=2) is not None


class TestBackpressure:
    def test_full_queue_drops_oldest_not_newest(self) -> None:
        # A stalled subscriber must not grow without bound: the oldest
        # (stalest) event is discarded so the freshest always lands.
        hub = EventHub()
        sub = hub.subscribe()
        total = 200  # well past the 64 cap
        for i in range(total):
            hub.publish({"n": i})
        drained = []
        try:
            while True:
                drained.append(sub.get_nowait()["n"])
        except queue.Empty:
            pass
        assert len(drained) <= 64
        # The newest event survived; the very first was dropped.
        assert drained[-1] == total - 1
        assert 0 not in drained
