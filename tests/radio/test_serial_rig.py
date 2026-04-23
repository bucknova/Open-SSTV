# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for ``open_sstv.radio.serial_rig.IcomCIVRig``.

All tests mock ``_command()`` so no serial port is required.  They guard
specifically against the CI-V command-echo-byte bug (C-1 through C-4)
where every data-response payload includes the command echo as its first
byte, and the accessors must strip it before parsing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from open_sstv.radio.serial_rig import IcomCIVRig


@pytest.fixture
def rig() -> IcomCIVRig:
    """Return an IcomCIVRig whose serial port is never opened."""
    r = IcomCIVRig.__new__(IcomCIVRig)
    # Minimal init — skip serial.Serial entirely.
    import threading
    r._port = "/dev/null"
    r._baud_rate = 19200
    r._addr = 0x94
    r._lock = threading.Lock()
    r._ser = None
    return r


# ---------------------------------------------------------------------------
# C-1 — get_freq: command echo byte stripped before _bcd_to_freq
# ---------------------------------------------------------------------------


def test_get_freq_strips_command_echo(rig: IcomCIVRig) -> None:
    """Payload [0x03, b0..b4] must decode as 14.230 MHz, not garbage."""
    # 14,230,000 Hz in BCD (LSB first): 00 00 23 14 00
    payload = bytes([0x03, 0x00, 0x00, 0x23, 0x14, 0x00])
    with patch.object(rig, "_command", return_value=payload):
        freq = rig.get_freq()
    assert freq == 14_230_000


def test_get_freq_with_echo_returns_zero_when_too_short(rig: IcomCIVRig) -> None:
    """Payload shorter than 6 bytes (cmd echo + 5 BCD) → 0."""
    with patch.object(rig, "_command", return_value=bytes([0x03, 0x00, 0x00])):
        assert rig.get_freq() == 0


def test_bcd_to_freq_round_trip() -> None:
    """Static helper decodes correctly for several spot frequencies.

    BCD encoding (LSB-first, 2 digits per byte, hi-nibble = more significant):
    For byte i, contribution = (hi*10+lo) * 10^(2*i).
    Example: 14,074,000 Hz
      i=1: 0x40 → (4*10+0)*100 = 4 000   (thousands + hundreds)
      i=2: 0x07 → (0*10+7)*10000 = 70 000 (hundred-thousands + ten-thousands)
      i=3: 0x14 → (1*10+4)*1e6 = 14 000 000
    """
    cases = [
        # 14.074 MHz (FT8 20m)
        (14_074_000,  bytes([0x00, 0x40, 0x07, 0x14, 0x00])),
        # 7.074 MHz (FT8 40m)
        (7_074_000,   bytes([0x00, 0x40, 0x07, 0x07, 0x00])),
        # 144.200 MHz (2m SSB calling)
        (144_200_000, bytes([0x00, 0x00, 0x20, 0x44, 0x01])),
    ]
    for expected_hz, bcd in cases:
        assert IcomCIVRig._bcd_to_freq(bcd) == expected_hz, (
            f"_bcd_to_freq({bcd.hex()}) should be {expected_hz}"
        )


# ---------------------------------------------------------------------------
# C-2 — get_mode: command echo byte stripped; USB returned as USB
# ---------------------------------------------------------------------------


def test_get_mode_strips_command_echo_usb(rig: IcomCIVRig) -> None:
    """Payload [0x04, 0x01, 0x10] → ('USB', 1600), not ('RTTY', 100)."""
    # 0x04 = cmd echo, 0x01 = USB, 0x10 = 16 → 16*100 = 1600 Hz passband
    payload = bytes([0x04, 0x01, 0x10])
    with patch.object(rig, "_command", return_value=payload):
        mode, passband = rig.get_mode()
    assert mode == "USB"
    assert passband == 1600


def test_get_mode_strips_command_echo_lsb(rig: IcomCIVRig) -> None:
    payload = bytes([0x04, 0x00, 0x18])  # cmd echo, LSB, 0x18=24 → 2400 Hz
    with patch.object(rig, "_command", return_value=payload):
        mode, passband = rig.get_mode()
    assert mode == "LSB"
    assert passband == 2400


