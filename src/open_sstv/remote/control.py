# SPDX-License-Identifier: GPL-3.0-or-later
"""Control plane — the safety logic for remote transmit.

This module is the **reference monitor** for remote TX: every decision
about whether the rig may be keyed lives here, as pure, headless-testable
logic.  It touches no Qt and no rig — the app injects three callables:

- ``now()``       — a monotonic clock (injectable so the dead-man's-switch
                    can be tested with a fake clock);
- ``transmit(image_id, mode)`` — start a transmission (the app marshals
                    this onto the Qt thread and emits the same
                    ``_request_transmit`` signal the desktop Send button
                    uses — the web is just another button);
- ``unkey(reason)`` — abort/stop and unkey the rig (the app routes this
                    through the v0.4.1 watchdog/unkey path).

The safety invariants, all enforced here:

1. **Off by default.** Nothing transmits unless ``enabled()`` (the
   ``remote_tx_enabled`` config gate) is true — separate from viewing.
2. **Single-writer lease.** Exactly one client may hold TX authority at a
   time; only the holder can request/confirm/abort. The local GUI can
   reclaim unconditionally (:meth:`reclaim_local`).
3. **Per-transmit confirmation.** ``request`` returns a short-lived token;
   the rig is not keyed until ``confirm(token)`` arrives inside the window.
4. **Dead-man's-switch.** While transmitting, the holder must heartbeat;
   if heartbeats stop for :data:`HEARTBEAT_TIMEOUT_S`, :meth:`tick`
   unkeys the rig automatically.

:meth:`tick` is meant to be called on a short timer (the app uses a
QTimer; tests call it with a fake clock).  All public methods are
lock-guarded and re-entrancy-safe: the injected callbacks are invoked
*after* the lock is released.
"""
from __future__ import annotations

import logging
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)

#: A ``request`` token is valid this long before it must be re-requested.
CONFIRM_WINDOW_S = 30.0
#: Dead-man's-switch: no heartbeat for this long *during TX* → unkey.
HEARTBEAT_TIMEOUT_S = 2.5
#: An idle lease lapses if the holder goes silent this long, freeing it
#: for another operator.  (During TX the faster dead-man's-switch applies.)
LEASE_TIMEOUT_S = 15.0


class TxState(StrEnum):
    """Where the remote transmit path is right now."""

    IDLE = "idle"
    AWAITING_CONFIRM = "awaiting_confirm"
    TRANSMITTING = "transmitting"


@dataclass(frozen=True)
class Result:
    """Outcome of a control-plane call.

    ``error`` is a stable machine code (``"tx_disabled"``, ``"no_rig"``,
    ``"not_lease_holder"``, ``"busy"``, ``"bad_token"``,
    ``"confirm_expired"``, ``"no_lease"``) the HTTP layer maps to a status.
    """

    ok: bool
    error: str | None = None
    token: str | None = None


