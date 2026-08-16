# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct serial CAT/PTT rig control — no external daemons required.

Implements the ``Rig`` protocol using ``pyserial`` to talk to radios
directly over their serial (USB-serial) port. Supports three families:

* **PTT-only** (``SerialPttRig``) — keys PTT via DTR or RTS line.
  Works with virtually any radio that has a serial PTT interface
  (Signalink, homebrew interfaces, many rigs' ACC/DATA ports).

* **Icom CI-V** (``IcomCIVRig``) — full CAT for Icom radios.
  Covers IC-7300, IC-705, IC-7100, IC-9700, IC-7200, IC-7600, etc.

* **Kenwood** (``KenwoodRig``) — text-based protocol used by
  Kenwood (TS-590, TS-890, TS-2000, TS-480) and Elecraft (K3, KX3, K4).

* **Yaesu** (``YaesuRig``) — Yaesu CAT protocol used by FT-991A,
  FT-891, FT-710, FTDX10, FTDX101, FT-950, FT-450/450D.

All classes are drop-in replacements for ``RigctldClient`` — they
implement the same ``Rig`` protocol and can be swapped in the
MainWindow without touching any other code.

Usage
-----

    rig = IcomCIVRig("/dev/cu.usbserial-1410", baud_rate=19200, ci_v_address=0x94)
    rig.open()
    rig.set_ptt(True)
    ...
    rig.set_ptt(False)
    rig.close()
"""
from __future__ import annotations

import logging
import threading
import time

import serial

_log = logging.getLogger(__name__)

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError

# termios.error is raised by reset_input_buffer() / tcflush() on a USB unplug.
# It does NOT inherit from OSError (its MRO is termios.error → Exception) so a
# bare `except OSError` misses it.  Include it explicitly where present.
try:
    import termios as _termios

    _SERIAL_IO_ERRORS: tuple[type[Exception], ...] = (
        serial.SerialException, OSError, _termios.error
    )
except ImportError:
    # Windows: no termios module — serial.SerialException (itself an OSError) suffices.
    _SERIAL_IO_ERRORS = (serial.SerialException, OSError)


def _close_serial_port(ser: serial.Serial) -> None:
    """Close *ser*, force-releasing the OS handle if ``close()`` raises.

    M8 (v0.3 audit): on a physically unplugged USB adapter,
    ``Serial.close()`` can raise (termios tcsetattr/flush runs before
    the actual ``os.close``) and the file descriptor leaks — on
    Linux/macOS the replugged port then re-opens as "Device or resource
    busy" until the process exits.  Closing the raw fd directly is safe
    after a failed close: worst case it's already gone and ``os.close``
    raises ``OSError``, which we ignore.
    """
    try:
        ser.close()
    except _SERIAL_IO_ERRORS as exc:
        _log.debug("Serial close raised (%s) — force-releasing fd", exc)
        fd = getattr(ser, "fd", None)  # POSIX pyserial; None on Windows
        if fd is not None:
            import os  # noqa: PLC0415

            try:
                os.close(fd)
            except OSError:
                pass  # already released


# ============================================================
# PTT-only via serial control lines
# ============================================================


class SerialPttRig:
    """PTT via DTR or RTS on a serial port.

    The simplest possible rig interface: toggling a serial control line
    is all many operators need for SSTV (VOX handles the rest, or the
    radio has its own PTT-sense input).
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
        ptt_line: str = "DTR",  # "DTR" or "RTS"
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._ptt_line = ptt_line.upper()
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        """Friendly per-instance label (L-3).

        Previously a class attribute, so two ``SerialPttRig`` instances
        bound to different ports rendered as the same UI label and any
        error message lost the port context.  Including the port string
        here (e.g. ``"Serial PTT (/dev/cu.usbserial-1410)"`` or
        ``"Serial PTT (COM3)"``) makes multi-rig log lines and toasts
        unambiguous without any caller changes.
        """
        return f"Serial PTT ({self._port})"

    def open(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                self._ser = serial.Serial(
                    self._port,
                    self._baud_rate,
                    timeout=1.0,
                    write_timeout=1.0,
                )
                # Ensure PTT is off on open
                self._set_ptt_line(False)
            except _SERIAL_IO_ERRORS as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._set_ptt_line(False)
                except _SERIAL_IO_ERRORS:
                    pass  # link already dead; unkey is best-effort here
                _close_serial_port(self._ser)
                self._ser = None

    def get_freq(self) -> int:
        return 0

    def set_freq(self, hz: int) -> None:
        pass

    def get_mode(self) -> tuple[str, int]:
        return ("", 0)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        pass

    def get_ptt(self) -> bool:
        with self._lock:
            if self._ser is None:
                return False
            try:
                if self._ptt_line == "RTS":
                    return self._ser.rts
                return self._ser.dtr
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Serial PTT read failed on {self._port}: {exc}"
                ) from exc

    def set_ptt(self, on: bool) -> None:
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            try:
                self._set_ptt_line(on)
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Serial PTT write failed on {self._port}: {exc}"
                ) from exc

    def get_strength(self) -> int:
        return 0

    def ping(self) -> None:
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")

    def _set_ptt_line(self, on: bool) -> None:
        if self._ser is None:
            return
        if self._ptt_line == "RTS":
            self._ser.rts = on
        else:
            self._ser.dtr = on