def test_get_mode_without_echo_byte_would_give_rtty(rig: IcomCIVRig) -> None:
    """Sanity check: if we DID use resp[0] (the echo byte 0x04), we'd get RTTY."""
    # This documents exactly the regression scenario — don't use resp[0].
    payload = bytes([0x04, 0x01, 0x10])
    # resp[0] == 0x04 maps to "RTTY" in the mode_map
    assert payload[0] == 0x04  # the echo byte
    assert payload[1] == 0x01  # the real mode byte (USB)


# ---------------------------------------------------------------------------
# C-3 — get_ptt: command echo byte stripped; state in resp[2]
# ---------------------------------------------------------------------------


def test_get_ptt_returns_false_when_rx(rig: IcomCIVRig) -> None:
    # [cmd_echo(0x1C), subcmd(0x00), state(0x00)]
    with patch.object(rig, "_command", return_value=bytes([0x1C, 0x00, 0x00])):
        assert rig.get_ptt() is False


def test_get_ptt_returns_true_when_tx(rig: IcomCIVRig) -> None:
    with patch.object(rig, "_command", return_value=bytes([0x1C, 0x00, 0x01])):
        assert rig.get_ptt() is True


def test_get_ptt_echo_byte_alone_would_be_truthy(rig: IcomCIVRig) -> None:
    """Documents the regression: echo byte 0x1C != 0x00, so old code
    always returned True regardless of actual TX state."""
    assert bytes([0x1C, 0x00, 0x00])[0] != 0x00  # echo byte is truthy


# ---------------------------------------------------------------------------
# C-4 — get_strength: command echo byte stripped; value in resp[2:4]
# ---------------------------------------------------------------------------


def test_get_strength_s9(rig: IcomCIVRig) -> None:
    """S9 corresponds to raw=120 → −73 dBm.

    IC-7300 encodes 120 as BCD bytes [0x01, 0x20], not binary [0x00, 0x78].
    _bcd_byte_to_int(0x01)=1, _bcd_byte_to_int(0x20)=20 → 1*100+20 = 120.
    """
    # [cmd_echo(0x15), subcmd(0x02), hi_bcd(0x01), lo_bcd(0x20)]
    with patch.object(rig, "_command", return_value=bytes([0x15, 0x02, 0x01, 0x20])):
        strength = rig.get_strength()
    assert strength == -73


def test_get_strength_s0(rig: IcomCIVRig) -> None:
    """S0 corresponds to raw=0 → −127 dBm."""
    with patch.object(rig, "_command", return_value=bytes([0x15, 0x02, 0x00, 0x00])):
        strength = rig.get_strength()
    assert strength == -73 - 9 * 6  # S0 = -127 dBm


def test_get_strength_s9_plus_60(rig: IcomCIVRig) -> None:
    """S9+60 corresponds to raw=241 → −13 dBm."""
    # [cmd_echo(0x15), subcmd(0x02), hi_bcd(0x02), lo_bcd(0x41)]
    # _bcd_byte_to_int(0x02)=2, _bcd_byte_to_int(0x41)=41 → 2*100+41 = 241
    with patch.object(rig, "_command", return_value=bytes([0x15, 0x02, 0x02, 0x41])):
        strength = rig.get_strength()
    assert strength == -73 + (241 - 120) * 60 // 121  # == -13 dBm


def test_get_strength_bcd_not_binary(rig: IcomCIVRig) -> None:
    """Documents C-4b regression: naive binary read of the S9 BCD payload
    gives 288 instead of 120, mapping to a nonsensical ~S9+80 reading."""
    # S9 BCD payload bytes (after echo + subcmd): 0x01, 0x20
    # Binary interpretation: (0x01 << 8) | 0x20 = 288 — wrong
    # BCD interpretation: 1*100 + 20 = 120 — correct
    assert (0x01 << 8) | 0x20 == 288  # what the old code would compute


def test_get_strength_fixed_raw_without_fix_would_be_constant(rig: IcomCIVRig) -> None:
    """Documents the C-4 regression: old code computed raw=(0x15<<8)|0x02=5378
    regardless of actual signal, so S-meter never changed."""
    # Echo byte 0x15, subcmd 0x02
    old_raw = (0x15 << 8) | 0x02
    assert old_raw == 0x1502  # 5378 — always the same


