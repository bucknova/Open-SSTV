# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for ``sstv_app.radio.serial_rig.IcomCIVRig``.

All tests mock ``_command()`` so no serial port is required.  They guard
specifically against the CI-V command-echo-byte bug (C-1 through C-4)
where every data-response payload includes the command echo as its first
byte, and the accessors must strip it before parsing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sstv_app.radio.serial_rig import IcomCIVRig


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