# ============================================================
# Icom CI-V protocol
# ============================================================

#: H12: shorter deadline for read-only diagnostic commands (get_freq,
#: get_mode, get_strength, get_ptt).  The serial lock is held for the
#: entire round-trip; a stale response holding it for the full 1.0 s
#: would delay PTT-off writes from the watchdog or Stop button by up
#: to that long, making "stop doesn't unkey immediately" a real
#: symptom.  200 ms is enough headroom for any healthy CI-V exchange
#: and means the lock turns over fast enough for set commands to
#: pre-empt.  Set commands keep the 1.0 s default — they're rare and
#: the user wants them to succeed rather than time out.
_DIAG_DEADLINE_S: float = 0.2

# CI-V frame: FE FE <to> <from> <cmd> [<subcmd>] [<data>...] FD
_CIV_PREAMBLE = b"\xfe\xfe"
_CIV_EOM = b"\xfd"
_CIV_CONTROLLER = 0xE0  # default controller address
_CIV_OK = 0xFB
_CIV_NG = 0xFA

# Common CI-V addresses for popular Icom radios
ICOM_ADDRESSES: dict[str, int] = {
    "IC-7300": 0x94,
    "IC-7610": 0x98,
    "IC-9700": 0xA2,
    "IC-705": 0xA4,
    "IC-7100": 0x88,
    "IC-7200": 0x76,
    "IC-7600": 0x7A,
    "IC-7000": 0x70,
    "IC-7851": 0x8E,
    "IC-R8600": 0x96,
}


