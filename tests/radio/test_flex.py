# SPDX-License-Identifier: GPL-3.0-or-later
"""FlexRadio direct backend, driven against a fake SmartSDR TCP radio.

No hardware: a small in-process server speaks the documented line
protocol (``V``/``H`` greeting, ``C<seq>|`` commands, ``R<seq>|`` replies,
``S<handle>|`` pushed status) so the client's parsing, command
round-trips, and error mapping are all exercised for real over a socket.
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError
from open_sstv.radio.flex import FlexRig, normalize_flex_mode

HANDLE = "2A3B4C5D"


class FakeFlex:
    """Minimal SmartSDR-speaking radio."""

    def __init__(
        self,
        *,
        slice_index: int = 0,
        greet: bool = True,
        push_slice: bool = True,
        fail_commands: set[str] | None = None,
    ) -> None:
        self._slice = slice_index
        self._greet = greet
        self._push_slice = push_slice
        self._fail = fail_commands or set()
        self.commands: list[str] = []
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self._conn = conn
        try:
            if self._greet:
                conn.sendall(b"V1.4.0.0\n")
                conn.sendall(f"H{HANDLE}\n".encode())
            if self._push_slice:
                conn.sendall(
                    f"S{HANDLE}|slice {self._slice} RF_frequency=14.074000 "
                    f"mode=USB filter_lo=100 filter_hi=2900\n".encode()
                )
            buf = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._handle(conn, raw.decode().strip())
        except OSError:
            return

    def _handle(self, conn: socket.socket, line: str) -> None:
        if not line.startswith("C"):
            return
        seq, _, command = line[1:].partition("|")
        self.commands.append(command)
        code = 0x50000015 if command.split()[0] in self._fail else 0
        conn.sendall(f"R{seq}|{code:X}|\n".encode())
        # Reflect a tune back as pushed status, like a real radio does.
        if command.startswith("slice tune"):
            mhz = command.split()[-1]
            conn.sendall(
                f"S{HANDLE}|slice {self._slice} RF_frequency={mhz}\n".encode()
            )
        elif command.startswith("xmit "):
            state = "TRANSMITTING" if command.endswith("1") else "READY"
            conn.sendall(f"S{HANDLE}|interlock state={state}\n".encode())

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass


@pytest.fixture
def radio() -> Iterator[FakeFlex]:
    fake = FakeFlex()
    yield fake
    fake.close()


@pytest.fixture
def rig(radio: FakeFlex) -> Iterator[FlexRig]:
    r = FlexRig(host="127.0.0.1", port=radio.port, slice_index=0)
    r.open()
    yield r
    r.close()


class TestNormalizeMode:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [("usb", "USB"), ("USB", "USB"), ("PKTUSB", "DIGU"),
         ("pktlsb", "DIGL"), ("DIGU", "DIGU"), ("weird", "WEIRD")],
    )
    def test_maps_vocabularies_onto_flex(self, given: str, expected: str) -> None:
        assert normalize_flex_mode(given) == expected


class TestConnect:
    def test_open_subscribes_and_reads_state(
        self, rig: FlexRig, radio: FakeFlex
    ) -> None:
        assert any(c.startswith("sub slice") for c in radio.commands)
        assert rig.get_freq() == 14_074_000
        mode, passband = rig.get_mode()
        assert mode == "USB"
        assert passband == 2800  # filter_hi - filter_lo

    def test_open_fails_when_slice_never_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A radio that greets but never pushes our slice → clear error at
        # connect time rather than a mystery 0.000 MHz later.
        monkeypatch.setattr("open_sstv.radio.flex._SLICE_TIMEOUT_S", 0.3)
        fake = FakeFlex(push_slice=False)
        try:
            r = FlexRig(host="127.0.0.1", port=fake.port, slice_index=0)
            with pytest.raises(RigConnectionError, match="never reported slice"):
                r.open()
        finally:
            fake.close()

    def test_connect_refused_is_rig_error(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listening now
        r = FlexRig(host="127.0.0.1", port=port)
        with pytest.raises(RigConnectionError):
            r.open()

    def test_ping_round_trips(self, rig: FlexRig, radio: FakeFlex) -> None:
        rig.ping()
        assert "slice list" in radio.commands


class TestControl:
    def test_set_ptt_sends_xmit_and_tracks_state(
        self, rig: FlexRig, radio: FakeFlex
    ) -> None:
        rig.set_ptt(True)
        assert "xmit 1" in radio.commands
        assert rig.get_ptt() is True
        rig.set_ptt(False)
        assert "xmit 0" in radio.commands
        assert rig.get_ptt() is False

    def test_set_freq_tunes_in_mhz(self, rig: FlexRig, radio: FakeFlex) -> None:
        rig.set_freq(14_230_000)
        assert "slice tune 0 14.230000" in radio.commands

    def test_set_mode_normalizes(self, rig: FlexRig, radio: FakeFlex) -> None:
        rig.set_mode("PKTUSB", 0)
        assert "slice set 0 mode=DIGU" in radio.commands

    def test_command_error_is_rig_error(self) -> None:
        fake = FakeFlex(fail_commands={"xmit"})
        try:
            r = FlexRig(host="127.0.0.1", port=fake.port)
            r.open()
            with pytest.raises(RigCommandError, match="Flex error"):
                r.set_ptt(True)
            r.close()
        finally:
            fake.close()

    def test_strength_is_zero_not_an_error(self, rig: FlexRig) -> None:
        # Meters ride VITA-49/UDP; we report 0 and the UI tolerates it.
        assert rig.get_strength() == 0


class TestLifecycle:
    def test_calls_after_close_raise_rather_than_hang(
        self, radio: FakeFlex
    ) -> None:
        r = FlexRig(host="127.0.0.1", port=radio.port)
        r.open()
        r.close()
        with pytest.raises(RigConnectionError):
            r.get_freq()
        with pytest.raises(RigConnectionError):
            r.set_ptt(False)

    def test_close_is_idempotent(self, radio: FakeFlex) -> None:
        r = FlexRig(host="127.0.0.1", port=radio.port)
        r.open()
        r.close()
        r.close()  # must not raise
