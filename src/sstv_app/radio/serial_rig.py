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
  FT-891, FT-710, FTDX10, FTDX101, FT-950, FT-817/818.

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

import threading
import time

import serial

from sstv_app.radio.exceptions import RigCommandError, RigConnectionError


# ============================================================
# PTT-only via serial control lines
# ============================================================


class SerialPttRig:
    """PTT via DTR or RTS on a serial port.

    The simplest possible rig interface: toggling a serial control line
    is all many operators need for SSTV (VOX handles the rest, or the
    radio has its own PTT-sense input).
    """

    name: str = "Serial PTT"

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
            except serial.SerialException as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._set_ptt_line(False)
                    self._ser.close()
                except serial.SerialException:
                    pass
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
            if self._ptt_line == "RTS":
                return self._ser.rts
            return self._ser.dtr

    def set_ptt(self, on: bool) -> None:
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            self._set_ptt_line(on)

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

    name: str = "Icom CI-V"

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
            except serial.SerialException as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except serial.SerialException:
                    pass
                self._ser = None

    def get_freq(self) -> int:
        """Read the current VFO frequency."""
        resp = self._command(b"\x03")
        if len(resp) < 5:
            return 0
        return self._bcd_to_freq(resp)

    def set_freq(self, hz: int) -> None:
        data = self._freq_to_bcd(hz)
        self._command(b"\x05" + data)

    def get_mode(self) -> tuple[str, int]:
        resp = self._command(b"\x04")
        if len(resp) < 1:
            return ("", 0)
        mode_map = {
            0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW",
            0x04: "RTTY", 0x05: "FM", 0x07: "CW-R", 0x08: "RTTY-R",
            0x17: "DV",
        }
        mode_name = mode_map.get(resp[0], f"0x{resp[0]:02X}")
        passband = 0
        if len(resp) >= 2:
            passband = resp[1] * 100  # rough approximation
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
        resp = self._command(b"\x1c\x00")
        if len(resp) >= 1:
            return resp[0] != 0x00
        return False

    def set_ptt(self, on: bool) -> None:
        # CI-V command 0x1C 0x00 <01=TX, 00=RX>
        self._command(b"\x1c\x00" + (b"\x01" if on else b"\x00"))

    def get_strength(self) -> int:
        # CI-V command 0x15 0x02 — read S-meter
        resp = self._command(b"\x15\x02")
        if len(resp) >= 2:
            raw = (resp[0] << 8) | resp[1]
            # Icom S-meter: 0000=S0, 0120=S9, 0241=S9+60
            # Rough conversion to dBm
            if raw <= 120:
                return -73 - (9 - raw * 9 // 120) * 6
            return -73 + (raw - 120) * 60 // 121
        return 0

    def ping(self) -> None:
        self.get_freq()

    # === CI-V internals ===

    def _command(self, cmd_data: bytes) -> bytes:
        """Send a CI-V command and return the response data payload."""
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
            self._ser.reset_input_buffer()
            self._ser.write(frame)
            return self._read_response()

    def _read_response(self) -> bytes:
        """Read and parse a CI-V response frame."""
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            avail = self._ser.in_waiting
            if avail:
                buf.extend(self._ser.read(avail))
            else:
                time.sleep(0.01)
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
                # Response from rig to us
                if to_addr == _CIV_CONTROLLER and from_addr == self._addr:
                    if payload and payload[0] == _CIV_OK:
                        return payload[1:]  # data after OK byte
                    if payload and payload[0] == _CIV_NG:
                        raise RigCommandError(
                            "CI-V command rejected (NG)",
                            command=payload.hex(),
                        )
                    # Data response (e.g. frequency read) — command echo + data
                    return payload
        raise RigConnectionError("CI-V response timeout")

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
    """

    name: str = "Kenwood/Elecraft"

    def __init__(
        self,
        port: str,
        baud_rate: int = 9600,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

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
            except serial.SerialException as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except serial.SerialException:
                    pass
                self._ser = None

    def get_freq(self) -> int:
        resp = self._command("FA")
        # Response: "FAnnnnnnnnnnnn;" — 11-digit frequency in Hz
        if resp.startswith("FA") and len(resp) >= 13:
            try:
                return int(resp[2:13])
            except ValueError:
                return 0
        return 0

    def set_freq(self, hz: int) -> None:
        self._command(f"FA{hz:011d}")

    def get_mode(self) -> tuple[str, int]:
        resp = self._command("MD")
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
        self._command(f"MD{digit}")

    def get_ptt(self) -> bool:
        resp = self._command("TX")
        # Response: "TXn;" where n=0 is RX, n=1 is TX
        if resp.startswith("TX") and len(resp) >= 3:
            return resp[2] != "0"
        return False

    def set_ptt(self, on: bool) -> None:
        if on:
            self._command("TX1")
        else:
            self._command("RX")

    def get_strength(self) -> int:
        resp = self._command("SM0")
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

    def _command(self, cmd: str) -> str:
        """Send a Kenwood command and return the response."""
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            self._ser.reset_input_buffer()
            self._ser.write(f"{cmd};".encode("ascii"))
            return self._read_response(expected_prefix=cmd[:2])

    def _read_response(self, expected_prefix: str = "") -> str:
        """Read until a ``;``-terminated response matching *expected_prefix*.

        Discards unsolicited status messages (common when the operator
        turns knobs during polling) and keeps reading until a response
        whose first characters match *expected_prefix* arrives, or until
        the 1 s deadline expires.
        """
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            avail = self._ser.in_waiting
            if avail:
                buf.extend(self._ser.read(avail))
                # Consume all complete (;-terminated) responses in the buffer.
                while b";" in buf:
                    idx = buf.index(b";")
                    text = buf[:idx].decode("ascii", errors="replace")
                    del buf[:idx + 1]
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

    Modern Yaesu radios (FT-991A, FT-891, FT-710, FTDX10, FTDX101,
    FT-950) use a Kenwood-like text protocol with ``;``-terminated
    commands. Older radios (FT-817/818, FT-857) use a binary protocol;
    this class targets the modern text variant.
    """

    name: str = "Yaesu CAT"

    def __init__(
        self,
        port: str,
        baud_rate: int = 38400,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

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
            except serial.SerialException as exc:
                self._ser = None
                raise RigConnectionError(
                    f"Could not open {self._port}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except serial.SerialException:
                    pass
                self._ser = None

    def get_freq(self) -> int:
        resp = self._command("FA")
        # Response: "FAnnnnnnnn;" — 8 or 9 digit frequency in Hz
        if resp.startswith("FA") and len(resp) >= 10:
            try:
                return int(resp[2:])
            except ValueError:
                return 0
        return 0

    def set_freq(self, hz: int) -> None:
        self._command(f"FA{hz:09d}")

    def get_mode(self) -> tuple[str, int]:
        resp = self._command("MD0")
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
        self._command(f"MD0{digit}")

    def get_ptt(self) -> bool:
        # Read TX status
        resp = self._command("TX")
        if resp.startswith("TX") and len(resp) >= 3:
            return resp[2] != "0"
        return False

    def set_ptt(self, on: bool) -> None:
        if on:
            self._command("TX1")
        else:
            self._command("TX0")

    def get_strength(self) -> int:
        resp = self._command("SM0")
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

    def _command(self, cmd: str) -> str:
        """Send a Yaesu command and return the response."""
        with self._lock:
            if self._ser is None:
                raise RigConnectionError("Serial port not open")
            self._ser.reset_input_buffer()
            self._ser.write(f"{cmd};".encode("ascii"))
            return self._read_response(expected_prefix=cmd[:2])

    def _read_response(self, expected_prefix: str = "") -> str:
        """Read until a ``;``-terminated response matching *expected_prefix*.

        Discards unsolicited status messages and keeps reading until a
        response starting with *expected_prefix* arrives or the deadline
        expires.
        """
        if self._ser is None:
            raise RigConnectionError("Serial port not open")
        buf = bytearray()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            avail = self._ser.in_waiting
            if avail:
                buf.extend(self._ser.read(avail))
                while b";" in buf:
                    idx = buf.index(b";")
                    text = buf[:idx].decode("ascii", errors="replace")
                    del buf[:idx + 1]
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
