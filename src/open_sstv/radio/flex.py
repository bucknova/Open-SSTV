# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct TCP control for FlexRadio 6000-series radios (SmartSDR API).

Talks the SmartSDR TCP/IP API on port 4992 straight to the radio, so a
Flex needs neither ``rigctld`` nor a virtual serial port to key PTT and
follow frequency.  One socket, one reader thread, no extra dependency.

**Wire protocol.**  Line-oriented ASCII.  On connect the radio greets us
with a version line and a client handle::

    V1.4.0.0
    H2A3B4C5D

Thereafter each side is prefixed by its message type:

- ``C<seq>|<command>``  — command from us,
- ``R<seq>|<hex code>|<message>`` — the reply to that command (``0`` is
  success; anything else is a Flex error code),
- ``S<handle>|<object> key=value …`` — pushed status, once subscribed,
- ``M<hex>|<text>`` — an unsolicited message from the radio.

**State is pushed, not polled.**  Unlike Hamlib, SmartSDR has no "read
the frequency" command — you ``sub`` to an object and the radio streams
updates.  So :meth:`get_freq` / :meth:`get_mode` / :meth:`get_ptt` return
the most recent value the reader thread cached, which makes them cheap
and non-blocking (the 1 Hz UI poll never touches the socket).  Writes
(:meth:`set_freq`, :meth:`set_mode`, :meth:`set_ptt`) are real
round-trips: we wait for the matching ``R`` line so a failed PTT is a
raised error, never a silent no-op.

**S-meter.**  Not implemented — Flex streams meter values as VITA-49
packets over a separate UDP socket, which is a lot of machinery for a
cosmetic readout.  :meth:`get_strength` returns 0; the UI treats an
unavailable S-meter as non-fatal.

