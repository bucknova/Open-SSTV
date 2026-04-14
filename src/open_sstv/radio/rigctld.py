# SPDX-License-Identifier: GPL-3.0-or-later
"""TCP client for Hamlib's ``rigctld`` daemon.

We talk to ``rigctld`` (the network rig-control daemon shipped with hamlib)
over a plain TCP socket on port 4532. The wire protocol is one command per
line, ``\\n``-terminated.

**Why the daemon and not the SWIG bindings.** Hamlib's Python bindings
require building Hamlib from source with ``--with-python-binding``, which
breaks portability across distros and macOS. ``rigctld`` is shipped as
a daemon by every hamlib package on every supported platform and the
on-the-wire protocol has been stable for over a decade. The cost is one
process boundary; the benefit is reaching hundreds of supported radios
with a 200-line client.

**Why ``+`` extended responses.** rigctld's default protocol has no
consistent per-command terminator: set commands return ``RPRT N`` but
get commands return raw values, so a client has to know exactly how many
lines each command emits to read the right number of bytes. Prefixing
every command with ``+`` switches the daemon into the extended response
mode, where every response — set or get — ends with a single ``RPRT N``
line. We just read until we see that line and we never have to special-case
command shapes.

**Threading.** A single ``threading.Lock`` serializes ``_send_recv``, so
the UI poll thread and the TX worker can never interleave bytes on the
same socket. The lock is held for the whole send-then-receive sequence,
not just the send, because the response is what closes the transaction.

**Reconnection.** Lazy connect on the first command (so construction can't
fail just because the daemon isn't up yet) and one automatic reconnect on
``BrokenPipeError`` / ``ConnectionResetError`` per command. If the second
attempt also fails, raise ``RigConnectionError`` and let the UI surface
it as a non-modal status bar message.
"""
from __future__ import annotations

import socket
import threading

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError


