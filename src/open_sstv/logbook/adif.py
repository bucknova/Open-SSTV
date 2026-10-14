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
- **Export**: CALL, QSO_DATE, TIME_ON, MODE (``SSTV`` plus a compact
  SUBMODE like ``MartinM1`` for SSTV modes; the real mode otherwise),
  BAND (derived from FREQ, else the stored band), FREQ (MHz), RST_SENT,
  RST_RCVD, NAME, QTH, GRIDSQUARE, COMMENT,
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

import functools
import logging
import math
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, time
from pathlib import Path

from open_sstv.bands import BAND_NAMES
from open_sstv.bands import hz_to_band as _shared_hz_to_band
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
# SUBMODE.  ADIF 3.1's Mode enumeration lists SSTV with *no* submodes, so
# there's no official list, but MMSSTV established the de-facto
# convention of compact spaces-removed names (MartinM1, Scottie1, PD120)
# and Open-SSTV has always exported that form — kept for compatibility
# with existing logs.  We accept any reasonable variant on import
# (case-insensitive, optional spaces).
#
# v0.6.14 audit C-M8: the writer hard-coded MODE=SSTV and put *any* mode in
# SUBMODE, so an imported FT8 contact went out as MODE=SSTV SUBMODE=FT8
# and LoTW / Club Log rejected or mis-filed it.  SUBMODE is now written
# only for recognised SSTV modes; anything else goes out under its own
# MODE (with the ADIF parent MODE where the enumeration nests it).

#: SSTV mode names other loggers (MMSSTV, QSSTV, Log4OM) write that
#: Open-SSTV doesn't transmit, so imported rows still export as SSTV.
_EXTRA_SSTV_MODE_NAMES: tuple[str, ...] = (
    "Robot 8", "Robot 12", "Robot 24", "Robot 72",
    "Scottie DX2", "Martin HQ1", "Martin HQ2",
    "SC2-60", "Wraase SC2-30", "Wraase SC2-60",
    "PD 50", "AVT 90", "AVT 94", "FAX480",
    "MP73", "MP115", "MP140", "MP175", "MR73", "MR90", "MR115", "MR140",
    "MR175", "ML180", "ML240", "ML280", "ML320",
    "BW 8", "BW 12", "B/W 8", "B/W 12",
)

