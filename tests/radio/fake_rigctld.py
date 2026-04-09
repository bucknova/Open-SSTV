# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny in-process stand-in for hamlib's ``rigctld``.

CI doesn't have hamlib installed, so the rigctld client tests can't talk
to a real daemon. ``FakeRigctld`` runs an asyncio TCP server on a random
local port that speaks just enough of rigctld's extended response
protocol (the ``+`` prefix variant) to drive every method on
``RigctldClient``.

The server runs on a daemon thread so the test process can shut down
even if a test forgets to call ``stop()`` (it shouldn't, but the safety
net is cheap).

State and call recording are exposed as plain attributes so tests can
set up scenarios:

    with FakeRigctld() as fake:
        fake.freq = 14_070_000
        fake.fail_next_command = True   # next request gets RPRT -1
        client = RigctldClient("127.0.0.1", fake.port)
        ...
        assert "F 14250000" in fake.commands_received

Bound only to ``127.0.0.1`` so the loopback firewall on macOS / Linux
hosts doesn't surface a permissions prompt.
"""
from __future__ import annotations

import asyncio
import threading
from types import TracebackType


class FakeRigctld:
    """Minimal asyncio rigctld emulator for tests."""

    def __init__(self) -> None:
        # Mutable state — tests poke at these between requests.
        self.freq: int = 14_070_000
        self.mode_name: str = "USB"
        self.passband_hz: int = 2400
        self.ptt: bool = False
        self.strength_db: int = -73
        # Test hooks.
        self.commands_received: list[str] = []
        #: When True, the next command receives ``RPRT -1`` instead of being
        #: dispatched. Auto-resets after one use so tests don't have to.
        self.fail_next_command: bool = False
        #: When True, every command (including the current one) gets
        #: ``RPRT -1``. Useful for "rig in fault state" scenarios.
        self.fail_all_commands: bool = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._port: int = 0

    @property
    def port(self) -> int:
        return self._port

    # === lifecycle ===

    def start(self) -> None:
        """Start the server thread and block until it's listening."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("FakeRigctld failed to start within 2 s")

    def stop(self) -> None:
        """Signal the server to shut down and join its thread."""
        if self._loop is not None and self._stop_event is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "FakeRigctld":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # === server thread ===

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()
            self._loop = None

    async def _serve(self) -> None:
        assert self._stop_event is not None
        server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        self._server = server
        sock = server.sockets[0]
        self._port = sock.getsockname()[1]
        self._ready.set()
        async with server:
            await self._stop_event.wait()
            server.close()
            await server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                command_text = line.decode("ascii", errors="replace").rstrip("\r\n")
                response = self._dispatch(command_text)
                writer.write(response.encode("ascii"))
                try:
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return
        except (ConnectionResetError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    # === protocol ===

    def _dispatch(self, command_text: str) -> str:
        """Translate one wire command into one wire response.

        Strips the ``+`` prefix the real client always sends. Records the
        un-prefixed command on ``commands_received`` so tests can assert
        about what the client actually wrote.
        """
        cmd = command_text[1:] if command_text.startswith("+") else command_text
        self.commands_received.append(cmd)

        if self.fail_all_commands:
            return "RPRT -1\n"
        if self.fail_next_command:
            self.fail_next_command = False
            return "RPRT -1\n"

        parts = cmd.split()
        if not parts:
            return "RPRT -1\n"

        op = parts[0]
        try:
            if op == "f":
                return f"Frequency: {self.freq}\nRPRT 0\n"
            if op == "F":
                self.freq = int(parts[1])
                return "RPRT 0\n"
            if op == "m":
                return (
                    f"Mode: {self.mode_name}\nPassband: {self.passband_hz}\nRPRT 0\n"
                )
            if op == "M":
                self.mode_name = parts[1]
                self.passband_hz = int(parts[2])
                return "RPRT 0\n"
            if op == "t":
                return f"PTT: {1 if self.ptt else 0}\nRPRT 0\n"
            if op == "T":
                self.ptt = parts[1] == "1"
                return "RPRT 0\n"
            if op == "l" and len(parts) >= 2 and parts[1] == "STRENGTH":
                return f"STRENGTH: {self.strength_db}\nRPRT 0\n"
        except (IndexError, ValueError):
            return "RPRT -1\n"

        return "RPRT -1\n"


__all__ = ["FakeRigctld"]