# ---------------------------------------------------------------------------
# OP-02 — serial.SerialException is wrapped as RigConnectionError
# ---------------------------------------------------------------------------


class TestSerialExceptionWrapping:
    """Regression tests for OP-02: CAT backends must translate pyserial
    exceptions to ``RigConnectionError`` so a mid-session USB unplug
    doesn't kill the rig poll thread with a raw ``serial.SerialException``.
    """

    def test_icom_command_wraps_serial_exception(self) -> None:
        """IcomCIVRig._command raising SerialException → RigConnectionError."""
        import threading

        import serial as _pyserial

        from open_sstv.radio.exceptions import RigConnectionError

        r = IcomCIVRig.__new__(IcomCIVRig)
        r._port = "/dev/null"
        r._baud_rate = 19200
        r._addr = 0x94
        r._lock = threading.Lock()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = _pyserial.SerialException("device disconnected")

        with pytest.raises(RigConnectionError, match="Icom CI-V serial I/O failed"):
            r._command(b"\x03")

    def test_kenwood_command_wraps_serial_exception(self) -> None:
        import threading

        import serial as _pyserial

        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = KenwoodRig.__new__(KenwoodRig)
        r._port = "/dev/null"
        r._baud_rate = 9600
        r._lock = threading.Lock()
        r._ser = MagicMock()
        r._ser.write.side_effect = _pyserial.SerialException("pipe broken")

        with pytest.raises(RigConnectionError, match="Kenwood serial I/O failed"):
            r._command("FA")

    def test_yaesu_command_wraps_serial_exception(self) -> None:
        import threading

        import serial as _pyserial

        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import YaesuRig

        r = YaesuRig.__new__(YaesuRig)
        r._port = "/dev/null"
        r._baud_rate = 38400
        r._lock = threading.Lock()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = _pyserial.SerialException("readiness error")

        with pytest.raises(RigConnectionError, match="Yaesu serial I/O failed"):
            r._command("FA")

    def test_serial_ptt_set_ptt_wraps_serial_exception(self) -> None:
        """SerialPttRig.set_ptt must also wrap SerialException."""
        import threading

        import serial as _pyserial

        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import SerialPttRig

        r = SerialPttRig.__new__(SerialPttRig)
        r._port = "/dev/null"
        r._baud_rate = 9600
        r._ptt_line = "DTR"
        r._lock = threading.Lock()

        class _BadSer:
            @property
            def dtr(self) -> bool:
                return False

            @dtr.setter
            def dtr(self, value: bool) -> None:
                raise _pyserial.SerialException("serial write failed")

        r._ser = _BadSer()

        with pytest.raises(RigConnectionError, match="Serial PTT write failed"):
            r.set_ptt(True)


# ---------------------------------------------------------------------------
# Y-1 through Y-3 — YaesuRig.set_ptt write-only path (FT-991 timeout fix)
# ---------------------------------------------------------------------------


def _make_yaesu_rig() -> "YaesuRig":
    from open_sstv.radio.serial_rig import YaesuRig
    import threading

    r = YaesuRig.__new__(YaesuRig)
    r._port = "/dev/null"
    r._baud_rate = 38400
    r._lock = threading.Lock()
    r._ser = None
    return r


class TestYaesuSetPtt:
    """set_ptt must use a write-only path — no 1-second timeout for TX1;/TX0;."""

    def test_set_ptt_true_no_response_succeeds(self) -> None:
        """TX1; sent, no response — must return immediately without raising."""
        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r.set_ptt(True)
        r._ser.write.assert_called_once_with(b"TX1;")
        r._ser.read.assert_not_called()

    def test_set_ptt_false_no_response_succeeds(self) -> None:
        """TX0; sent, no response — must return immediately without raising."""
        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r.set_ptt(False)
        r._ser.write.assert_called_once_with(b"TX0;")
        r._ser.read.assert_not_called()

    def test_set_ptt_echoed_response_does_not_block(self) -> None:
        """A radio that echoes TX1; must not block set_ptt or cause an error."""
        r = _make_yaesu_rig()
        mock_ser = MagicMock()
        mock_ser.in_waiting = 4
        mock_ser.read.return_value = b"TX1;"
        r._ser = mock_ser
        # write-only path: does not attempt to read, so no blocking
        r.set_ptt(True)
        mock_ser.write.assert_called_once_with(b"TX1;")
        mock_ser.read.assert_not_called()