#: ADIF 3.1 Submode enumeration, grouped by parent MODE.  An imported
#: record keeps only its SUBMODE as ``mode``, so export needs the full map
#: to restore the parent — a partial list sent e.g. PSK250 out as
#: MODE=PSK250, which isn't in the Mode enumeration.
_ADIF_SUBMODES: dict[str, tuple[str, ...]] = {
    "CHIP": ("CHIP64", "CHIP128"),
    "CW": ("PCW",),
    "DIGITALVOICE": ("C4FM", "DMR", "DSTAR", "FREEDV", "M17"),
    "DOMINO": (
        "DOM-M", "DOM4", "DOM5", "DOM8", "DOM11", "DOM16", "DOM22", "DOM44",
        "DOM88", "DOMINOEX", "DOMINOF",
    ),
    "DYNAMIC": ("FREEDATA", "VARA HF", "VARA SATELLITE", "VARA FM 1200", "VARA FM 9600"),
    "FSK": ("SCAMP_FAST", "SCAMP_SLOW", "SCAMP_VSLOW"),
    "HELL": (
        "FMHELL", "FSKH105", "FSKH245", "FSKHELL", "HELL80", "HELLX5", "HELLX9",
        "HFSK", "PSKHELL", "SLOWHELL",
    ),
    "ISCAT": ("ISCAT-A", "ISCAT-B"),
    "JT4": ("JT4A", "JT4B", "JT4C", "JT4D", "JT4E", "JT4F", "JT4G"),
    "JT9": (
        "JT9-1", "JT9-2", "JT9-5", "JT9-10", "JT9-30", "JT9A", "JT9B", "JT9C",
        "JT9D", "JT9E", "JT9E FAST", "JT9F", "JT9F FAST", "JT9G", "JT9G FAST",
        "JT9H", "JT9H FAST",
    ),
    "JT65": ("JT65A", "JT65B", "JT65B2", "JT65C", "JT65C2"),
    "MFSK": (
        "FSQCALL", "FST4", "FST4W", "FT4", "JS8", "JTMS", "MFSK4", "MFSK8",
        "MFSK11", "MFSK16", "MFSK22", "MFSK31", "MFSK32", "MFSK64", "MFSK64L",
        "MFSK128", "MFSK128L", "Q65",
    ),
    "OLIVIA": (
        "OLIVIA 4/125", "OLIVIA 4/250", "OLIVIA 8/250", "OLIVIA 8/500",
        "OLIVIA 16/500", "OLIVIA 16/1000", "OLIVIA 32/1000",
    ),
    "OPERA": ("OPERA-BEACON", "OPERA-QSO"),
    "PAC": ("PAC2", "PAC3", "PAC4"),
    "PAX": ("PAX2",),
    "PSK": (
        "8PSK125", "8PSK125F", "8PSK125FL", "8PSK250", "8PSK250F", "8PSK250FL",
        "8PSK500", "8PSK500F", "8PSK1000", "8PSK1000F", "8PSK1200F", "FSK31",
        "PSK10", "PSK31", "PSK63", "PSK63F", "PSK63RC4", "PSK63RC5",
        "PSK63RC10", "PSK63RC20", "PSK63RC32", "PSK125", "PSK125C12",
        "PSK125R", "PSK125RC10", "PSK125RC12", "PSK125RC16", "PSK125RC4",
        "PSK125RC5", "PSK250", "PSK250C6", "PSK250R", "PSK250RC2",
        "PSK250RC3", "PSK250RC5", "PSK250RC6", "PSK250RC7", "PSK500",
        "PSK500C2", "PSK500C4", "PSK500R", "PSK500RC2", "PSK500RC3",
        "PSK500RC4", "PSK800C2", "PSK800RC2", "PSK1000", "PSK1000C2",
        "PSK1000R", "PSK1000RC2", "PSKAM10", "PSKAM31", "PSKAM50", "PSKFEC31",
        "QPSK31", "QPSK63", "QPSK125", "QPSK250", "QPSK500", "SIM31",
    ),
    "QRA64": ("QRA64A", "QRA64B", "QRA64C", "QRA64D", "QRA64E"),
    "ROS": ("ROS-EME", "ROS-HF", "ROS-MF"),
    "RTTY": ("ASCI",),
    "SSB": ("LSB", "USB"),
    "THOR": (
        "THOR-M", "THOR4", "THOR5", "THOR8", "THOR11", "THOR16", "THOR22",
        "THOR25X4", "THOR50X1", "THOR50X2", "THOR100",
    ),
    "THRB": ("THRBX", "THRBX1", "THRBX2", "THRBX4", "THROB1", "THROB2", "THROB4"),
    "TOR": ("AMTORFEC", "GTOR", "NAVTEX", "SITORB"),
}

_SUBMODE_PARENT: dict[str, str] = {
    sub: parent for parent, subs in _ADIF_SUBMODES.items() for sub in subs
}


@functools.lru_cache(maxsize=1)
def _sstv_submode_keys() -> frozenset[str]:
    """Compact-uppercase forms of every recognised SSTV mode name."""
    from open_sstv.logbook.coordinator import MODE_DISPLAY_NAMES  # noqa: PLC0415

    names = (*MODE_DISPLAY_NAMES.values(), *MODE_DISPLAY_NAMES.keys(),
             *_EXTRA_SSTV_MODE_NAMES)
    return frozenset(mode_to_submode(n).upper().replace("_", "") for n in names)


def is_sstv_mode(mode: str) -> bool:
    """True when *mode* names a specific SSTV mode ("Martin M1", "PD120")."""
    key = mode_to_submode(mode).upper().replace("_", "")
    return bool(key) and key in _sstv_submode_keys()


def adif_mode_fields(mode: str) -> tuple[str, str]:
    """``(MODE, SUBMODE)`` to export for a stored mode string.

    - empty / "SSTV" → ``("SSTV", "")`` (our own contacts are SSTV)
    - a recognised SSTV mode → ``("SSTV", "MartinM1")``
    - anything else → its own MODE (``"FT8"``), or the ADIF parent with
      the submode (``FT4`` → ``("MFSK", "FT4")``).
    """
    mode = (mode or "").strip()
    if not mode or mode.upper() == "SSTV":
        return ("SSTV", "")
    if is_sstv_mode(mode):
        return ("SSTV", mode_to_submode(mode))
    upper = mode.upper()
    parent = _SUBMODE_PARENT.get(upper)
    if parent is not None and parent != upper:
        return (parent, upper)
    return (upper, "")

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
# The table lives in ``open_sstv.bands`` (shared with the template
# ``%b`` token since the v0.6.14 audit); this is the ADIF-facing wrapper.


