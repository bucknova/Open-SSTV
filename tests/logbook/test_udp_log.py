# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the UDP QSO-log broadcast (ported from cwrobot)."""
from __future__ import annotations

import socket
import struct
from datetime import UTC, datetime

import pytest

from open_sstv.logbook.model import QSO, StationInfo
from open_sstv.logbook.udp_log import (
    QsoLoggingError,
    UdpQsoLogger,
    build_logged_adif_message,
)

_MAGIC = 0xADBCCBDA
_SCHEMA = 3
_TYPE_LOGGED_ADIF = 12


def _qso() -> QSO:
    return QSO(
        direction="TX",
        callsign="W0AEZ",
        time_utc=datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC),
        mode="SSTV",
        frequency_hz=14_230_000,
        rsv_sent="595",
        rsv_received="589",
        name="Alex",
        qth="Springfield",
        grid="EN34",
    )


def _station() -> StationInfo:
    return StationInfo(callsign="N0CALL", grid="EM12", qth="Home", name="Op")


def _read_utf8_field(data: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from(">I", data, offset)
    offset += 4
    text = data[offset : offset + length].decode("utf-8")
    return text, offset + length


# ---------------------------------------------------------------------------
# build_logged_adif_message — WSJT-X Network Message framing
# ---------------------------------------------------------------------------


class TestBuildLoggedAdifMessage:
    def test_header_fields(self) -> None:
        data = build_logged_adif_message("<CALL:5>W0AEZ <EOR>", client_id="Open-SSTV")
        magic, schema, msg_type = struct.unpack_from(">III", data, 0)
        assert magic == _MAGIC
        assert schema == _SCHEMA
        assert msg_type == _TYPE_LOGGED_ADIF

    def test_id_and_adif_text_round_trip(self) -> None:
        adif_text = "<CALL:5>W0AEZ <QSO_DATE:8>20260528 <EOR>"
        data = build_logged_adif_message(adif_text, client_id="Open-SSTV")
        client_id, offset = _read_utf8_field(data, 12)
        text, offset = _read_utf8_field(data, offset)
        assert client_id == "Open-SSTV"
        assert text == adif_text
        assert offset == len(data)

    def test_non_ascii_field_length_is_byte_count(self) -> None:
        # "é" is 2 UTF-8 bytes — the length prefix must be bytes, not
        # characters, or a non-ASCII operator name would desync the stream.
        data = build_logged_adif_message("café", client_id="x")
        _, offset = _read_utf8_field(data, 12)
        text, offset = _read_utf8_field(data, offset)
        assert text == "café"
        assert offset == len(data)


# ---------------------------------------------------------------------------
# UdpQsoLogger — actual UDP send, both wire formats
# ---------------------------------------------------------------------------


@pytest.fixture
def udp_listener() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    try:
        yield sock
    finally:
        sock.close()


class TestUdpQsoLogger:
    def test_adif_format_sends_bare_record(self, udp_listener: socket.socket) -> None:
        port = udp_listener.getsockname()[1]
        logger = UdpQsoLogger("127.0.0.1", port, format="adif")
        logger.log_qso(_qso(), _station())

        data, _addr = udp_listener.recvfrom(65536)
        text = data.decode("utf-8")
        assert text.startswith("<CALL:5>W0AEZ")
        assert "<MODE:4>SSTV" in text
        assert "<RST_SENT:3>595" in text
        assert "<RST_RCVD:3>589" in text
        assert "<STATION_CALLSIGN:6>N0CALL" in text
        # Frequency (from _qso()'s frequency_hz=14_230_000, the same value
        # MainWindow reads from the "Freq:" field on the main page) must
        # ride along as both BAND and FREQ.
        assert "<BAND:3>20m" in text
        assert "<FREQ:9>14.230000" in text
        assert "<EOR>" in text
        # No WSJT-X framing bytes in front of a bare-ADIF datagram.
        assert not text.startswith("\xad\xbc\xcb\xda")

    def test_wsjtx_format_sends_framed_message(self, udp_listener: socket.socket) -> None:
        port = udp_listener.getsockname()[1]
        logger = UdpQsoLogger("127.0.0.1", port, format="wsjtx")
        logger.log_qso(_qso(), _station())

        data, _addr = udp_listener.recvfrom(65536)
        magic, schema, msg_type = struct.unpack_from(">III", data, 0)
        assert magic == _MAGIC
        assert schema == _SCHEMA
        assert msg_type == _TYPE_LOGGED_ADIF
        client_id, offset = _read_utf8_field(data, 12)
        adif_text, offset = _read_utf8_field(data, offset)
        assert client_id == "Open-SSTV"
        assert "<EOH>" in adif_text  # mini-file header, not a bare record
        assert "<CALL:5>W0AEZ" in adif_text
        assert "<FREQ:9>14.230000" in adif_text
        assert offset == len(data)

    def test_mini_header_programid_length_matches_client_id(
        self, udp_listener: socket.socket
    ) -> None:
        # Regression: PROGRAMID's length prefix must be "Open-SSTV"'s
        # actual UTF-8 byte count (9), not a stale hardcoded value — a
        # wrong length makes a strict parser read a truncated/garbled
        # id and leave stray bytes dangling before <EOH>.
        port = udp_listener.getsockname()[1]
        logger = UdpQsoLogger("127.0.0.1", port, format="wsjtx")
        logger.log_qso(_qso())

        data, _addr = udp_listener.recvfrom(65536)
        _client_id, offset = _read_utf8_field(data, 12)
        adif_text, _offset = _read_utf8_field(data, offset)
        assert "<PROGRAMID:9>Open-SSTV<EOH>" in adif_text

    def test_no_frequency_omits_band_and_freq(self, udp_listener: socket.socket) -> None:
        # No rig connected → frequency_hz=None (mirrors "Freq:" showing
        # "—" on the main page) — BAND/FREQ must simply be absent, not
        # rendered as "0.000000" or similar.
        port = udp_listener.getsockname()[1]
        logger = UdpQsoLogger("127.0.0.1", port, format="adif")
        no_freq_qso = _qso()
        no_freq_qso.frequency_hz = None
        logger.log_qso(no_freq_qso)

        data, _addr = udp_listener.recvfrom(65536)
        text = data.decode("utf-8")
        assert "<BAND:" not in text
        assert "<FREQ:" not in text

    def test_send_failure_raises_qso_logging_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare OSError from sendto() (unreachable host, no route, a
        # firewall rejection, ...) must come out as a normalised
        # QsoLoggingError, not the raw OSError. Forced via monkeypatch
        # rather than an actually-invalid destination (e.g. port 0)
        # since real OS network stacks disagree on whether/when that
        # fails synchronously — Windows' Winsock, unlike Linux, does
        # not reject a sendto() to port 0.
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise OSError("network is unreachable")

        monkeypatch.setattr(socket.socket, "sendto", _raise)
        logger = UdpQsoLogger("127.0.0.1", 2237, format="adif")
        with pytest.raises(QsoLoggingError):
            logger.log_qso(_qso())

    def test_station_defaults_to_empty_when_omitted(
        self, udp_listener: socket.socket
    ) -> None:
        port = udp_listener.getsockname()[1]
        logger = UdpQsoLogger("127.0.0.1", port, format="adif")
        logger.log_qso(_qso())  # no station passed

        data, _addr = udp_listener.recvfrom(65536)
        text = data.decode("utf-8")
        assert "<CALL:5>W0AEZ" in text
        assert "STATION_CALLSIGN" not in text