class TestYaesuQuestionMarkError:
    """?; from the radio must raise RigCommandError, not silently time out."""

    def test_question_mark_via_read_command_raises(self) -> None:
        from open_sstv.radio.exceptions import RigCommandError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        mock_ser = MagicMock()
        # Simulate radio returning ?; immediately
        mock_ser.in_waiting = 2
        mock_ser.read.return_value = b"?;"
        r._ser = mock_ser

        with pytest.raises(RigCommandError, match="Radio rejected command"):
            r._read_response(expected_prefix="FA")

    def test_question_mark_error_carries_command_context(self) -> None:
        from open_sstv.radio.exceptions import RigCommandError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        mock_ser = MagicMock()
        mock_ser.in_waiting = 2
        mock_ser.read.return_value = b"?;"
        r._ser = mock_ser

        exc = pytest.raises(RigCommandError, r._read_response, expected_prefix="ID")
        assert exc.value.command == "ID"


# ---------------------------------------------------------------------------
# K-1 through K-3 — KenwoodRig.set_ptt write-only path (robustness fix)
# ---------------------------------------------------------------------------


def _make_kenwood_rig() -> "KenwoodRig":
    from open_sstv.radio.serial_rig import KenwoodRig
    import threading

    r = KenwoodRig.__new__(KenwoodRig)
    r._port = "/dev/null"
    r._baud_rate = 9600
    r._lock = threading.Lock()
    r._ser = None
    return r


class TestKenwoodSetPtt:
    """set_ptt must use a write-only path for cross-firmware robustness."""

    def test_set_ptt_true_no_response_succeeds(self) -> None:
        """TX1; sent, no response — must return without raising."""
        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r.set_ptt(True)
        r._ser.write.assert_called_once_with(b"TX1;")
        r._ser.read.assert_not_called()

    def test_set_ptt_false_sends_rx(self) -> None:
        """RX; is the Kenwood receive command — must be sent for set_ptt(False)."""
        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r.set_ptt(False)
        r._ser.write.assert_called_once_with(b"RX;")
        r._ser.read.assert_not_called()

    def test_set_ptt_echoed_response_does_not_block(self) -> None:
        """Kenwood radios that echo TX1; must not block set_ptt."""
        r = _make_kenwood_rig()
        mock_ser = MagicMock()
        mock_ser.in_waiting = 4
        mock_ser.read.return_value = b"TX1;"
        r._ser = mock_ser
        r.set_ptt(True)
        mock_ser.write.assert_called_once_with(b"TX1;")
        mock_ser.read.assert_not_called()


class TestKenwoodQuestionMarkError:
    """?; from the radio must raise RigCommandError, not silently time out."""

    def test_question_mark_via_read_command_raises(self) -> None:
        from open_sstv.radio.exceptions import RigCommandError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        mock_ser = MagicMock()
        mock_ser.in_waiting = 2
        mock_ser.read.return_value = b"?;"
        r._ser = mock_ser

        with pytest.raises(RigCommandError, match="Radio rejected command"):
            r._read_response(expected_prefix="FA")

    def test_question_mark_error_carries_command_context(self) -> None:
        from open_sstv.radio.exceptions import RigCommandError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        mock_ser = MagicMock()
        mock_ser.in_waiting = 2
        mock_ser.read.return_value = b"?;"
        r._ser = mock_ser

        exc = pytest.raises(RigCommandError, r._read_response, expected_prefix="MD")
        assert exc.value.command == "MD"


# ---------------------------------------------------------------------------
# K-4 / K-5 — KenwoodRig set_freq and set_mode use write-only path
# (FA{data}; and MD{digit}; are set commands — no response on any tested HW)
# ---------------------------------------------------------------------------