def hz_to_band(freq_hz: int | None) -> str | None:
    """Map frequency (Hz) to ADIF Band Enumeration string ("20m", "40m")."""
    return _shared_hz_to_band(freq_hz)


def hz_to_mhz_str(freq_hz: int | None) -> str | None:
    """Format frequency Hz as a fixed-6-decimal MHz string ("14.230000")."""
    if freq_hz is None or freq_hz <= 0:
        return None
    return f"{freq_hz / 1_000_000.0:.6f}"


#: Upper bound for a plausible FREQ (300 GHz — the top of the radio
#: spectrum).  Anything past it is junk, not a contact.
_MAX_FREQ_HZ = 300_000_000_000


def mhz_str_to_hz(mhz_str: str) -> int | None:
    """Parse an ADIF FREQ field (MHz) into integer Hz, or ``None``.

    v0.6.14 audit S-L8: ``FREQ=inf`` raised ``OverflowError`` from
    ``int()`` (not caught) and aborted the whole import; ``1e300`` and
    negatives were accepted.  Non-finite and out-of-range (not
    0 < Hz < 300 GHz) values now parse as "no frequency".
    """
    try:
        mhz = float(mhz_str)
        if not math.isfinite(mhz):
            return None
        hz = int(round(mhz * 1_000_000))
    except (ValueError, TypeError, OverflowError):
        return None
    if not 0 < hz < _MAX_FREQ_HZ:
        return None
    return hz


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
    # SSTV contacts: MODE=SSTV + SUBMODE; anything else (imported FT8,
    # SSB, …) under its own MODE — v0.6.14 audit C-M8.
    adif_mode, adif_submode = adif_mode_fields(qso.mode)
    record_parts.append(_adif_field("MODE", adif_mode))
    record_parts.append(_adif_field("SUBMODE", adif_submode))
    # Band + frequency.  BAND derives from FREQ when there is one, else
    # it's the band stored from an import that had BAND but no FREQ.
    band = hz_to_band(qso.frequency_hz) or (qso.band or None)
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

    # Frequency: FREQ (MHz) when present and sane.  BAND is kept too
    # (v0.6.14 audit C-M8: it used to be dropped, so a FREQ-less record
    # lost its band on round-trip); a band derived from FREQ wins.
    freq_str = record.get("FREQ", "").strip()
    freq_hz = mhz_str_to_hz(freq_str) if freq_str else None
    band_raw = record.get("BAND", "").strip().lower()
    band = hz_to_band(freq_hz) or (band_raw if band_raw in BAND_NAMES else "")

    direction_raw = record.get("APP_OPENSSTV_DIRECTION", "TX").strip().upper()
    direction = direction_raw if direction_raw in DIRECTIONS else "TX"

    return QSO(
        direction=direction,  # type: ignore[arg-type]
        callsign=call,
        time_utc=time_utc,
        mode=mode,
        frequency_hz=freq_hz,
        band=band,
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
    # ADIF spec: "If the first character in an ADI file is <, it contains
    # no header."  Look for <EOH> only when there *is* a header.  Searching
    # unconditionally meant that a header-less file with "<EOH>" in a field
    # value (a COMMENT, say) had every QSO before it silently dropped.
    # (2026-10 stability audit.)
    if not data.lstrip().startswith(b"<"):
        eoh_idx = data.upper().find(b"<EOH>")
        if eoh_idx >= 0:
            data = data[eoh_idx + len(b"<EOH>"):]

    qsos: list[QSO] = []

    def _add(record: dict[str, str]) -> None:
        # v0.6.14 audit S-L8: one malformed record must cost only itself,
        # never the whole import.
        try:
            qso = _build_qso(record, mode_table=mode_table)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "skipping unreadable ADIF record (CALL=%r): %s",
                record.get("CALL", ""), exc,
            )
            return
        if qso is not None:
            qsos.append(qso)

    current: dict[str, str] = {}
    for tag, value in _tokenize(data):
        if tag == "EOR":
            _add(current)
            current = {}
            continue
        if tag == "EOH":
            # Stray EOH inside records — reset and continue.
            current = {}
            continue
        current[tag] = value
    # Trailing record without <EOR> — try to build it anyway.
    if current:
        _add(current)
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