**Slices.**  A Flex can run several receivers ("slices").  We follow one,
chosen by index (0 = slice A), configurable via ``flex_slice``.
"""
from __future__ import annotations

import logging
import socket
import threading

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError

_log = logging.getLogger(__name__)

#: SmartSDR's fixed TCP API port.
DEFAULT_FLEX_PORT = 4992

#: How long to wait for the radio's ``V``/``H`` greeting after connecting.
_GREETING_TIMEOUT_S: float = 5.0
#: How long a command waits for its matching ``R<seq>`` reply.
_RESPONSE_TIMEOUT_S: float = 5.0
#: How long :meth:`open` waits for the first status of the target slice.
#: Reaching this means the slice isn't active, which is worth an error at
#: connect time rather than a mystery 0.000 MHz later.
_SLICE_TIMEOUT_S: float = 5.0
#: Socket timeout for the reader thread. Idle reads time out and loop, so
#: this only bounds how quickly close() is noticed.
_SOCKET_TIMEOUT_S: float = 1.0

#: Flex mode tokens we accept for a slice.
FLEX_MODES = (
    "USB", "LSB", "CW", "AM", "SAM", "FM", "NFM", "DFM", "DIGU", "DIGL", "RTTY",
)

#: Aliases from other rig vocabularies (Hamlib/Kenwood style) → Flex.
#: SSTV runs in a sideband mode; the data variants map onto Flex's DIG*.
_MODE_ALIASES = {
    "PKTUSB": "DIGU", "PKTLSB": "DIGL",
    "DATA-U": "DIGU", "DATA-L": "DIGL",
    "USB-D": "DIGU", "LSB-D": "DIGL",
    "FMN": "NFM",
}


def normalize_flex_mode(mode: str) -> str:
    """Map a mode string from any of our vocabularies onto a Flex token.

    Unknown values are passed through upper-cased so a mode this build
    doesn't know about still reaches the radio, which is the authority.

    Examples
    --------
    >>> normalize_flex_mode("usb")
    'USB'
    >>> normalize_flex_mode("PKTUSB")
    'DIGU'
    """
    token = mode.strip().upper()
    return _MODE_ALIASES.get(token, token)


class FlexRig:
    """Direct SmartSDR TCP control of a FlexRadio 6000-series radio.

    Implements ``open_sstv.radio.base.Rig`` structurally (see the note in
    :mod:`open_sstv.radio.rigctld` on why we don't inherit the Protocol).
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_FLEX_PORT,
        slice_index: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._slice = slice_index

        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()          # guards socket writes + _seq
        self._state_lock = threading.Lock()    # guards the cached state below
        self._seq = 0

        #: seq → [Event, reply-tuple] for in-flight commands.
        self._pending: dict[int, list] = {}

        self._handle: str = ""
        self._version: str = ""
        self._freq_hz: int | None = None
        self._mode: str = ""
        self._passband_hz: int = 0
        self._ptt: bool = False
        self._slice_seen = threading.Event()

    @property
    def name(self) -> str:
        return f"FlexRadio@{self._host}:{self._port} (slice {self._slice})"

    # === lifecycle ===

    def open(self) -> None:
        """Connect, read the greeting, subscribe, and await slice state."""
        with self._lock:
            if self._sock is not None:
                return
            try:
                sock = socket.create_connection(
                    (self._host, self._port), timeout=_GREETING_TIMEOUT_S
                )
            except OSError as exc:
                raise RigConnectionError(
                    f"could not connect to {self.name}: {exc}"
                ) from exc
            sock.settimeout(_SOCKET_TIMEOUT_S)
            self._sock = sock
            self._stop.clear()
            self._slice_seen.clear()
            self._reader = threading.Thread(
                target=self._read_loop, name="sstv-flex-reader", daemon=True
            )
            self._reader.start()

        # Subscribe outside the write lock's connect section — _command
        # takes the lock itself.  Both are needed: slice for freq/mode,
        # transmit for the PTT state the health monitor reads back.
        try:
            self._command(f"sub slice {self._slice}")
            self._command("sub tx all")
        except RigCommandError:
            # Older firmware may not accept a per-slice subscription;
            # fall back to subscribing to everything.
            self._command("sub slice all")

        if not self._slice_seen.wait(_SLICE_TIMEOUT_S):
            self.close()
            raise RigConnectionError(
                f"{self.name}: radio never reported slice {self._slice} — "
                "is that slice active in SmartSDR?"
            )
        _log.info(
            "FlexRadio connected: version=%s handle=%s slice=%d",
            self._version or "?", self._handle or "?", self._slice,
        )

    def close(self) -> None:
        """Close the socket and stop the reader. Idempotent."""
        self._stop.set()
        with self._lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        # Fail any command still waiting so no caller blocks on a dead link.
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for entry in pending:
            entry[1] = (-1, "connection closed")
            entry[0].set()

    def __enter__(self) -> FlexRig:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # === public Rig surface ===

    def get_freq(self) -> int:
        self._require_alive()
        with self._state_lock:
            freq = self._freq_hz
        if freq is None:
            raise RigCommandError("no slice frequency yet", command="get_freq")
        return freq

    def set_freq(self, hz: int) -> None:
        # SmartSDR tunes in MHz; 6 dp is 1 Hz resolution.
        self._command(f"slice tune {self._slice} {hz / 1_000_000:.6f}")

    def get_mode(self) -> tuple[str, int]:
        self._require_alive()
        with self._state_lock:
            return (self._mode, self._passband_hz)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        self._command(
            f"slice set {self._slice} mode={normalize_flex_mode(mode)}"
        )
        if passband_hz > 0:
            # Flex expresses the filter as absolute edges around the carrier.
            self._command(
                f"filt {self._slice} 0 {passband_hz}"
            )

    def get_ptt(self) -> bool:
        # Also the liveness probe the TX health monitor leans on: it only
        # cares whether this raises, so _require_alive carries the weight.
        self._require_alive()
        with self._state_lock:
            return self._ptt

    def set_ptt(self, on: bool) -> None:
        self._command(f"xmit {1 if on else 0}")
        with self._state_lock:
            self._ptt = on

    def get_strength(self) -> int:
        """Not implemented — Flex streams meters over VITA-49/UDP.

        Returns 0 rather than raising: the UI treats an unavailable
        S-meter as non-fatal and this keeps the poll quiet.
        """
        return 0

    def ping(self) -> None:
        """Cheapest real round-trip that proves the radio is answering."""
        self._command("slice list")

    # === internals ===

    def _require_alive(self) -> None:
        if self._sock is None or self._stop.is_set():
            raise RigConnectionError(f"{self.name}: not connected")

    def _command(self, command: str, timeout_s: float = _RESPONSE_TIMEOUT_S) -> str:
        """Send one command and block until the radio's ``R`` reply lands."""
        event = threading.Event()
        entry: list = [event, None]
        with self._lock:
            if self._sock is None:
                raise RigConnectionError(f"{self.name}: not connected")
            self._seq += 1
            seq = self._seq
            with self._state_lock:
                self._pending[seq] = entry
            wire = f"C{seq}|{command}\n".encode("ascii")
            _log.debug("%s: >>> %s", self.name, wire.decode().rstrip())
            try:
                self._sock.sendall(wire)
            except OSError as exc:
                with self._state_lock:
                    self._pending.pop(seq, None)
                raise RigConnectionError(
                    f"{self.name}: send failed for {command!r}: {exc}"
                ) from exc

        if not event.wait(timeout_s):
            with self._state_lock:
                self._pending.pop(seq, None)
            raise RigConnectionError(
                f"{self.name}: timed out waiting for reply to {command!r}"
            )
        code, message = entry[1]
        if code != 0:
            raise RigCommandError(
                f"{command!r} failed: Flex error 0x{code:X} {message}".rstrip(),
                command=command,
                rprt=code,
            )
        return message

    def _read_loop(self) -> None:
        """Parse the radio's line stream until the socket closes."""
        buf = b""
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue          # idle; just re-check _stop
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    try:
                        self._handle_line(line)
                    except Exception:  # noqa: BLE001 — a bad line must not kill the reader
                        _log.debug("%s: unparseable line %r", self.name, line,
                                   exc_info=True)
        _log.debug("%s: reader loop exiting", self.name)

    def _handle_line(self, line: str) -> None:
        _log.debug("%s: <<< %s", self.name, line)
        kind, body = line[0], line[1:]
        if kind == "V":
            self._version = body
        elif kind == "H":
            self._handle = body
        elif kind == "R":
            self._handle_reply(body)
        elif kind == "S":
            self._handle_status(body)
        elif kind == "M":
            _log.debug("%s: radio message: %s", self.name, body)

    def _handle_reply(self, body: str) -> None:
        # "<seq>|<hex code>|<message>"
        parts = body.split("|", 2)
        if len(parts) < 2:
            return
        try:
            seq = int(parts[0])
            code = int(parts[1], 16)
        except ValueError:
            return
        message = parts[2] if len(parts) > 2 else ""
        with self._state_lock:
            entry = self._pending.pop(seq, None)
        if entry is not None:
            entry[1] = (code, message)
            entry[0].set()

    def _handle_status(self, body: str) -> None:
        # "<handle>|<object> key=value key=value …"
        _, _, payload = body.partition("|")
        tokens = payload.split()
        if not tokens:
            return
        fields = {}
        for token in tokens:
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value

        if tokens[0] == "slice" and len(tokens) > 1:
            # Only track the slice we were told to follow.
            try:
                index = int(tokens[1])
            except ValueError:
                return
            if index != self._slice:
                return
            self._apply_slice_fields(fields)
        elif tokens[0] in ("transmit", "interlock"):
            self._apply_tx_fields(fields)

    def _apply_slice_fields(self, fields: dict[str, str]) -> None:
        with self._state_lock:
            raw_freq = fields.get("RF_frequency")
            if raw_freq:
                try:
                    self._freq_hz = round(float(raw_freq) * 1_000_000)
                except ValueError:
                    pass
            mode = fields.get("mode")
            if mode:
                self._mode = mode.upper()
            lo, hi = fields.get("filter_lo"), fields.get("filter_hi")
            if lo and hi:
                try:
                    self._passband_hz = max(0, int(float(hi)) - int(float(lo)))
                except ValueError:
                    pass
            have_freq = self._freq_hz is not None
        if have_freq:
            self._slice_seen.set()

    def _apply_tx_fields(self, fields: dict[str, str]) -> None:
        # Flex reports transmit state in a couple of shapes depending on
        # firmware/object; accept whichever arrives.
        state = fields.get("state", "").upper()
        with self._state_lock:
            if state:
                self._ptt = state == "TRANSMITTING"
            elif "xmit" in fields:
                self._ptt = fields["xmit"] in ("1", "true", "True")


__all__ = ["DEFAULT_FLEX_PORT", "FLEX_MODES", "FlexRig", "normalize_flex_mode"]