class TestKenwoodSetFreqAndMode:
    def test_set_freq_no_response_succeeds(self) -> None:
        """FA{11d}; is a set command; must not block waiting for a response."""
        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r.set_freq(14_230_000)
        r._ser.write.assert_called_once_with(b"FA00014230000;")
        r._ser.read.assert_not_called()

    def test_set_mode_no_response_succeeds(self) -> None:
        """MD{digit}; is a set command; must not block waiting for a response."""
        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r.set_mode("USB", 0)
        r._ser.write.assert_called_once_with(b"MD2;")
        r._ser.read.assert_not_called()

    def test_set_mode_unknown_falls_back_to_usb(self) -> None:
        """Unmapped mode name defaults to USB (digit '2')."""
        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r.set_mode("OLIVIA", 0)
        r._ser.write.assert_called_once_with(b"MD2;")


# ---------------------------------------------------------------------------
# Y-4 / Y-5 — YaesuRig set_freq and set_mode use write-only path
# (FA{data}; and MD0{digit}; are set commands — Yaesu sends no response)
# ---------------------------------------------------------------------------


class TestYaesuSetFreqAndMode:
    def test_set_freq_no_response_succeeds(self) -> None:
        """FA{9d}; is a set command; must not block waiting for a response."""
        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r.set_freq(14_230_000)
        r._ser.write.assert_called_once_with(b"FA014230000;")
        r._ser.read.assert_not_called()

    def test_set_mode_no_response_succeeds(self) -> None:
        """MD0{digit}; is a set command; must not block waiting for a response."""
        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r.set_mode("USB", 0)
        r._ser.write.assert_called_once_with(b"MD02;")
        r._ser.read.assert_not_called()

    def test_set_mode_unknown_falls_back_to_usb(self) -> None:
        """Unmapped mode name defaults to USB (digit '2')."""
        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r.set_mode("OLIVIA", 0)
        r._ser.write.assert_called_once_with(b"MD02;")


# ---------------------------------------------------------------------------
# OP-TX-01 — OSError (termios.error) is wrapped as RigConnectionError
#
# USB unplug raises termios.error (errno 6, "Device not configured") from
# reset_input_buffer().  termios.error is a subclass of OSError.  All
# _command() and _write_command() methods must catch it and re-raise as
# RigConnectionError so the finally block in _run_tx can catch RigError
# and the GUI always unfreezes.
# ---------------------------------------------------------------------------