class RigctldClient:
    """Synchronous client for hamlib's ``rigctld`` daemon.

    Implements ``open_sstv.radio.base.Rig`` structurally; we don't inherit
    from the Protocol because Protocols are for type-checking, not
    runtime base classes.
    """

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 4532  # rigctld default
    DEFAULT_TIMEOUT_S = 2.0

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None

    @property
    def name(self) -> str:
        return f"rigctld@{self._host}:{self._port}"

    # === lifecycle ===

    def open(self) -> None:
        """Connect to the daemon. Idempotent and safe to call repeatedly."""
        with self._lock:
            self._connect_locked()

    def close(self) -> None:
        """Close the socket. Idempotent and safe to call when not open."""
        with self._lock:
            self._close_locked()

    def __enter__(self) -> "RigctldClient":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # === public Rig surface ===

    def get_freq(self) -> int:
        # +f → "Frequency: 14070000\nRPRT 0"
        body = self._send_recv("f")
        return int(_parse_value(body[0]))

    def set_freq(self, hz: int) -> None:
        self._send_recv(f"F {hz}")

    def get_mode(self) -> tuple[str, int]:
        # +m → "Mode: USB\nPassband: 2400\nRPRT 0"
        body = self._send_recv("m")
        if len(body) < 2:
            raise RigCommandError(
                f"unexpected mode response: {body!r}", command="m"
            )
        return (_parse_value(body[0]), int(_parse_value(body[1])))

    def set_mode(self, mode: str, passband_hz: int) -> None:
        self._send_recv(f"M {mode} {passband_hz}")

    def get_ptt(self) -> bool:
        # +t → "PTT: 0\nRPRT 0"
        body = self._send_recv("t")
        return _parse_value(body[0]) != "0"

    def set_ptt(self, on: bool) -> None:
        self._send_recv(f"T {1 if on else 0}")

    def get_strength(self) -> int:
        # +l STRENGTH → "STRENGTH: -73\nRPRT 0"
        body = self._send_recv("l STRENGTH")
        return int(_parse_value(body[0]))

    def ping(self) -> None:
        """Cheapest round-trip we can do — verifies the daemon is alive."""
        self.get_freq()

    # === connection internals ===

    def _connect_locked(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout_s
            )
        except OSError as exc:
            raise RigConnectionError(
                f"could not connect to {self.name}: {exc}"
            ) from exc
        sock.settimeout(self._timeout_s)
        self._sock = sock

    def _close_locked(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.close()
        finally:
            self._sock = None

    def _send_recv(self, command: str) -> list[str]:
        """Send a command and return the response body (lines before RPRT).

        Holds the lock for the whole transaction so concurrent callers can
        never interleave bytes. On a broken socket we close, reconnect, and
        retry once before giving up.
        """
        with self._lock:
            try:
                self._connect_locked()
                return self._send_recv_locked(command)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Half-open or peer-closed socket: try once more from scratch.
                self._close_locked()
                try:
                    self._connect_locked()
                    return self._send_recv_locked(command)
                except OSError as exc:
                    raise RigConnectionError(
                        f"{self.name}: lost connection during {command!r}: {exc}"
                    ) from exc
            except TimeoutError as exc:
                raise RigConnectionError(
                    f"{self.name}: timed out waiting for response to {command!r}"
                ) from exc
            except OSError as exc:
                raise RigConnectionError(
                    f"{self.name}: socket error on {command!r}: {exc}"
                ) from exc

    def _send_recv_locked(self, command: str) -> list[str]:
        """Inner half — assumes the lock is held and the socket is open."""
        if self._sock is None:
            raise RigConnectionError(f"{self.name}: socket is not open")
        # ``+`` activates rigctld's extended response mode for this command,
        # so the response is always terminated by an ``RPRT N`` line.
        wire = f"+{command}\n".encode("ascii")
        self._sock.sendall(wire)

        lines = self._read_until_rprt()
        if not lines or not lines[-1].startswith("RPRT "):
            raise RigCommandError(
                f"malformed response to {command!r}: {lines!r}",
                command=command,
            )
        try:
            rprt = int(lines[-1].split()[1])
        except (IndexError, ValueError) as exc:
            raise RigCommandError(
                f"could not parse RPRT line {lines[-1]!r}",
                command=command,
            ) from exc
        if rprt != 0:
            raise RigCommandError(
                f"{command!r} returned RPRT {rprt}",
                command=command,
                rprt=rprt,
            )
        return lines[:-1]  # drop the RPRT terminator

    #: Maximum number of lines accepted in a single rigctld response.
    #: Guards against unbounded buffer growth if the daemon sends garbage
    #: without an RPRT terminator before the socket timeout.
    _MAX_RESPONSE_LINES: int = 1000

    def _read_until_rprt(self) -> list[str]:
        """Read from the socket until we see a complete ``RPRT N`` line."""
        if self._sock is None:
            raise RigConnectionError(f"{self.name}: socket is not open")
        buf = bytearray()
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise BrokenPipeError("rigctld closed the connection")
            buf.extend(chunk)
            # Guard against unbounded growth from a misbehaving daemon.
            line_count = buf.count(b"\n")
            if line_count > self._MAX_RESPONSE_LINES:
                raise RigCommandError(
                    f"rigctld response exceeded {self._MAX_RESPONSE_LINES} lines "
                    "— possible runaway daemon",
                    command="<unknown>",
                )
            # We have a complete response when there is an ``RPRT `` token
            # followed by a newline somewhere after it.
            idx = buf.rfind(b"RPRT ")
            if idx != -1 and buf.find(b"\n", idx) != -1:
                break
        text = buf.decode("ascii", errors="replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # drop trailing empty from final \n
        return lines


def _parse_value(line: str) -> str:
    """Extract the value from an extended-response line.

    Extended responses look like ``"Frequency: 14070000"``; older mixed
    daemons sometimes emit just the value with no label. We tolerate both.
    """
    if ":" in line:
        return line.split(":", 1)[1].strip()
    return line.strip()


__all__ = ["RigctldClient"]
