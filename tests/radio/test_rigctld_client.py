# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.radio.rigctld``.

These run against ``tests.radio.fake_rigctld.FakeRigctld``, an in-process
asyncio TCP server that speaks just enough of the rigctld extended
protocol to drive every method on ``RigctldClient``. They do **not**
require hamlib to be installed — that's the whole point of the fake.

If you have hamlib locally and want to point these at a real ``rigctld -m 1``
instead, set ``SSTVAPP_TEST_RIGCTLD_PORT=4532`` in your environment and the
``real_rigctld`` fixture will skip the fake. (Not yet implemented; tracked
for the integration job in CI when hamlib is on the runner.)
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError
from open_sstv.radio.rigctld import RigctldClient, is_safe_rigctld_arg
from tests.radio.fake_rigctld import FakeRigctld


@pytest.fixture
def fake() -> Iterator[FakeRigctld]:
    f = FakeRigctld()
    f.start()
    try:
        yield f
    finally:
        f.stop()


@pytest.fixture
def client(fake: FakeRigctld) -> Iterator[RigctldClient]:
    c = RigctldClient(host="127.0.0.1", port=fake.port, timeout_s=2.0)
    try:
        yield c
    finally:
        c.close()


# === lifecycle ===


def test_open_is_idempotent(client: RigctldClient) -> None:
    client.open()
    client.open()  # second open is a no-op, must not raise
    client.close()


def test_close_is_idempotent_when_not_open(client: RigctldClient) -> None:
    client.close()
    client.close()


def test_context_manager(fake: FakeRigctld) -> None:
    with RigctldClient(host="127.0.0.1", port=fake.port) as c:
        assert c.get_freq() == fake.freq


def test_lazy_connect(fake: FakeRigctld) -> None:
    """Construction must succeed even before any I/O."""
    c = RigctldClient(host="127.0.0.1", port=fake.port)
    # Hasn't touched the network yet — just constructing.
    assert c.name == f"rigctld@127.0.0.1:{fake.port}"
    c.close()


# === frequency ===


def test_get_freq(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.freq = 14_070_000
    assert client.get_freq() == 14_070_000


def test_set_freq(client: RigctldClient, fake: FakeRigctld) -> None:
    client.set_freq(14_250_000)
    assert fake.freq == 14_250_000
    # Round-trips back through get_freq.
    assert client.get_freq() == 14_250_000


def test_set_freq_uses_extended_command(
    client: RigctldClient, fake: FakeRigctld
) -> None:
    client.set_freq(7_074_000)
    assert "F 7074000" in fake.commands_received


# === mode ===


def test_get_mode(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.mode_name = "LSB"
    fake.passband_hz = 1800
    assert client.get_mode() == ("LSB", 1800)


def test_set_mode(client: RigctldClient, fake: FakeRigctld) -> None:
    client.set_mode("USB", 2400)
    assert fake.mode_name == "USB"
    assert fake.passband_hz == 2400


# === PTT ===


def test_get_ptt_unkeyed(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.ptt = False
    assert client.get_ptt() is False


def test_get_ptt_keyed(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.ptt = True
    assert client.get_ptt() is True


def test_set_ptt_keys_and_unkeys(
    client: RigctldClient, fake: FakeRigctld
) -> None:
    client.set_ptt(True)
    assert fake.ptt is True
    client.set_ptt(False)
    assert fake.ptt is False


# === strength ===


def test_get_strength(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.strength_db = -42
    assert client.get_strength() == -42


# === ping ===


def test_ping(client: RigctldClient, fake: FakeRigctld) -> None:
    client.ping()
    # Cheapest possible round-trip — just needs to not raise.
    assert "f" in fake.commands_received


# === error paths ===


def test_connection_refused() -> None:
    """Pointing at a port nobody is listening on raises RigConnectionError."""
    # Port 1 is reserved + privileged, basically guaranteed to be unused.
    c = RigctldClient(host="127.0.0.1", port=1, timeout_s=0.5)
    with pytest.raises(RigConnectionError):
        c.get_freq()


def test_command_error_raised_on_rprt_nonzero(
    client: RigctldClient, fake: FakeRigctld
) -> None:
    fake.fail_next_command = True
    with pytest.raises(RigCommandError) as excinfo:
        client.get_freq()
    assert excinfo.value.rprt == -1
    assert excinfo.value.command == "f"


def test_recovers_from_first_failure(
    client: RigctldClient, fake: FakeRigctld
) -> None:
    """fail_next_command auto-resets, so the next command succeeds."""
    fake.fail_next_command = True
    with pytest.raises(RigCommandError):
        client.set_freq(14_000_000)
    # Same client, same socket — second command should go through fine.
    client.set_freq(14_000_000)
    assert fake.freq == 14_000_000


def test_serializes_calls_under_threading(
    client: RigctldClient, fake: FakeRigctld
) -> None:
    """The internal lock should keep two threads from interleaving bytes
    on the socket. We hammer the client from N threads and assert every
    one of them reads back a sane (parseable) frequency value."""
    import threading

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(20):
                client.set_freq(14_070_000)
                assert client.get_freq() == 14_070_000
        except BaseException as exc:  # noqa: BLE001 — test wants every error
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


# ---------------------------------------------------------------------------
# OP-13 — is_safe_rigctld_arg
# ---------------------------------------------------------------------------


class TestIsSafeRigctldArg:
    """Regression tests for the leading-dash validator (OP-13).

    Values that start with ``-`` must be rejected so a hand-edited
    config can't smuggle an arbitrary rigctld flag into the argv the
    launcher constructs.  Empty / None is explicitly safe (callers
    skip adding the corresponding ``-r`` pair when the port is blank).
    """

    def test_accepts_real_device_paths(self) -> None:
        assert is_safe_rigctld_arg("/dev/cu.usbserial-1410") is True
        assert is_safe_rigctld_arg("/dev/ttyUSB0") is True
        assert is_safe_rigctld_arg("COM3") is True

    def test_accepts_empty_and_none(self) -> None:
        assert is_safe_rigctld_arg("") is True
        assert is_safe_rigctld_arg(None) is True

    def test_rejects_leading_dash(self) -> None:
        assert is_safe_rigctld_arg("-rf") is False
        assert is_safe_rigctld_arg("--help") is False
        assert is_safe_rigctld_arg("-") is False

    def test_rejects_whitespace_then_dash(self) -> None:
        """Whitespace-padded flag-like values are still rejected."""
        assert is_safe_rigctld_arg("  --help") is False
        assert is_safe_rigctld_arg("\t-r") is False

    def test_dash_mid_value_is_safe(self) -> None:
        """Dashes inside a value (e.g. device path) are fine."""
        assert is_safe_rigctld_arg("/dev/cu.usbserial-1410") is True
        assert is_safe_rigctld_arg("mid-dash-value") is True