class TestOSErrorWrapping:
    """Regression: termios.error / OSError from a mid-TX USB unplug must
    be translated to RigConnectionError in every rig backend, not leak raw.
    """

    def _icom_rig(self) -> IcomCIVRig:
        import threading
        r = IcomCIVRig.__new__(IcomCIVRig)
        r._port = "/dev/null"
        r._baud_rate = 19200
        r._addr = 0x94
        r._lock = threading.Lock()
        r._ser = MagicMock()
        return r

    def _kenwood_rig(self) -> "KenwoodRig":
        return _make_kenwood_rig()

    def _yaesu_rig(self) -> "YaesuRig":
        return _make_yaesu_rig()

    def test_icom_command_wraps_oserror(self) -> None:
        """IcomCIVRig._command: termios.error from reset_input_buffer → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError

        r = self._icom_rig()
        r._ser.reset_input_buffer.side_effect = OSError(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Icom CI-V serial I/O failed"):
            r._command(b"\x1c\x00\x00")

    def test_icom_command_wraps_oserror_on_write(self) -> None:
        """IcomCIVRig._command: OSError from write() → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError

        r = self._icom_rig()
        r._ser.write.side_effect = OSError(5, "Input/output error")

        with pytest.raises(RigConnectionError, match="Icom CI-V serial I/O failed"):
            r._command(b"\x03")

    def test_kenwood_command_wraps_oserror(self) -> None:
        """KenwoodRig._command: OSError from reset_input_buffer → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = OSError(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Kenwood serial I/O failed"):
            r._command("FA")

    def test_kenwood_write_command_wraps_oserror(self) -> None:
        """KenwoodRig._write_command: OSError → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r._ser.write.side_effect = OSError(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Kenwood serial I/O failed"):
            r._write_command("TX1")

    def test_yaesu_command_wraps_oserror(self) -> None:
        """YaesuRig._command: OSError from reset_input_buffer → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = OSError(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Yaesu serial I/O failed"):
            r._command("FA")

    def test_yaesu_write_command_wraps_oserror(self) -> None:
        """YaesuRig._write_command: OSError → RigConnectionError."""
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r._ser.write.side_effect = OSError(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Yaesu serial I/O failed"):
            r._write_command("TX0")

    def test_serial_ptt_set_ptt_wraps_oserror(self) -> None:
        """SerialPttRig.set_ptt: OSError from DTR write → RigConnectionError."""
        import threading
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import SerialPttRig

        r = SerialPttRig.__new__(SerialPttRig)
        r._port = "/dev/null"
        r._baud_rate = 9600
        r._ptt_line = "DTR"
        r._lock = threading.Lock()

        class _DisconnectedSer:
            @property
            def dtr(self) -> bool:
                return False

            @dtr.setter
            def dtr(self, value: bool) -> None:
                raise OSError(6, "Device not configured")

        r._ser = _DisconnectedSer()

        with pytest.raises(RigConnectionError, match="Serial PTT write failed"):
            r.set_ptt(True)


# ---------------------------------------------------------------------------
# OP-TX-02 — termios.error specifically (not a subclass of OSError)
#
# termios.error MRO: termios.error → Exception → BaseException → object.
# A bare `except OSError` does NOT catch it.  The _SERIAL_IO_ERRORS tuple
# includes termios.error explicitly so a raw USB unplug is caught even when
# pyserial doesn't wrap it in SerialException.
# ---------------------------------------------------------------------------


class TestTermiosErrorWrapping:
    """Regression: raw termios.error from tcflush/reset_input_buffer must be
    caught and translated, not propagate as an unhandled exception.
    """

    def test_icom_command_wraps_termios_error(self) -> None:
        """IcomCIVRig._command: termios.error → RigConnectionError."""
        import termios
        import threading
        from open_sstv.radio.exceptions import RigConnectionError

        r = IcomCIVRig.__new__(IcomCIVRig)
        r._port = "/dev/null"
        r._baud_rate = 19200
        r._addr = 0x94
        r._lock = threading.Lock()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = termios.error(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Icom CI-V serial I/O failed"):
            r._command(b"\x1c\x00\x00")

    def test_kenwood_command_wraps_termios_error(self) -> None:
        """KenwoodRig._command: termios.error → RigConnectionError."""
        import termios
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = termios.error(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Kenwood serial I/O failed"):
            r._command("FA")

    def test_kenwood_write_command_wraps_termios_error(self) -> None:
        """KenwoodRig._write_command: termios.error → RigConnectionError."""
        import termios
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import KenwoodRig

        r = _make_kenwood_rig()
        r._ser = MagicMock()
        r._ser.write.side_effect = termios.error(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Kenwood serial I/O failed"):
            r._write_command("TX1")

    def test_yaesu_command_wraps_termios_error(self) -> None:
        """YaesuRig._command: termios.error → RigConnectionError."""
        import termios
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r._ser.reset_input_buffer.side_effect = termios.error(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Yaesu serial I/O failed"):
            r._command("FA")

    def test_yaesu_write_command_wraps_termios_error(self) -> None:
        """YaesuRig._write_command: termios.error → RigConnectionError."""
        import termios
        from open_sstv.radio.exceptions import RigConnectionError
        from open_sstv.radio.serial_rig import YaesuRig

        r = _make_yaesu_rig()
        r._ser = MagicMock()
        r._ser.write.side_effect = termios.error(6, "Device not configured")

        with pytest.raises(RigConnectionError, match="Yaesu serial I/O failed"):
            r._write_command("TX0")

    def test_termios_error_is_not_oserror_subclass(self) -> None:
        """Documents the root cause: termios.error does NOT inherit from OSError.
        A bare `except OSError` would miss it — _SERIAL_IO_ERRORS includes it
        explicitly for exactly this reason.
        """
        import termios
        assert not issubclass(termios.error, OSError), (
            "If termios.error became an OSError subclass, the explicit entry in "
            "_SERIAL_IO_ERRORS is now redundant but harmless — update this test "
            "and the comment in serial_rig.py to reflect the new MRO."
        )
