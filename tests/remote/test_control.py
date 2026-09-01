# SPDX-License-Identifier: GPL-3.0-or-later
"""remote.control — the remote-TX safety state machine (no rig, fake clock)."""
from __future__ import annotations

import pytest

from open_sstv.remote.control import (
    CONFIRM_WINDOW_S,
    HEARTBEAT_TIMEOUT_S,
    LEASE_TIMEOUT_S,
    ControlPlane,
    TxState,
)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Spy:
    def __init__(self) -> None:
        self.transmits: list[tuple[str, str]] = []
        self.unkeys: list[str] = []

    def transmit(self, image_id: str, mode: str) -> None:
        self.transmits.append((image_id, mode))

    def unkey(self, reason: str) -> None:
        self.unkeys.append(reason)


def _cp(
    clock: Clock, spy: Spy, enabled: bool = True, rig_ready: bool = True
) -> ControlPlane:
    return ControlPlane(
        now=clock, transmit=spy.transmit, unkey=spy.unkey,
        enabled=lambda: enabled, rig_ready=lambda: rig_ready,
    )


def _armed(clock: Clock, spy: Spy) -> ControlPlane:
    """A control plane with client 'A' holding the lease and transmitting."""
    cp = _cp(clock, spy)
    assert cp.take_lease("A").ok
    tok = cp.request("A", "img1", "martin_m1").token
    assert tok is not None
    assert cp.confirm("A", tok).ok
    return cp


class TestLease:
    def test_take_when_free(self) -> None:
        cp = _cp(Clock(), Spy())
        assert cp.take_lease("A").ok
        assert cp.holds_lease("A")

    def test_second_client_is_busy(self) -> None:
        cp = _cp(Clock(), Spy())
        cp.take_lease("A")
        r = cp.take_lease("B")
        assert not r.ok and r.error == "busy"

    def test_same_client_can_retake(self) -> None:
        cp = _cp(Clock(), Spy())
        cp.take_lease("A")
        assert cp.take_lease("A").ok

    def test_release_frees_it(self) -> None:
        cp = _cp(Clock(), Spy())
        cp.take_lease("A")
        assert cp.release_lease("A").ok
        assert cp.take_lease("B").ok

    def test_idle_lease_lapses_after_silence(self) -> None:
        clock = Clock()
        cp = _cp(clock, Spy())
        cp.take_lease("A")
        clock.advance(LEASE_TIMEOUT_S + 1)
        # A went silent → B may take over.
        assert cp.take_lease("B").ok

    def test_tick_clears_lapsed_idle_lease(self) -> None:
        clock = Clock()
        cp = _cp(clock, Spy())
        cp.take_lease("A")
        clock.advance(LEASE_TIMEOUT_S + 1)
        cp.tick()
        assert not cp.holds_lease("A")