class ControlPlane:
    """Owns remote-TX authority and the transmit lifecycle.

    **Callback contract.** ``transmit`` and ``unkey`` are queued while the
    state lock is held and then dispatched *after* it is released, in the
    order they were queued.  A key can therefore never be dispatched after
    the unkey that supersedes it, without the lock being held across the
    callback itself.

    That last part matters: the callbacks reach the rig, and rig I/O
    blocks.  ``unkey`` in the app performs a synchronous PTT-off over
    serial or TCP, which can take seconds on a wedged link.  Invoking it
    under the lock meant every other caller — including ``reclaim_local``,
    which is the first statement of the app's ``closeEvent``, and
    ``status`` on the GUI thread at ~10 Hz during TX — blocked behind it.
    The observed symptom was the app appearing to hang for over a minute at
    quit while the dead-man's-switch was trying to unkey a stuck rig.

    Callbacks should still be quick and **must not re-enter** this object.
    Both must also travel the **same ordered channel** so they *execute* in
    dispatch order too (e.g. both marshalled onto the Qt thread), or the
    ordering guarantee stops at dispatch.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        transmit: Callable[[str, str], None],
        unkey: Callable[[str], None],
        enabled: Callable[[], bool],
        rig_ready: Callable[[], bool] | None = None,
        tx_busy: Callable[[], bool] | None = None,
    ) -> None:
        self._now = now
        self._transmit = transmit
        self._unkey = unkey
        self._enabled = enabled
        #: Whether a rig that can be positively keyed is connected.  Remote
        #: TX is unattended, so we refuse it on the no-op manual/VOX backend
        #: where PTT isn't commanded and the dead-man's-switch could only
        #: stop audio, not drop PTT.  Defaults to always-ready for headless
        #: tests; the app passes "a CAT rig is connected".
        self._rig_ready = rig_ready if rig_ready is not None else (lambda: True)
        #: Whether the transmitter is *already* busy with a transmission this
        #: plane did not start — i.e. the local operator pressed Send.  The
        #: plane owns remote TX authority, not the radio, so without this it
        #: happily granted a remote request during a local transmission: the
        #: image queued behind the local one, the local completion reset the
        #: plane to IDLE, and the worker then keyed for the remote image with
        #: the plane reporting idle — abort answering "busy", reclaim_local a
        #: no-op, and the dead-man's-switch never armed.  Defaults to
        #: never-busy for headless tests; the app passes the TX worker.
        self._tx_busy = tx_busy if tx_busy is not None else (lambda: False)
        self._lock = threading.RLock()
        #: Callbacks queued under _lock, dispatched after it is released.
        #: _dispatch_lock serialises drainers so queue order is also
        #: execution order.  See the callback contract above.
        self._pending_dispatch: deque[tuple[str, tuple[object, ...]]] = deque()
        self._dispatch_lock = threading.Lock()

        self._state = TxState.IDLE
        self._lease_holder: str | None = None
        self._lease_seen = 0.0            # last activity from the holder
        self._confirm_token: str | None = None
        self._confirm_expires = 0.0
        self._pending: tuple[str, str] | None = None  # (image_id, mode)
        self._tx_last_hb = 0.0            # last heartbeat while transmitting

    # -- lease ---------------------------------------------------------

    def take_lease(self, client_id: str) -> Result:
        """Acquire TX authority.  Granted if free, already yours, or the
        current holder's lease has lapsed; otherwise ``"busy"``."""
        with self._lock:
            holder = self._effective_holder()
            if holder is not None and holder != client_id:
                return Result(False, "busy")
            self._lease_holder = client_id
            self._lease_seen = self._now()
            return Result(True)

    def release_lease(self, client_id: str) -> Result:
        """Give up TX authority.  Aborts + unkeys if mid-transmit."""
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            was_tx = self._state is TxState.TRANSMITTING
            self._reset_locked()
            self._lease_holder = None
            if was_tx:
                self._queue_locked("unkey", "lease_released")
        self._drain()
        return Result(True)

    def reclaim_local(self) -> None:
        """The local GUI takes back control unconditionally — unkeys any
        in-flight remote TX and clears the lease.  Always available."""
        with self._lock:
            was_tx = self._state is TxState.TRANSMITTING
            self._reset_locked()
            self._lease_holder = None
            if was_tx:
                self._queue_locked("unkey", "local_reclaim")
        self._drain()

    def heartbeat(self, client_id: str) -> Result:
        """Liveness ping from the holder.  Refreshes the lease and, while
        transmitting, the dead-man's-switch clock."""
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            t = self._now()
            self._lease_seen = t
            if self._state is TxState.TRANSMITTING:
                self._tx_last_hb = t
            return Result(True)

    # -- transmit lifecycle -------------------------------------------

    def request(self, client_id: str, image_id: str, mode: str) -> Result:
        """Ask to transmit *image_id* in *mode*.  Returns a confirm token.

        Gated on: remote TX enabled, caller holds the lease, and nothing
        else is in flight.
        """
        with self._lock:
            if not self._enabled():
                return Result(False, "tx_disabled")
            if not self._rig_ready():
                return Result(False, "no_rig")
            if self._effective_holder() != client_id:
                return Result(False, "not_lease_holder")
            if self._state is not TxState.IDLE:
                return Result(False, "busy")
            # The radio may be transmitting something this plane did not
            # start (the local Send button).  Queuing behind it would key
            # the rig later with the plane reporting idle.
            if self._tx_busy():
                return Result(False, "busy")
            t = self._now()
            self._lease_seen = t
            token = secrets.token_urlsafe(12)
            self._confirm_token = token
            self._confirm_expires = t + CONFIRM_WINDOW_S
            self._pending = (image_id, mode)
            self._state = TxState.AWAITING_CONFIRM
            return Result(True, token=token)

    def confirm(self, client_id: str, token: str) -> Result:
        """Confirm a pending request → key the rig.  The single point that
        actually starts a transmission."""
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            if self._state is not TxState.AWAITING_CONFIRM:
                return Result(False, "busy")
            # Re-check the gate at the one point that actually keys the rig,
            # so a request made while enabled can't confirm after it's been
            # disabled (defence-in-depth vs. relying on reclaim_local).
            if not self._enabled():
                self._reset_locked()
                return Result(False, "tx_disabled")
            # Re-check at the keying edge: the rig could have been disconnected
            # between request and confirm.
            if not self._rig_ready():
                self._reset_locked()
                return Result(False, "no_rig")
            # Re-check at the keying edge: the local operator may have
            # pressed Send between request and confirm.
            if self._tx_busy():
                self._reset_locked()
                return Result(False, "busy")
            t = self._now()
            # Compare as bytes: hmac/secrets refuse to compare str with any
            # non-ASCII character and raise TypeError, which would escape as
            # an unhandled error instead of a clean "bad_token".
            if self._confirm_token is None or not secrets.compare_digest(
                token.encode("utf-8", "surrogateescape"),
                self._confirm_token.encode("utf-8", "surrogateescape"),
            ):
                return Result(False, "bad_token")
            if t >= self._confirm_expires:
                self._reset_locked()
                return Result(False, "confirm_expired")
            assert self._pending is not None
            image_id, mode = self._pending
            self._state = TxState.TRANSMITTING
            self._tx_last_hb = t
            self._lease_seen = t
            self._confirm_token = None
            # Queued under the lock so no concurrent abort/reclaim can slip
            # its unkey ahead of this key; dispatched below, outside it.
            self._queue_locked("transmit", image_id, mode)
        self._drain()
        return Result(True)

    def abort(self, client_id: str) -> Result:
        """Operator-initiated stop of a pending or in-flight transmit."""
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            if self._state is TxState.IDLE:
                return Result(False, "busy")
            was_tx = self._state is TxState.TRANSMITTING
            self._reset_locked()
            if was_tx:
                self._queue_locked("unkey", "operator_abort")
        self._drain()
        return Result(True)

    def on_tx_finished(self) -> None:
        """The app calls this when the TX worker reports the transmission
        ended (normally or via its own abort).  Returns state to IDLE."""
        with self._lock:
            if self._state is TxState.TRANSMITTING:
                self._reset_locked()

    def tick(self) -> None:
        """Periodic safety sweep — the dead-man's-switch lives here.

        Call on a short timer (well under HEARTBEAT_TIMEOUT_S).  Unkeys the
        rig if the transmitting holder stopped heartbeating OR remote TX was
        disabled mid-transmission; expires stale confirm tokens; lapses an
        idle lease whose holder went silent.
        """
        with self._lock:
            t = self._now()
            if self._state is TxState.TRANSMITTING:
                unkey_reason: str | None = None
                if t - self._tx_last_hb > HEARTBEAT_TIMEOUT_S:
                    unkey_reason = "heartbeat_lost"
                elif not self._enabled():
                    # Gate revoked mid-TX — enforce it continuously, not
                    # just at request time.
                    unkey_reason = "tx_disabled"
                if unkey_reason is not None:
                    self._reset_locked()
                    self._lease_holder = None  # lost link / revoked → drop authority
                    _log.warning("remote TX safety sweep: %s → unkey", unkey_reason)
                    self._queue_locked("unkey", unkey_reason)
            elif self._state is TxState.AWAITING_CONFIRM:
                if t >= self._confirm_expires:
                    self._reset_locked()
            if (
                self._lease_holder is not None
                and self._state is TxState.IDLE
                and t - self._lease_seen > LEASE_TIMEOUT_S
            ):
                self._lease_holder = None
        self._drain()

    # -- introspection -------------------------------------------------

    # -- deferred callback dispatch ------------------------------------

    def _queue_locked(self, kind: str, *args: object) -> None:
        """Queue a callback for dispatch after the lock is released.

        Must be called with ``_lock`` held: queue order is what preserves
        the key-never-after-its-unkey ordering guarantee.
        """
        self._pending_dispatch.append((kind, args))

    def _drain(self) -> None:
        """Invoke queued callbacks in order, with ``_lock`` NOT held.

        ``_dispatch_lock`` serialises drainers, so two threads that queued
        under ``_lock`` still execute their callbacks in the order they
        queued them.  Callbacks reach the rig and can block for seconds;
        that must never stall another caller's state transition.
        """
        with self._dispatch_lock:
            while True:
                with self._lock:
                    if not self._pending_dispatch:
                        return
                    kind, args = self._pending_dispatch.popleft()
                if kind == "transmit":
                    self._transmit(*args)  # type: ignore[arg-type]
                else:
                    self._unkey(*args)  # type: ignore[arg-type]

    def status(self) -> dict[str, object]:
        """Snapshot for the SSE ``tx.state`` event / the HTTP layer."""
        with self._lock:
            return {
                "state": self._state.value,
                "lease_held": self._lease_holder is not None,
                "tx_enabled": bool(self._enabled()),
            }

    def holds_lease(self, client_id: str) -> bool:
        with self._lock:
            return self._effective_holder() == client_id

    # -- internals -----------------------------------------------------

    def _effective_holder(self) -> str | None:
        """Current holder, treating a lapsed idle lease as free."""
        if self._lease_holder is None:
            return None
        if (
            self._state is TxState.IDLE
            and self._now() - self._lease_seen > LEASE_TIMEOUT_S
        ):
            self._lease_holder = None
        return self._lease_holder

    def _reset_locked(self) -> None:
        """Return the transmit lifecycle to IDLE (keeps the lease)."""
        self._state = TxState.IDLE
        self._confirm_token = None
        self._confirm_expires = 0.0
        self._pending = None
        self._tx_last_hb = 0.0


__all__ = [
    "CONFIRM_WINDOW_S",
    "HEARTBEAT_TIMEOUT_S",
    "LEASE_TIMEOUT_S",
    "ControlPlane",
    "Result",
    "TxState",
]
