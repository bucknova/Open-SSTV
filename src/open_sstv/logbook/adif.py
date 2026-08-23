# SPDX-License-Identifier: GPL-3.0-or-later
"""ADIF 3.1.5 reader/writer for the logbook.

ADIF (Amateur Data Interchange Format) is the universal exchange
format for ham radio contact logs.  Spec: https://www.adif.org/315/

Format basics
─────────────
Each field is encoded as ``<TAG:length[:type]>value`` where ``length``
is the **byte** count of ``value`` (matters for non-ASCII).  Records
end with ``<EOR>``.  An optional header section ends with ``<EOH>``.
Tags are case-insensitive.

What we support
───────────────
- **Export**: CALL, QSO_DATE, TIME_ON, MODE=SSTV, SUBMODE (compact
  SSTV mode form like ``MartinM1``), BAND (derived from FREQ),
  FREQ (MHz), RST_SENT, RST_RCVD, NAME, QTH, GRIDSQUARE, COMMENT,
  STATION_CALLSIGN, MY_GRIDSQUARE, MY_CITY, OPERATOR, MY_NAME.  Plus our own
  ``APP_OPENSSTV_DIRECTION`` for RX-only entries (default TX/two-way
  is implicit).
- **Import**: same fields, lenient about whitespace, line endings,
  case, and submode formatting (accepts ``Martin M1``, ``MartinM1``,
  ``MARTINM1``).  Unknown tags ignored silently.

What we don't support
─────────────────────
- ADIF data types (``<FOO:5:N>...``) — type code parsed and discarded.
- ADX (XML variant of ADIF) — not in scope for v0.4.0.
- Multi-byte length fields beyond what stdlib int parsing handles.

Out-of-scope ADIF fields (LoTW upload, eQSL, awards) come back when
those upload integrations land in v0.4.x patches.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, time
from pathlib import Path

from open_sstv.logbook.model import DIRECTIONS, QSO, StationInfo

_log = logging.getLogger(__name__)

#: Bumped when the ADIF writer's output shape changes in a way that
#: tools need to know about.  Reported as PROGRAMVERSION in headers.
PROGRAM_ID = "Open-SSTV"


class AdifParseError(ValueError):
    """Raised on unrecoverable ADIF parsing errors."""


# ---------------------------------------------------------------------------
# Mode <-> SUBMODE mapping
# ---------------------------------------------------------------------------
#
# ADIF MODE for SSTV is always "SSTV"; the specific SSTV mode goes in
# SUBMODE.  There's no official SSTV submode list in the spec, but
# MMSSTV established the de-facto convention of compact spaces-removed
# names (MartinM1, Scottie1, PD120).  We export that form and accept
# any reasonable variant on import (case-insensitive, optional spaces).

def mode_to_submode(mode: str) -> str:
    """Convert a QSO ``mode`` field to ADIF SUBMODE form (no spaces)."""
    return mode.replace(" ", "").replace("-", "")


def submode_to_mode(submode: str, *, table: Iterable[str] = ()) -> str:
    """Convert an ADIF SUBMODE back to our canonical mode string.

    Looks up against the optional ``table`` of known mode names; if any
    matches case-insensitively with spaces collapsed, returns the
    canonical form.  Otherwise returns the SUBMODE verbatim (which the
    UI can display unchanged).
    """
    target = mode_to_submode(submode).upper()
    for canon in table:
        if mode_to_submode(canon).upper() == target:
            return canon
    return submode


# ---------------------------------------------------------------------------
# Band derivation from frequency
# ---------------------------------------------------------------------------
#
# Standard ham bands.  SSTV is realistically HF (160m–10m) plus the
# occasional 6m, 2m, 70cm contact.  Adding higher bands is cheap and
# future-proofs us for the rare VHF/UHF SSTV op.

_BAND_TABLE: list[tuple[float, float, str]] = [
    # (lo_mhz, hi_mhz, band_name) — ranges per ADIF Band Enumeration
    (0.1357, 0.1378, "2190m"),
    (0.472, 0.479, "630m"),
    (1.8, 2.0, "160m"),
    (3.5, 4.0, "80m"),
    (5.06, 5.45, "60m"),
    (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"),
    (14.0, 14.35, "20m"),
    (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"),
    (24.89, 24.99, "12m"),
    (28.0, 29.7, "10m"),
    (50.0, 54.0, "6m"),
    (70.0, 70.5, "4m"),
    (144.0, 148.0, "2m"),
    (219.0, 225.0, "1.25m"),
    (420.0, 450.0, "70cm"),
    (902.0, 928.0, "33cm"),
    (1240.0, 1300.0, "23cm"),
    (2300.0, 2450.0, "13cm"),
    (3300.0, 3500.0, "9cm"),
    (5650.0, 5925.0, "6cm"),
    (10000.0, 10500.0, "3cm"),
    (24000.0, 24250.0, "1.25cm"),
]


def hz_to_band(freq_hz: int | None) -> str | None:
    """Map frequency (Hz) to ADIF Band Enumeration string ("20m", "40m")."""
    if freq_hz is None or freq_hz <= 0:
        return None
    mhz = freq_hz / 1_000_000.0
    for lo, hi, name in _BAND_TABLE:
        if lo <= mhz <= hi:
            return name
    return None


def hz_to_mhz_str(freq_hz: int | None) -> str | None:
    """Format frequency Hz as a fixed-6-decimal MHz string ("14.230000")."""
    if freq_hz is None or freq_hz <= 0:
        return None
    return f"{freq_hz / 1_000_000.0:.6f}"


def mhz_str_to_hz(mhz_str: str) -> int | None:
    """Parse an ADIF FREQ field (MHz) into integer Hz."""
    try:
        return int(round(float(mhz_str) * 1_000_000))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Date / time formatters
# ---------------------------------------------------------------------------

def datetime_to_qso_date(dt: datetime) -> str:
    """Format the date part as ADIF ``QSO_DATE`` (YYYYMMDD, UTC)."""
    return dt.astimezone(UTC).strftime("%Y%m%d")


def datetime_to_time_on(dt: datetime) -> str:
    """Format the time part as ADIF ``TIME_ON`` (HHMMSS, UTC)."""
    return dt.astimezone(UTC).strftime("%H%M%S")


def parse_qso_date_time(qso_date: str, time_on: str) -> datetime:
    """Parse ADIF QSO_DATE + TIME_ON into a UTC datetime.

    ``time_on`` may be HHMM (4 chars) or HHMMSS (6 chars).
    """
    qso_date = qso_date.strip()
    time_on = time_on.strip()
    if len(qso_date) != 8 or not qso_date.isdigit():
        raise AdifParseError(f"invalid QSO_DATE {qso_date!r}; expected YYYYMMDD")
    year = int(qso_date[:4])
    month = int(qso_date[4:6])
    day = int(qso_date[6:8])
    if len(time_on) == 4 and time_on.isdigit():
        hour = int(time_on[:2])
        minute = int(time_on[2:4])
        second = 0
    elif len(time_on) == 6 and time_on.isdigit():
        hour = int(time_on[:2])
        minute = int(time_on[2:4])
        second = int(time_on[4:6])
    else:
        raise AdifParseError(f"invalid TIME_ON {time_on!r}; expected HHMM or HHMMSS")
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        # Digit-valid but out-of-range (Feb 31, hour 25, month 13, …).
        # Must be AdifParseError so _build_qso skips just this record —
        # a bare ValueError would abort the whole import (audit #1).
        raise AdifParseError(
            f"out-of-range QSO_DATE/TIME_ON {qso_date!r} {time_on!r}: {exc}"
        ) from exc


def datetime_to_created_timestamp(dt: datetime) -> str:
    """ADIF CREATED_TIMESTAMP format: ``YYYYMMDD HHMMSS``."""
    s = dt.astimezone(UTC).strftime("%Y%m%d %H%M%S")
    return s


# ---------------------------------------------------------------------------
# Field writer
# ---------------------------------------------------------------------------

def _adif_field(tag: str, value: str | None) -> str:
    """Render a single ADIF ``<TAG:length>value`` field.

    Returns the empty string if ``value`` is None or empty — caller is
    responsible for omitting required fields, not us.  Length is BYTE
    count of UTF-8 encoded value, per spec.
    """
    if value is None or value == "":
        return ""
    encoded = value.encode("utf-8")
    return f"<{tag.upper()}:{len(encoded)}>{value}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def format_qso_record(qso: QSO, station: StationInfo | None = None) -> str:
    """Render one QSO as a single ADIF record line (no header, no ``<EOH>``).

    Shared by ``export_adif`` (one call per QSO) and the UDP QSO-log
    sender (``logbook.udp_log``), which needs the exact same field set
    for a single contact without the multi-record document wrapper.
    """
    station = station or StationInfo()
    record_parts: list[str] = []
    # Contact identity — required.
    record_parts.append(_adif_field("CALL", qso.callsign.strip().upper()))
    record_parts.append(_adif_field("QSO_DATE", datetime_to_qso_date(qso.time_utc)))
    record_parts.append(_adif_field("TIME_ON", datetime_to_time_on(qso.time_utc)))
    # Mode is always SSTV for us; submode carries the specific mode.
    record_parts.append(_adif_field("MODE", "SSTV"))
    if qso.mode:
        record_parts.append(_adif_field("SUBMODE", mode_to_submode(qso.mode)))
    # Band + frequency.
    band = hz_to_band(qso.frequency_hz)
    if band:
        record_parts.append(_adif_field("BAND", band))
    mhz = hz_to_mhz_str(qso.frequency_hz)
    if mhz:
        record_parts.append(_adif_field("FREQ", mhz))
    # Signal reports.
    record_parts.append(_adif_field("RST_SENT", qso.rsv_sent))
    record_parts.append(_adif_field("RST_RCVD", qso.rsv_received))
    # Contact metadata.
    record_parts.append(_adif_field("NAME", qso.name))
    record_parts.append(_adif_field("QTH", qso.qth))
    record_parts.append(_adif_field("GRIDSQUARE", qso.grid))
    record_parts.append(_adif_field("COMMENT", qso.comment))
    # Operator's identity.
    record_parts.append(_adif_field("STATION_CALLSIGN", station.callsign))
    record_parts.append(_adif_field("MY_GRIDSQUARE", station.grid))
    record_parts.append(_adif_field("MY_CITY", station.qth))
    record_parts.append(_adif_field("OPERATOR", station.callsign))
    record_parts.append(_adif_field("MY_NAME", station.name))
    # Our extension: RX direction (TX/two-way is the implicit default).
    if qso.direction == "RX":
        record_parts.append(_adif_field("APP_OPENSSTV_DIRECTION", "RX"))
    # Filter out blanks and join with spaces (one record per line).
    line = " ".join(p for p in record_parts if p)
    return f"{line} <EOR>"


def export_adif(
    qsos: Iterable[QSO],
    *,
    station: StationInfo | None = None,
    program_version: str = "",
    include_drafts: bool = False,
) -> str:
    """Render an iterable of QSOs as an ADIF 3.1.5 text document.

    - ``station``: operator's own identity for STATION_CALLSIGN /
      MY_GRIDSQUARE / MY_CITY / OPERATOR fields.  Omit and only
      contact-side fields will be written.
    - ``program_version``: emitted as PROGRAMVERSION in the header.
      Caller should pass the running Open-SSTV version.
    - ``include_drafts``: by default, QSOs with empty callsign are
      skipped (they're not real contacts yet).  Set True to dump
      everything (e.g. for a complete backup).
    """
    station = station or StationInfo()
    now = datetime.now(UTC)

    lines: list[str] = [
        "Open-SSTV logbook export",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "",
        _adif_field("ADIF_VER", "3.1.5"),
        _adif_field("PROGRAMID", PROGRAM_ID),
    ]
    if program_version:
        lines.append(_adif_field("PROGRAMVERSION", program_version))
    lines.append(_adif_field("CREATED_TIMESTAMP", datetime_to_created_timestamp(now)))
    lines.append("<EOH>")
    lines.append("")

    for qso in qsos:
        if not include_drafts and not qso.callsign.strip():
            continue
        lines.append(format_qso_record(qso, station))

    lines.append("")  # trailing newline
    return "\n".join(lines)


def write_adif_file(
    path: Path,
    qsos: Iterable[QSO],
    *,
    station: StationInfo | None = None,
    program_version: str = "",
    include_drafts: bool = False,
) -> None:
    """Write QSOs as ADIF to ``path``.  UTF-8 encoded, LF line endings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = export_adif(
        qsos,
        station=station,
        program_version=program_version,
        include_drafts=include_drafts,
    )
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(rb"<([A-Za-z_][A-Za-z0-9_]*)((?::-?\d+)?(?::[A-Za-z])?)>")


def _tokenize(data: bytes) -> Iterator[tuple[str, str]]:
    """Yield ``(TAG_UPPER, value_str)`` tuples from raw ADIF bytes.

    Sentinel tags ``EOH`` and ``EOR`` yield with empty value.  Tag
    names are uppercased.  Values are decoded as UTF-8 with replacement
    on invalid sequences — a malformed byte should not abort the whole
    import.
    """
    i = 0
    n = len(data)
    while i < n:
        lt = data.find(b"<", i)
        if lt < 0:
            return
        gt = data.find(b">", lt + 1)
        if gt < 0:
            return  # malformed; stop cleanly
        block = data[lt + 1:gt].decode("ascii", errors="replace")
        parts = block.split(":")
        tag = parts[0].strip().upper()
        if tag in ("EOH", "EOR"):
            yield (tag, "")
            i = gt + 1
            continue
        if len(parts) < 2:
            # Tag with no length, e.g. malformed `<FOO>`; skip.
            i = gt + 1
            continue
        try:
            length = int(parts[1])
        except ValueError:
            i = gt + 1
            continue
        if length < 0:
            i = gt + 1
            continue
        value_bytes = data[gt + 1:gt + 1 + length]
        value = value_bytes.decode("utf-8", errors="replace")
        yield (tag, value)
        i = gt + 1 + length


def _parse_rsv(s: str) -> str:
    """Pass through; we store whatever the source logger wrote."""
    return s.strip()


def _build_qso(record: dict[str, str], *, mode_table: Iterable[str] = ()) -> QSO | None:
    """Build a QSO from one record's tag → value dict.  Returns None if
    the record lacks the minimum (CALL, QSO_DATE, TIME_ON)."""
    call = record.get("CALL", "").strip().upper()
    qso_date = record.get("QSO_DATE", "").strip()
    time_on = record.get("TIME_ON", "").strip()
    if not call or not qso_date or not time_on:
        return None
    try:
        time_utc = parse_qso_date_time(qso_date, time_on)
    except AdifParseError as e:
        _log.warning("skipping ADIF record with bad date/time: %s", e)
        return None

    # Mode resolution: prefer SUBMODE (specific SSTV mode), fall back
    # to MODE (which will likely just be "SSTV").
    submode = record.get("SUBMODE", "").strip()
    mode_str = record.get("MODE", "").strip()
    if submode:
        mode = submode_to_mode(submode, table=mode_table)
    else:
        mode = mode_str  # "SSTV" or whatever the source put

    # Frequency: prefer FREQ (MHz), ignore BAND for now (band is
    # derivable and rarely more precise than FREQ).
    freq_str = record.get("FREQ", "").strip()
    freq_hz = mhz_str_to_hz(freq_str) if freq_str else None

    direction_raw = record.get("APP_OPENSSTV_DIRECTION", "TX").strip().upper()
    direction = direction_raw if direction_raw in DIRECTIONS else "TX"

    return QSO(
        direction=direction,  # type: ignore[arg-type]
        callsign=call,
        time_utc=time_utc,
        mode=mode,
        frequency_hz=freq_hz,
        rsv_sent=_parse_rsv(record.get("RST_SENT", "")),
        rsv_received=_parse_rsv(record.get("RST_RCVD", "")),
        name=record.get("NAME", "").strip(),
        qth=record.get("QTH", "").strip(),
        grid=record.get("GRIDSQUARE", "").strip(),
        comment=record.get("COMMENT", "").strip(),
    )


def import_adif(
    text: str | bytes,
    *,
    mode_table: Iterable[str] = (),
) -> list[QSO]:
    """Parse an ADIF document into a list of QSOs.

    - ``text`` may be ``str`` or ``bytes``; ``str`` is encoded to UTF-8
      first.
    - ``mode_table``: optional iterable of known canonical mode names
      to normalize SUBMODE values against (e.g. ``["Martin M1",
      "Scottie 1", ...]`` so ``MartinM1`` becomes ``Martin M1``).
      Pass ``[m.value for m in Mode]`` from ``core.modes`` at the
      call site to keep this module free of the dependency.

    Lenient: malformed records are skipped (with a warning), valid
    records returned.  Header section (everything before ``<EOH>``)
    is discarded; if no ``<EOH>`` is present the whole document is
    treated as records, matching most real-world ADIF emitters.
    """
    if isinstance(text, str):
        data = text.encode("utf-8")
    else:
        data = text

    # Skip past the header if present.
    eoh_idx = data.upper().find(b"<EOH>")
    if eoh_idx >= 0:
        data = data[eoh_idx + len(b"<EOH>"):]

    qsos: list[QSO] = []
    current: dict[str, str] = {}
    for tag, value in _tokenize(data):
        if tag == "EOR":
            qso = _build_qso(current, mode_table=mode_table)
            if qso is not None:
                qsos.append(qso)
            current = {}
            continue
        if tag == "EOH":
            # Stray EOH inside records — reset and continue.
            current = {}
            continue
        current[tag] = value
    # Trailing record without <EOR> — try to build it anyway.
    if current:
        qso = _build_qso(current, mode_table=mode_table)
        if qso is not None:
            qsos.append(qso)
    return qsos


def read_adif_file(
    path: Path,
    *,
    mode_table: Iterable[str] = (),
) -> list[QSO]:
    """Read an ADIF file from ``path``.  Bytes-in to preserve length counts."""
    return import_adif(Path(path).read_bytes(), mode_table=mode_table)


# Convenience for callers that pass through a time object without a date.
def _combine_today(t: time) -> datetime:
    """Combine a bare time with today's UTC date (used internally only)."""
    today = datetime.now(UTC).date()
    return datetime.combine(today, t, tzinfo=UTC)