class TestGating:
    def test_request_denied_when_tx_disabled(self) -> None:
        cp = _cp(Clock(), Spy(), enabled=False)
        cp.take_lease("A")
        r = cp.request("A", "img1", "martin_m1")
        assert not r.ok and r.error == "tx_disabled"

    def test_request_denied_when_no_rig(self) -> None:
        cp = _cp(Clock(), Spy(), rig_ready=False)
        cp.take_lease("A")
        r = cp.request("A", "img1", "martin_m1")
        assert not r.ok and r.error == "no_rig"

    def test_confirm_denied_if_rig_disconnects_after_request(self) -> None:
        # Rig present at request, gone by confirm → refuse at the keying edge.
        clock, spy = Clock(), Spy()
        ready = {"on": True}
        cp = ControlPlane(
            now=clock, transmit=spy.transmit, unkey=spy.unkey,
            enabled=lambda: True, rig_ready=lambda: ready["on"],
        )
        cp.take_lease("A")
        tok = cp.request("A", "img1", "martin_m1").token
        assert tok is not None
        ready["on"] = False
        r = cp.confirm("A", tok)
        assert not r.ok and r.error == "no_rig"
        assert spy.transmits == [], "must not key with no rig"

    def test_request_denied_without_lease(self) -> None:
        cp = _cp(Clock(), Spy())
        r = cp.request("A", "img1", "martin_m1")
        assert not r.ok and r.error == "not_lease_holder"

    def test_non_holder_cannot_confirm_or_abort(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        tok = cp.request("A", "img1", "martin_m1").token
        assert cp.confirm("B", tok or "").error == "not_lease_holder"
        assert cp.abort("B").error == "not_lease_holder"


class TestConfirm:
    def test_confirm_keys_the_rig(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        tok = cp.request("A", "imgX", "scottie_s1").token
        assert tok is not None
        assert cp.confirm("A", tok).ok
        assert spy.transmits == [("imgX", "scottie_s1")]

    def test_bad_token_rejected(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        cp.request("A", "img1", "martin_m1")
        assert cp.confirm("A", "not-the-token").error == "bad_token"
        assert spy.transmits == []

    def test_confirm_rechecks_gate_disabled_after_request(self) -> None:
        # Gate ON at request, OFF by confirm time — the confirm edge (the
        # one point that keys the rig) must refuse, not lean on reclaim.
        clock, spy = Clock(), Spy()
        gate = {"on": True}
        cp = ControlPlane(
            now=clock, transmit=spy.transmit, unkey=spy.unkey,
            enabled=lambda: gate["on"],
        )
        cp.take_lease("A")
        tok = cp.request("A", "img1", "martin_m1").token
        gate["on"] = False
        r = cp.confirm("A", tok or "")
        assert not r.ok and r.error == "tx_disabled"
        assert spy.transmits == []

    def test_confirm_after_window_expires(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        tok = cp.request("A", "img1", "martin_m1").token
        clock.advance(CONFIRM_WINDOW_S + 1)
        r = cp.confirm("A", tok or "")
        assert not r.ok and r.error == "confirm_expired"
        assert spy.transmits == []

    def test_tick_expires_stale_pending(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        cp.request("A", "img1", "martin_m1")
        clock.advance(CONFIRM_WINDOW_S + 1)
        cp.tick()
        assert cp.status()["state"] == TxState.IDLE.value

    def test_cannot_request_twice(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        cp.request("A", "img1", "martin_m1")
        assert cp.request("A", "img2", "martin_m1").error == "busy"


class TestDeadMansSwitch:
    def test_lost_heartbeat_unkeys(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        clock.advance(HEARTBEAT_TIMEOUT_S + 0.1)
        cp.tick()
        assert spy.unkeys == ["heartbeat_lost"]
        assert cp.status()["state"] == TxState.IDLE.value
        assert not cp.holds_lease("A")  # lost link drops authority

    def test_disabling_gate_mid_tx_unkeys(self) -> None:
        # The enable gate is enforced continuously, not just at request.
        clock, spy = Clock(), Spy()
        gate = {"on": True}
        cp = ControlPlane(
            now=clock, transmit=spy.transmit, unkey=spy.unkey,
            enabled=lambda: gate["on"],
        )
        cp.take_lease("A")
        tok = cp.request("A", "img1", "martin_m1").token
        assert cp.confirm("A", tok or "").ok
        gate["on"] = False  # operator turns remote TX off during a transmission
        cp.tick()
        assert spy.unkeys == ["tx_disabled"]
        assert cp.status()["state"] == TxState.IDLE.value

    def test_heartbeats_keep_tx_alive(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        for _ in range(10):
            clock.advance(1.0)
            cp.heartbeat("A")
            cp.tick()
        assert spy.unkeys == []
        assert cp.status()["state"] == TxState.TRANSMITTING.value

    def test_tick_below_timeout_does_not_unkey(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        clock.advance(HEARTBEAT_TIMEOUT_S - 0.1)
        cp.tick()
        assert spy.unkeys == []


class TestAbortAndReclaim:
    def test_operator_abort_unkeys(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        assert cp.abort("A").ok
        assert spy.unkeys == ["operator_abort"]
        assert cp.status()["state"] == TxState.IDLE.value

    def test_abort_pending_does_not_unkey(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _cp(clock, spy)
        cp.take_lease("A")
        cp.request("A", "img1", "martin_m1")
        assert cp.abort("A").ok
        assert spy.unkeys == []  # nothing was keyed yet
        assert cp.status()["state"] == TxState.IDLE.value

    def test_release_during_tx_unkeys(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        assert cp.release_lease("A").ok
        assert spy.unkeys == ["lease_released"]

    def test_local_reclaim_unkeys_and_drops_lease(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        cp.reclaim_local()
        assert spy.unkeys == ["local_reclaim"]
        assert not cp.holds_lease("A")
        # After a local reclaim, a remote must re-acquire.
        assert cp.take_lease("A").ok

    def test_on_tx_finished_returns_to_idle(self) -> None:
        clock, spy = Clock(), Spy()
        cp = _armed(clock, spy)
        cp.on_tx_finished()
        assert cp.status()["state"] == TxState.IDLE.value
        assert spy.unkeys == []  # normal completion, no unkey


class TestStatus:
    def test_status_reflects_enable_gate(self) -> None:
        assert _cp(Clock(), Spy(), enabled=False).status()["tx_enabled"] is False
        assert _cp(Clock(), Spy(), enabled=True).status()["tx_enabled"] is True

    @pytest.mark.parametrize("held", [True, False])
    def test_status_lease_flag(self, held: bool) -> None:
        cp = _cp(Clock(), Spy())
        if held:
            cp.take_lease("A")
        assert cp.status()["lease_held"] is held


# ---------------------------------------------------------------------------
# The transmitter may already be busy with a transmission this plane did not
# start (the local Send button).
# ---------------------------------------------------------------------------


class TestLocalTxBlocksRemote:
    """A local transmission must block remote request/confirm.

    Regression: the plane gated only on its own state, so during a local TX
    it reported idle and granted a remote request.  The image queued behind
    the local one; the local completion reset the plane to IDLE; the worker
    then keyed for the remote image with the plane reporting idle — abort
    answering "busy", reclaim_local a no-op, and no dead-man's-switch.
    """

    def _plane(self, busy: list[bool]):
        keyed: list[tuple[str, str]] = []
        unkeyed: list[str] = []
        plane = ControlPlane(
            now=lambda: 1000.0,
            transmit=lambda i, m: keyed.append((i, m)),
            unkey=unkeyed.append,
            enabled=lambda: True,
            rig_ready=lambda: True,
            tx_busy=lambda: busy[0],
        )
        return plane, keyed, unkeyed

    def test_request_refused_while_local_tx_running(self) -> None:
        busy = [True]
        plane, keyed, _ = self._plane(busy)
        assert plane.take_lease("phone").ok
        res = plane.request("phone", "img-1", "scottie_s1")
        assert not res.ok
        assert res.error == "busy"
        assert keyed == []

    def test_request_allowed_once_local_tx_ends(self) -> None:
        busy = [True]
        plane, keyed, _ = self._plane(busy)
        plane.take_lease("phone")
        assert not plane.request("phone", "img-1", "scottie_s1").ok
        busy[0] = False
        res = plane.request("phone", "img-1", "scottie_s1")
        assert res.ok and res.token

    def test_local_send_between_request_and_confirm_refuses_the_key(self) -> None:
        """The widest window: the operator presses Send after the request."""
        busy = [False]
        plane, keyed, _ = self._plane(busy)
        plane.take_lease("phone")
        token = plane.request("phone", "img-1", "scottie_s1").token
        assert token is not None
        busy[0] = True  # local operator presses Send
        res = plane.confirm("phone", token)
        assert not res.ok
        assert res.error == "busy"
        assert keyed == [], "keyed the rig on top of a local transmission"
        assert plane.status()["state"] == "idle"


class TestNonAsciiConfirmToken:
    """A non-ASCII confirm token must be rejected, not raise.

    Regression: secrets.compare_digest refuses to compare str containing
    non-ASCII and raises TypeError, which escaped confirm() and left the
    plane stuck in AWAITING_CONFIRM.
    """

    def test_non_ascii_token_is_bad_token_not_typeerror(self) -> None:
        plane = ControlPlane(
            now=lambda: 1000.0,
            transmit=lambda i, m: None,
            unkey=lambda r: None,
            enabled=lambda: True,
        )
        plane.take_lease("phone")
        real = plane.request("phone", "img-1", "scottie_s1").token
        assert real is not None
        res = plane.confirm("phone", "tokén-with-é")
        assert not res.ok
        assert res.error == "bad_token"
        # ...and the plane is still usable with the real token afterwards.
        assert plane.confirm("phone", real).ok


class TestCallbacksNotInvokedUnderLock:
    """Callbacks must run with the state lock released.

    Regression: unkey ran under the lock while doing blocking rig I/O, so
    every other caller — including reclaim_local, the first statement of
    closeEvent — blocked behind a PTT-off that can take seconds on a wedged
    link.  The observed symptom was the app hanging for over a minute at
    quit with the rig still keyed.
    """

    def test_status_is_callable_from_inside_the_unkey_callback(self) -> None:
        import threading

        plane_box: list[ControlPlane] = []
        observed: list[object] = []
        done = threading.Event()

        def slow_unkey(reason: str) -> None:
            # Stand-in for blocking rig I/O: another thread must be able to
            # make progress against the plane while we are in here.
            def other() -> None:
                observed.append(plane_box[0].status()["state"])
                done.set()

            t = threading.Thread(target=other)
            t.start()
            assert done.wait(2.0), "a second thread blocked on the plane's lock"
            t.join()

        plane = ControlPlane(
            now=lambda: 1000.0,
            transmit=lambda i, m: None,
            unkey=slow_unkey,
            enabled=lambda: True,
        )
        plane_box.append(plane)
        plane.take_lease("phone")
        token = plane.request("phone", "img-1", "scottie_s1").token
        assert token is not None
        plane.confirm("phone", token)
        plane.abort("phone")  # fires slow_unkey
        assert observed == ["idle"]
