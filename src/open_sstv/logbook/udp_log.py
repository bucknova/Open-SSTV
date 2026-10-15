# SPDX-License-Identifier: GPL-3.0-or-later
"""UDP QSO-log broadcast — ported from the ``cwrobot`` sister project.

Fires a single logged contact at a UDP listener so third-party ham-radio
logging tools (QLog, JTAlert, GridTracker, Log4OM, N1MM…) can pick it up
automatically, following the "UDP logging" convention WSJT-X popularized.
Two incompatible real-world wire formats exist, so both are supported:

- ``"adif"`` — a bare ADIF record string, one datagram, no framing.  What
  Log4OM-style listeners expect.
- ``"wsjtx"`` — WSJT-X's own binary Network Message protocol, message type
  12 ("Logged ADIF").  QLog, JTAlert, GridTracker, and N1MM all listen for
  this specifically; a real QLog instance does not recognize a bare ADIF
  datagram at all.  Field layout verified against WSJT-X's own
  ``Network/NetworkMessage.hpp``.

This module is intentionally UDP-only and has nothing to do with the
local SQLite logbook (``logbook.store``) — it is invoked from a dedicated
"UDP Log" button on the TX panel's QSO-state bar, once per QSO, not from
the per-image auto-capture flow.
"""
from __future__ import annotations

import socket
import struct
from typing import Literal

from open_sstv.logbook.adif import format_qso_record
from open_sstv.logbook.model import QSO, StationInfo

#: Magic number / schema / message type from WSJT-X's Network Message
#: protocol.  Type 12 is "Logged ADIF".
_MAGIC = 0xADBCCBDA
_SCHEMA = 3
_TYPE_LOGGED_ADIF = 12

#: Identifies us as the sender in the WSJT-X "Id" field of every message.
CLIENT_ID = "Open-SSTV"

#: Header written ahead of the single record for the "wsjtx" format's
#: embedded ADIF text — WSJT-X expects a complete little ADIF file
#: fragment (header + ``<EOH>`` + record), not a bare record.
#:
#: PROGRAMID's length is computed from CLIENT_ID (UTF-8 byte count, per
#: the ADIF ``<TAG:length>value`` spec) rather than hardcoded, so it can
#: never drift out of sync the way a literal ``:8>`` did against the
#: 9-byte "Open-SSTV" (a strict parser would then read "Open-SST" and
#: leave a stray "V" dangling before ``<EOH>``).
_ADIF_MINI_HEADER = (
    "Open-SSTV ADIF export\n"
    f"<ADIF_VER:5>3.1.5<PROGRAMID:{len(CLIENT_ID.encode('utf-8'))}>{CLIENT_ID}<EOH>\n"
)

UdpLogFormat = Literal["adif", "wsjtx"]


class QsoLoggingError(Exception):
    """Raised when a QSO could not be sent over UDP."""


def _utf8_field(text: str) -> bytes:
    """One WSJT-X ``QDataStream`` UTF-8 string field: length-prefixed bytes."""
    encoded = text.encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def build_logged_adif_message(adif_text: str, client_id: str = CLIENT_ID) -> bytes:
    """Build a WSJT-X Network Message "Logged ADIF" (type 12) datagram.

    Byte layout (all integers big-endian, matching Qt's ``QDataStream``
    default byte order)::

        [4 bytes] magic = 0xADBCCBDA
        [4 bytes] schema number = 3
        [4 bytes] message type = 12 (Logged ADIF)
        [utf8 field] Id — sender/client identifier
        [utf8 field] ADIF text — a complete ADIF file fragment
    """
    return (
        struct.pack(">III", _MAGIC, _SCHEMA, _TYPE_LOGGED_ADIF)
        + _utf8_field(client_id)
        + _utf8_field(adif_text)
    )


class UdpQsoLogger:
    """Sends one QSO as a single fire-and-forget UDP datagram.

    A fresh ``socket.SOCK_DGRAM`` is opened and closed on every call —
    cheap enough to build per QSO, and simpler than keeping a persistent
    socket around for something clicked at most a few times per contact.
    """

    def __init__(
        self, host: str, port: int, format: UdpLogFormat = "wsjtx"
    ) -> None:
        self._host = host
        self._port = port
        self._format: UdpLogFormat = format

    def log_qso(self, qso: QSO, station: StationInfo | None = None) -> None:
        record = format_qso_record(qso, station)
        if self._format == "wsjtx":
            data = build_logged_adif_message(_ADIF_MINI_HEADER + record + "\n")
        else:
            data = (record + "\n").encode("utf-8")
        # v0.6.14 audit: AF_INET-only with no SO_BROADCAST, so an IPv6
        # listener ("::1", a v6 LAN address) was unreachable and a
        # broadcast target (192.168.1.255, a common logger setup) failed
        # with EACCES.  Resolve through getaddrinfo and allow broadcast.
        # (socket.gaierror is an OSError.)
        try:
            infos = socket.getaddrinfo(
                self._host, self._port, type=socket.SOCK_DGRAM
            )
            family, _type, _proto, _canon, addr = infos[0]
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                if family == socket.AF_INET:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(data, addr)
        except OSError as exc:
            raise QsoLoggingError(
                f"Could not send to {self._host}:{self._port}: {exc}"
            ) from exc


__all__ = [
    "CLIENT_ID",
    "QsoLoggingError",
    "UdpLogFormat",
    "UdpQsoLogger",
    "build_logged_adif_message",
]
