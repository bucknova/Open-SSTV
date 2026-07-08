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

    ``error`` is a stable machine code (``"tx_disabled"``,
    ``"not_lease_holder"``, ``"busy"``, ``"bad_token"``,
    ``"confirm_expired"``, ``"no_lease"``) the HTTP layer maps to a status.
    """

    ok: bool
    error: str | None = None
    token: str | None = None


class ControlPlane:
    """Owns remote-TX authority and the transmit lifecycle."""

    def __init__(
        self,
        *,
        now: Callable[[], float],
        transmit: Callable[[str, str], None],
        unkey: Callable[[str], None],
        enabled: Callable[[], bool],
    ) -> None:
        self._now = now
        self._transmit = transmit
        self._unkey = unkey
        self._enabled = enabled
        self._lock = threading.RLock()

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
        unkey_reason: str | None = None
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            if self._state is TxState.TRANSMITTING:
                unkey_reason = "lease_released"
            self._reset_locked()
            self._lease_holder = None
        if unkey_reason:
            self._unkey(unkey_reason)
        return Result(True)

    def reclaim_local(self) -> None:
        """The local GUI takes back control unconditionally — unkeys any
        in-flight remote TX and clears the lease.  Always available."""
        unkey = False
        with self._lock:
            if self._state is TxState.TRANSMITTING:
                unkey = True
            self._reset_locked()
            self._lease_holder = None
        if unkey:
            self._unkey("local_reclaim")

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
            if self._effective_holder() != client_id:
                return Result(False, "not_lease_holder")
            if self._state is not TxState.IDLE:
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
            t = self._now()
            if self._confirm_token is None or not secrets.compare_digest(
                token, self._confirm_token
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
        self._transmit(image_id, mode)
        return Result(True)

    def abort(self, client_id: str) -> Result:
        """Operator-initiated stop of a pending or in-flight transmit."""
        unkey = False
        with self._lock:
            if self._lease_holder != client_id:
                return Result(False, "not_lease_holder")
            if self._state is TxState.TRANSMITTING:
                unkey = True
            elif self._state is TxState.IDLE:
                return Result(False, "busy")
            self._reset_locked()
        if unkey:
            self._unkey("operator_abort")
        return Result(True)

    def on_tx_finished(self) -> None:
        """The app calls this when the TX worker reports the transmission
        ended (normally or via its own abort).  Returns state to IDLE."""
        with self._lock:
            if self._state is TxState.TRANSMITTING:
                self._reset_locked()

    def tick(self) -> None:
        """Periodic safety sweep — the dead-man's-switch lives here.

        Call on a short timer.  Unkeys the rig if the transmitting holder
        stopped heartbeating; expires stale confirm tokens; lapses an idle
        lease whose holder went silent.
        """
        unkey_reason: str | None = None
        with self._lock:
            t = self._now()
            if self._state is TxState.TRANSMITTING:
                if t - self._tx_last_hb > HEARTBEAT_TIMEOUT_S:
                    unkey_reason = "heartbeat_lost"
                    self._reset_locked()
                    self._lease_holder = None  # lost link → drop authority too
            elif self._state is TxState.AWAITING_CONFIRM:
                if t >= self._confirm_expires:
                    self._reset_locked()
            if (
                self._lease_holder is not None
                and self._state is TxState.IDLE
                and t - self._lease_seen > LEASE_TIMEOUT_S
            ):
                self._lease_holder = None
        if unkey_reason:
            _log.warning("remote TX dead-man's-switch: %s → unkey", unkey_reason)
            self._unkey(unkey_reason)

    # -- introspection -------------------------------------------------

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