class IcomCIVRig:
    """Direct CAT control for Icom radios via CI-V protocol."""

    def __init__(
        self,
        port: str,
        baud_rate: int = 19200,
        ci_v_address: int = 0x94,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._addr = ci_v_address
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        """Per-instance label including port (L-3)."""
        return f"Icom CI-V ({self._port})"

    def open(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                self._ser = serial.Serial(
                    self._port,
                    self._baud_rate,
                    timeout=0.5,
                    write_timeout=1.0,
                )
                self._ser.reset_input_buffer()
            except _SERIAL_IO_ERRORS as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                _close_serial_port(self._ser)
                self._ser = None

    def get_freq(self) -> int:
        """Read the current VFO frequency."""
        # H12: diagnostic read — short deadline so a stale response can't
        # hold the serial lock long enough to delay a PTT-off write.
        resp = self._command(b"\x03", deadline_s=_DIAG_DEADLINE_S)
        # Response payload: [cmd_echo(0x03), b0, b1, b2, b3, b4] — 6 bytes.
        # Strip the command echo before handing to _bcd_to_freq.
        if len(resp) < 6:
            return 0
        return self._bcd_to_freq(resp[1:])

    def set_freq(self, hz: int) -> None:
        data = self._freq_to_bcd(hz)
        self._command(b"\x05" + data)

    def get_mode(self) -> tuple[str, int]:
        resp = self._command(b"\x04", deadline_s=_DIAG_DEADLINE_S)
        # Response payload: [cmd_echo(0x04), mode_byte, passband_byte].
        # resp[0] is the command echo (happens to equal 0x04 = RTTY in the
        # mode_map), so without stripping it the mode always reads as RTTY.
        if len(resp) < 2:
            return ("", 0)
        mode_map = {
            0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW",
            0x04: "RTTY", 0x05: "FM", 0x07: "CW-R", 0x08: "RTTY-R",
            0x17: "DV",
        }
        mode_name = mode_map.get(resp[1], f"0x{resp[1]:02X}")
        passband = 0
        if len(resp) >= 3:
            passband = resp[2] * 100  # rough approximation
        return (mode_name, passband)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        mode_map = {
            "LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03,
            "RTTY": 0x04, "FM": 0x05, "CW-R": 0x07, "RTTY-R": 0x08,
        }
        mode_byte = mode_map.get(mode.upper(), 0x01)
        self._command(bytes([0x06, mode_byte]))

    def get_ptt(self) -> bool:
        # CI-V command 0x1C 0x00 — read TX state
        # Response: [cmd_echo(0x1C), subcmd(0x00), tx_state].
        # resp[0]=0x1C is always non-zero, so without stripping the echo
        # get_ptt() would always return True (rig appears permanently keyed).
        resp = self._command(b"\x1c\x00", deadline_s=_DIAG_DEADLINE_S)
        if len(resp) >= 3:
            return resp[2] != 0x00
        return False

    def set_ptt(self, on: bool) -> None:
        # CI-V command 0x1C 0x00 <01=TX, 00=RX>
        self._command(b"\x1c\x00" + (b"\x01" if on else b"\x00"))

    def get_strength(self) -> int:
        # CI-V command 0x15 0x02 — read S-meter
        # Response: [cmd_echo(0x15), subcmd(0x02), hi_bcd, lo_bcd].
        # Without stripping the echo, raw was always 0x1502=5378 (C-4).
        # The payload bytes are BCD, not binary: S9 is sent as 0x01 0x20
        # (= decimal 120), not 0x00 0x78 (= binary 120).
        resp = self._command(b"\x15\x02", deadline_s=_DIAG_DEADLINE_S)
        _log.info("S-meter: resp=%s (%d bytes)", resp.hex() if resp else "(empty)", len(resp))
        if len(resp) >= 4:
            raw = self._bcd_byte_to_int(resp[2]) * 100 + self._bcd_byte_to_int(resp[3])
            _log.info("S-meter: hi_bcd=0x%02x lo_bcd=0x%02x raw=%d", resp[2], resp[3], raw)
            # Icom S-meter: 0=S0, 120=S9, 241=S9+60 (decimal values)
            if raw <= 120:
                return -73 - (9 - raw * 9 // 120) * 6
            return -73 + (raw - 120) * 60 // 121
        return 0

    def ping(self) -> None:
        self.get_freq()

    # === CI-V internals ===

    def _command(self, cmd_data: bytes, deadline_s: float = 1.0) -> bytes:
        """Send a CI-V command and return the response data payload.

        Serial I/O errors (unplug, device busy, timeout) are translated to
        ``RigConnectionError`` so upstream callers that catch ``RigError``
        can recover gracefully.  A mid-session USB unplug used to leak a
        raw ``serial.SerialException`` past every ``RigError`` catch in
        the poll thread, killing it silently (OP-02).

        H12: ``deadline_s`` defaults to 1.0 s for set commands but
        diagnostic getters (get_freq / get_mode / get_strength / get_ptt)
        pass ``_DIAG_DEADLINE_S`` (200 ms) so a stale response from the
        rig (rig in tuning mode, brief firmware pause) doesn't hold the
        shared serial lock long enough to delay an unrelated PTT-off
        write.  Set commands keep the longer deadline because the user
        expects them to succeed rather than time out mid-tune.
        """
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            # Build frame: FE FE <to> <from> <cmd_data> FD
            frame = (
                _CIV_PREAMBLE
                + bytes([self._addr, _CIV_CONTROLLER])
                + cmd_data
                + _CIV_EOM
            )
            try:
                self._ser.reset_input_buffer()
                self._ser.write(frame)
                return self._read_response(deadline_s=deadline_s)
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Icom CI-V serial I/O failed on {self._port}: {exc}"
                ) from exc

    def _read_response(self, deadline_s: float = 1.0) -> bytes:
        """Read and parse a CI-V response frame.

        ``serial.SerialException`` raised by ``in_waiting``/``read`` (e.g.
        cable unplugged mid-read) propagates out; ``_command`` catches it
        and re-raises as ``RigConnectionError`` so the ``Rig`` surface is
        consistent.

        M-2 (audit 4.7/v0.2.9): the previous implementation polled
        ``in_waiting`` every 10 ms via ``time.sleep(0.01)`` — burning 100
        wake-ups per second while holding ``self._lock``.  Switch to a
        short-timeout blocking ``read`` so the OS schedules the wait
        instead.  Each iteration either returns bytes immediately when
        the rig replies (latency unchanged) or blocks up to 50 ms before
        looping to re-check the deadline.  Net: same worst-case
        responsiveness, no busy-loop CPU cost, and the lock release
        cadence improves because the GIL isn't held during the blocking
        ``read`` syscall.
        """
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + deadline_s
        # Snapshot and restore the user-configured timeout so this
        # function doesn't have a side effect on the next caller's
        # blocking-read semantics elsewhere.
        original_timeout = self._ser.timeout
        try:
            self._ser.timeout = 0.05
            while time.monotonic() < deadline:
                # Read whatever's buffered first (cheap, no wait), then
                # block briefly waiting for the next byte.  ``read(1)``
                # returns ``b""`` on timeout — no exception — so the
                # deadline check on the next loop iteration handles
                # end-of-time cleanly.
                avail = self._ser.in_waiting
                if avail:
                    buf.extend(self._ser.read(avail))
                else:
                    chunk = self._ser.read(1)
                    if chunk:
                        buf.extend(chunk)
                # Look for a complete response frame addressed to us
                while True:
                    start = buf.find(_CIV_PREAMBLE)
                    if start == -1:
                        break
                    end = buf.find(_CIV_EOM, start + 2)
                    if end == -1:
                        break
                    frame = buf[start + 2 : end]  # skip preamble
                    # Remove this frame from buffer
                    buf = buf[end + 1 :]
                    if len(frame) < 2:
                        continue
                    to_addr = frame[0]
                    from_addr = frame[1]
                    payload = frame[2:]
                    # Skip echo of our own command
                    if to_addr == self._addr and from_addr == _CIV_CONTROLLER:
                        continue
                    # Response from rig to us.
                    #
                    # L9: this check also implicitly rejects unsolicited
                    # broadcast frames the rig sends when the operator
                    # turns a knob — those have ``to_addr = 0x00``
                    # (transceive broadcast to all controllers) which
                    # differs from ``_CIV_CONTROLLER = 0xE0``, and
                    # from_addr must equal ``self._addr`` (the rig we're
                    # talking to specifically, not any other CI-V device
                    # on a bus topology).  So a knob-turn broadcast in
                    # flight when we issued our command cannot
                    # accidentally satisfy the predicate below and be
                    # returned as our response.
                    if to_addr == _CIV_CONTROLLER and from_addr == self._addr:
                        if payload and payload[0] == _CIV_OK:
                            return payload[1:]  # data after OK byte
                        if payload and payload[0] == _CIV_NG:
                            raise RigCommandError(
                                "CI-V command rejected (NG)",
                                command=payload.hex(),
                            )
                        # Data response (e.g. frequency read) —
                        # command echo + data
                        return payload
        finally:
            self._ser.timeout = original_timeout
        raise RigConnectionError("CI-V response timeout")

    @staticmethod
    def _bcd_byte_to_int(b: int) -> int:
        """Convert a single BCD-encoded byte to its decimal integer value.

        Each nibble holds one decimal digit: upper nibble = tens, lower = ones.
        E.g. 0x20 → 2*10 + 0 = 20; 0x41 → 4*10 + 1 = 41.
        """
        return (b >> 4) * 10 + (b & 0x0F)

    @staticmethod
    def _bcd_to_freq(data: bytes) -> int:
        """Convert CI-V BCD-encoded frequency (5 bytes, little-endian) to Hz."""
        freq = 0
        for i, byte in enumerate(data[:5]):
            lo = byte & 0x0F
            hi = (byte >> 4) & 0x0F
            freq += (hi * 10 + lo) * (10 ** (i * 2))
        return freq

    @staticmethod
    def _freq_to_bcd(hz: int) -> bytes:
        """Convert Hz to CI-V BCD-encoded frequency (5 bytes, little-endian)."""
        if hz < 0:
            raise ValueError(f"Frequency must be non-negative, got {hz}")
        result = bytearray(5)
        for i in range(5):
            lo = hz % 10
            hz //= 10
            hi = hz % 10
            hz //= 10
            result[i] = (hi << 4) | lo
        return bytes(result)


# ============================================================
# Kenwood / Elecraft protocol
# ============================================================


class KenwoodRig:
    """Direct CAT control for Kenwood and Elecraft radios.

    The Kenwood protocol is simple text: commands are ASCII strings
    terminated by ``;``. Responses echo the command prefix followed
    by data, also ``;``-terminated.

    Works with: TS-590, TS-890, TS-2000, TS-480, K3, KX3, KX2, K4.

    .. note::
        Kenwood distinguishes *read* from *set* by the presence of data
        after the command mnemonic.  ``FA;`` reads frequency; ``FA{11d};``
        sets it.  Set commands produce **no response** on all tested
        hardware (TS-590SG, TS-2000, TS-480, K3).  Set methods use
        ``_write_command`` so they do not wait for a response that will
        never arrive.
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        """Per-instance label including port (L-3)."""
        return f"Kenwood/Elecraft ({self._port})"

    def open(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                self._ser = serial.Serial(
                    self._port,
                    self._baud_rate,
                    timeout=0.5,
                    write_timeout=1.0,
                )
                self._ser.reset_input_buffer()
            except _SERIAL_IO_ERRORS as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                _close_serial_port(self._ser)
                self._ser = None

    def get_freq(self) -> int:
        resp = self._command("FA", deadline_s=_DIAG_DEADLINE_S)
        # Response: "FAnnnnnnnnnnnn;" — 11-digit frequency in Hz
        if resp.startswith("FA") and len(resp) >= 13:
            try:
                return int(resp[2:13])
            except ValueError:
                return 0
        return 0

    def set_freq(self, hz: int) -> None:
        # FA{11d}; is a set command — no response expected.
        self._write_command(f"FA{hz:011d}")

    def get_mode(self) -> tuple[str, int]:
        resp = self._command("MD", deadline_s=_DIAG_DEADLINE_S)
        # Response: "MDn;" where n is mode digit
        mode_map = {
            "1": "LSB", "2": "USB", "3": "CW", "4": "FM",
            "5": "AM", "6": "FSK", "7": "CW-R", "9": "FSK-R",
        }
        if resp.startswith("MD") and len(resp) >= 3:
            mode_name = mode_map.get(resp[2], resp[2])
            return (mode_name, 0)
        return ("", 0)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        mode_map = {
            "LSB": "1", "USB": "2", "CW": "3", "FM": "4",
            "AM": "5", "FSK": "6", "CW-R": "7", "FSK-R": "9",
        }
        digit = mode_map.get(mode.upper(), "2")
        # MD{digit}; is a set command — no response expected.
        self._write_command(f"MD{digit}")

    def get_ptt(self) -> bool:
        resp = self._command("TX", deadline_s=_DIAG_DEADLINE_S)
        # Response: "TXn;" where n=0 is RX, n=1 is TX
        if resp.startswith("TX") and len(resp) >= 3:
            return resp[2] != "0"
        return False

    def set_ptt(self, on: bool) -> None:
        # TX1;/RX; are set commands — use write-only path for robustness.
        # Kenwood radios generally echo set commands, but some firmware
        # versions and Elecraft models may not; write-only avoids a
        # potential 1-second timeout on every key.
        self._write_command("TX1" if on else "RX")

    def get_strength(self) -> int:
        resp = self._command("SM0", deadline_s=_DIAG_DEADLINE_S)
        # Response: "SM0nnnn;" — signal meter 0000-0030
        if resp.startswith("SM0") and len(resp) >= 7:
            try:
                raw = int(resp[3:7])
                # Rough conversion: 0=S0, 15=S9, 30=S9+60
                if raw <= 15:
                    return -73 - (9 - raw * 9 // 15) * 6
                return -73 + (raw - 15) * 60 // 15
            except ValueError:
                return 0
        return 0

    def ping(self) -> None:
        resp = self._command("ID")
        if not resp.startswith("ID"):
            raise RigConnectionError("No valid ID response from radio")

    def _write_command(self, cmd: str) -> None:
        """Write a set command without waiting for a response.

        All Kenwood set commands (``FA{data}``, ``MD{digit}``, ``TX1``,
        ``RX``, …) produce no response.  Callers must not block waiting
        for one.
        """
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f"{cmd};".encode("ascii"))
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Kenwood serial I/O failed on {self._port}: {exc}"
                ) from exc

    def _command(self, cmd: str, deadline_s: float = 1.0) -> str:
        """Send a Kenwood read command and return the response.

        Wraps ``serial.SerialException`` and ``OSError`` (e.g. termios.error
        on USB unplug) as ``RigConnectionError`` so a mid-session unplug
        is observable to callers that catch ``RigError`` (OP-02).

        H12: ``deadline_s`` shortens to ``_DIAG_DEADLINE_S`` (200 ms) for
        the diagnostic getters so the shared serial lock doesn't block
        PTT-off writes for the full 1 s default.
        """
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f"{cmd};".encode("ascii"))
                return self._read_response(
                    expected_prefix=cmd[:2], deadline_s=deadline_s
                )
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Kenwood serial I/O failed on {self._port}: {exc}"
                ) from exc

    def _read_response(
        self, expected_prefix: str = "", deadline_s: float = 1.0
    ) -> str:
        """Read until a ``;``-terminated response matching *expected_prefix*.

        Discards unsolicited status messages (common when the operator
        turns knobs during polling) and keeps reading until a response
        whose first characters match *expected_prefix* arrives, or until
        the 1 s deadline expires.  A ``?;`` response is treated as a
        command rejection and raises ``RigCommandError`` immediately.

        ``serial.SerialException`` raised by ``in_waiting``/``read``
        propagates out; ``_command`` catches it and re-raises as
        ``RigConnectionError``.
        """
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            avail = self._ser.in_waiting
            if avail:
                buf.extend(self._ser.read(avail))
                # Consume all complete (;-terminated) responses in the buffer.
                while b";" in buf:
                    idx = buf.index(b";")
                    text = buf[:idx].decode("ascii", errors="replace")
                    del buf[:idx + 1]
                    if text == "?":
                        raise RigCommandError(
                            "Radio rejected command (?)",
                            command=expected_prefix,
                        )
                    if not expected_prefix or text.startswith(expected_prefix):
                        return text
                    # Unsolicited message — discard and keep reading.
            else:
                time.sleep(0.01)
        raise RigConnectionError("Kenwood command timeout")


# ============================================================
# Yaesu CAT protocol
# ============================================================


class YaesuRig:
    """Direct CAT control for Yaesu radios.

    Modern Yaesu radios (FT-991, FT-991A, FT-891, FT-710, FTDX10,
    FTDX101, FT-950) and the older FT-450/450D use a Kenwood-like text
    protocol with ``;``-terminated commands. Older radios (FT-817/818,
    FT-857) use a binary protocol; this class targets the text variant.

    .. note::
        Yaesu *set* commands (e.g. ``TX1;``, ``TX0;``, ``FA{Nd};``,
        ``MD0{digit};``) execute silently — the radio sends **no response**.
        Read commands (e.g. ``TX;``, ``FA;``, ``MD0;``) respond with the
        current value.  All set methods use ``_write_command`` to avoid a
        1-second timeout waiting for a response that will never arrive.

    .. note::
        The ``FA`` frequency field width (``N`` digits above) is
        model-dependent, and sending the wrong width is rejected outright
        with ``?;`` — not just ignored.  FT-450/450D (HF+6m only) uses 8
        digits, no leading zero (e.g. ``FA14230000;``); FT-991A and other
        "modern CAT" rigs use 9, zero-padded.  ``set_freq``/``get_freq``
        detect and cache the width from a live read rather than hardcoding
        one, so both families work through the same class.
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = 38400,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        #: FA frequency field width in digits, detected from a live
        #: get_freq() response — see set_freq()/get_freq() for why this
        #: can't just be a fixed constant.
        self._freq_digits: int | None = None

    @property
    def name(self) -> str:
        """Per-instance label including port (L-3)."""
        return f"Yaesu CAT ({self._port})"

    def open(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                self._ser = serial.Serial(
                    self._port,
                    self._baud_rate,
                    timeout=0.5,
                    write_timeout=1.0,
                )
                self._ser.reset_input_buffer()
            except _SERIAL_IO_ERRORS as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                _close_serial_port(self._ser)
                self._ser = None

    def get_freq(self) -> int:
        resp = self._command("FA", deadline_s=_DIAG_DEADLINE_S)
        # Response: "FAnnnnnnnn;" — the digit count is model-dependent:
        # FT-450/450D (HF+6m only) reports/expects 8 digits with no
        # leading zero (e.g. "FA14230000;"); FT-991A and other "modern
        # CAT" rigs (FTDX10, FT-891, FT-710, FTDX101, FT-950) use 9,
        # zero-padded.  Remember the width here so set_freq() can match
        # it — sending the wrong width is rejected outright with "?;"
        # even though this read is lenient about either length.
        if resp.startswith("FA") and len(resp) >= 10:
            try:
                self._freq_digits = len(resp) - 2
                return int(resp[2:])
            except ValueError:
                return 0
        return 0

    def set_freq(self, hz: int) -> None:
        if self._freq_digits is None:
            # Digit width not known yet (no get_freq() has succeeded on
            # this connection) — probe once with a live read before the
            # very first set, so a Band Plan tune right after Connect
            # doesn't race the 1 Hz poll loop for this detection.
            # Best-effort: if the probe itself fails, fall through to the
            # 9-digit default below and try anyway.
            try:
                self.get_freq()
            except Exception:  # noqa: BLE001 — probe only, must not block set_freq
                pass
        digits = self._freq_digits or 9
        # FA{Nd}; is a set command — Yaesu sends no response.
        self._write_command(f"FA{hz:0{digits}d}")

    def get_mode(self) -> tuple[str, int]:
        resp = self._command("MD0", deadline_s=_DIAG_DEADLINE_S)
        # Response: "MD0n;" where n is mode digit
        mode_map = {
            "1": "LSB", "2": "USB", "3": "CW-U", "4": "FM",
            "5": "AM", "6": "RTTY-L", "7": "CW-L", "8": "DATA-L",
            "9": "RTTY-U", "A": "DATA-FM", "B": "FM-N",
            "C": "DATA-U", "D": "AM-N", "E": "C4FM",
        }
        if resp.startswith("MD0") and len(resp) >= 4:
            mode_name = mode_map.get(resp[3], resp[3])
            return (mode_name, 0)
        return ("", 0)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        mode_map = {
            "LSB": "1", "USB": "2", "CW-U": "3", "FM": "4",
            "AM": "5", "CW": "3", "DATA-U": "C", "DATA-L": "8",
        }
        digit = mode_map.get(mode.upper(), "2")
        # MD0{digit}; is a set command — Yaesu sends no response.
        self._write_command(f"MD0{digit}")

    def get_ptt(self) -> bool:
        # Read TX status
        resp = self._command("TX", deadline_s=_DIAG_DEADLINE_S)
        if resp.startswith("TX") and len(resp) >= 3:
            return resp[2] != "0"
        return False

    def set_ptt(self, on: bool) -> None:
        # TX1; / TX0; are set commands — Yaesu sends no response.
        # Using _write_command avoids a 1-second timeout on every key.
        self._write_command("TX1" if on else "TX0")

    def get_strength(self) -> int:
        resp = self._command("SM0", deadline_s=_DIAG_DEADLINE_S)
        if resp.startswith("SM0") and len(resp) >= 6:
            try:
                raw = int(resp[3:])
                # Yaesu meter: 0-255, S9 ~ 120
                if raw <= 120:
                    return -73 - (9 - raw * 9 // 120) * 6
                return -73 + (raw - 120) * 60 // 135
            except ValueError:
                return 0
        return 0

    def ping(self) -> None:
        resp = self._command("ID")
        if not resp.startswith("ID"):
            raise RigConnectionError("No valid ID response from radio")

    def _write_command(self, cmd: str) -> None:
        """Write a set command without waiting for a response.

        All Yaesu set commands (``FA{data}``, ``MD0{digit}``, ``TX1``,
        ``TX0``, …) execute silently — the radio sends no response frame.
        Callers must not block waiting for one.
        """
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f"{cmd};".encode("ascii"))
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Yaesu serial I/O failed on {self._port}: {exc}"
                ) from exc

    def _command(self, cmd: str, deadline_s: float = 1.0) -> str:
        """Send a Yaesu read command and return the response.

        Wraps ``serial.SerialException`` and ``OSError`` (e.g. termios.error
        on USB unplug) as ``RigConnectionError`` so a mid-session unplug
        is observable to callers that catch ``RigError`` (OP-02).

        H12: ``deadline_s`` shortens to ``_DIAG_DEADLINE_S`` (200 ms) for
        diagnostic getters so the serial lock turns over fast enough to
        let PTT-off writes pre-empt within ~200 ms instead of 1 s.
        """
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            try:
                self._ser.reset_input_buffer()
                self._ser.write(f"{cmd};".encode("ascii"))
                return self._read_response(
                    expected_prefix=cmd[:2], deadline_s=deadline_s
                )
            except _SERIAL_IO_ERRORS as exc:
                raise RigConnectionError(
                    f"Yaesu serial I/O failed on {self._port}: {exc}"
                ) from exc

    def _read_response(
        self, expected_prefix: str = "", deadline_s: float = 1.0
    ) -> str:
        """Read until a ``;``-terminated response matching *expected_prefix*.

        Discards unsolicited status messages and keeps reading until a
        response starting with *expected_prefix* arrives or the deadline
        expires.  A ``?;`` response is treated as a command rejection and
        raises ``RigCommandError`` immediately.  ``serial.SerialException``
        propagates to ``_command`` which re-raises as ``RigConnectionError``.
        """
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            avail = self._ser.in_waiting
            if avail:
                buf.extend(self._ser.read(avail))
                while b";" in buf:
                    idx = buf.index(b";")
                    text = buf[:idx].decode("ascii", errors="replace")
                    del buf[:idx + 1]
                    if text == "?":
                        raise RigCommandError(
                            "Radio rejected command (?)",
                            command=expected_prefix,
                        )
                    if not expected_prefix or text.startswith(expected_prefix):
                        return text
                    # Unsolicited message — discard and keep reading.
            else:
                time.sleep(0.01)
        raise RigConnectionError("Yaesu command timeout")


# === Factory helper ===

#: Map of protocol names to classes for the settings UI.
SERIAL_RIG_PROTOCOLS: dict[str, type] = {
    "PTT Only (DTR/RTS)": SerialPttRig,
    "Icom CI-V": IcomCIVRig,
    "Kenwood / Elecraft": KenwoodRig,
    "Yaesu CAT": YaesuRig,
}


def create_serial_rig(
    protocol: str,
    port: str,
    baud_rate: int = 9600,
    ci_v_address: int = 0x94,
    ptt_line: str = "DTR",
) -> SerialPttRig | IcomCIVRig | KenwoodRig | YaesuRig:
    """Factory: create the right serial rig backend from a protocol name."""
    if protocol == "Icom CI-V":
        return IcomCIVRig(port, baud_rate=baud_rate, ci_v_address=ci_v_address)
    if protocol == "Kenwood / Elecraft":
        return KenwoodRig(port, baud_rate=baud_rate)
    if protocol == "Yaesu CAT":
        return YaesuRig(port, baud_rate=baud_rate)
    return SerialPttRig(port, baud_rate=baud_rate, ptt_line=ptt_line)


__all__ = [
    "ICOM_ADDRESSES",
    "IcomCIVRig",
    "KenwoodRig",
    "SERIAL_RIG_PROTOCOLS",
    "SerialPttRig",
    "YaesuRig",
    "create_serial_rig",
]
