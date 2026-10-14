# SPDX-License-Identifier: GPL-3.0-or-later
"""Token resolver for Open-SSTV templates (filenames in v0.2.8, full
image templates in v0.3).

Two token syntaxes are supported interchangeably so authors can pick
whichever reads better to them, and MMSSTV-familiar operators get their
muscle-memory ``%c`` while new users get readable ``{callsign}``:

.. code-block:: text

    %c   {callsign}    own callsign (uppercased)
    %m   {mode}        SSTV mode short-name, filename-safe (e.g. "Scottie-S1")
    %d   {date}        UTC date, strftime ``%Y-%m-%d`` (filename-sortable ISO)
    %t   {time}        UTC time, strftime ``%H%M%S`` (no colons, Windows-safe)
    %ts  {timestamp}   Unix epoch seconds (compact, monotonic)
    %rx_tx {direction} literal "RX" or "TX"
    %%                 literal percent sign

Unknown tokens pass through unchanged — forward-compatible with future
tokens that a template may reference but an older install doesn't know
about.  Callers that need strict validation can check the resolved
string for unresolved ``%x`` or ``{name}`` runs afterward.

Design note: this module is pure — no Qt, no filesystem, no clock.
The caller provides a ``TokenContext`` with everything pre-resolved
(including the current time), which keeps the resolver deterministic
and trivially unit-testable.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenContext:
    """Everything a template may need at resolve time.

    Populated by the caller (``MainWindow`` / autosave code) and passed
    through the pure ``resolve_tokens`` function.  Frozen so it's
    hashable and safe to share across threads.

    Attributes
    ----------
    callsign:
        Own callsign from ``AppConfig.callsign``.  Uppercased.  May be
        empty — in which case ``%c`` resolves to an empty string and the
        filename builder compresses any resulting double-separators.
    mode:
        SSTV mode short-name already formatted for filenames (spaces
        and underscores → dashes, e.g. ``"Scottie-S1"``).  The caller
        is responsible for the formatting so this module stays free of
        ``Mode`` enum coupling.
    direction:
        ``"RX"`` or ``"TX"`` — which transfer this filename belongs to.
    now_utc:
        UTC timestamp to render date/time from.  The caller passes
        ``datetime.datetime.now(datetime.timezone.utc)`` at the point
        of interest; keeping the clock out of this module makes tests
        deterministic.
    date_format:
        strftime pattern for ``%d`` / ``{date}``.  Defaults to ISO
        ``"%Y-%m-%d"`` because filenames sort correctly that way.
    time_format:
        strftime pattern for ``%t`` / ``{time}``.  Defaults to
        ``"%H%M%S"`` — the colon separator is illegal on Windows.
    """

    callsign: str = ""
    mode: str = ""
    direction: Literal["RX", "TX"] = "RX"
    now_utc: datetime.datetime = datetime.datetime(
        2000, 1, 1, tzinfo=datetime.UTC
    )
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H%M%S"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


# Both syntaxes plus the ``%%`` escape in ONE alternation, applied in a
# single ``re.sub`` pass.  v0.6.14 audit: the old two-pass resolver
# re-scanned the output of the ``%x`` pass for ``{name}`` tokens, so a
# substituted value that happened to contain ``{callsign}`` (a QSO note,
# or remote-supplied text) was expanded a second time.  One pass means a
# substituted value is never re-interpreted.
_TOKEN_RE = re.compile(r"%%|%(rx_tx|ts|[a-z])|\{([a-z_]+)\}")


def _percent_table(ctx: TokenContext) -> dict[str, str]:
    """Map single-char (and ``ts`` / ``rx_tx``) tokens to their resolved text."""
    return {
        "c": ctx.callsign,
        "m": ctx.mode,
        "d": ctx.now_utc.strftime(ctx.date_format),
        "t": ctx.now_utc.strftime(ctx.time_format),
        "ts": str(int(ctx.now_utc.timestamp())),
        "rx_tx": ctx.direction,
    }


def _named_table(ctx: TokenContext) -> dict[str, str]:
    """Map named-token keys to their resolved text."""
    return {
        "callsign": ctx.callsign,
        "mode": ctx.mode,
        "date": ctx.now_utc.strftime(ctx.date_format),
        "time": ctx.now_utc.strftime(ctx.time_format),
        "timestamp": str(int(ctx.now_utc.timestamp())),
        "direction": ctx.direction,
    }


def resolve_tokens(pattern: str, ctx: TokenContext) -> str:
    """Resolve all tokens in *pattern* against *ctx*.

    Unknown tokens pass through unchanged so an older install doesn't
    mangle a template that references a newer-version token.  Callers
    that want strict validation can re-scan the result for leftover
    ``%`` or ``{`` markers.

    Parameters
    ----------
    pattern:
        The raw template string, e.g. ``"%d_%t_%c_%m"``.
    ctx:
        All context values (callsign, mode, clock, format strings)
        the resolver might need.

    Returns
    -------
    str
        The fully-resolved string with all recognised tokens expanded.
    """
    pct_table = _percent_table(ctx)
    named_table = _named_table(ctx)

    def _sub(match: re.Match[str]) -> str:
        pct_key, named_key = match.group(1), match.group(2)
        if pct_key is not None:
            return pct_table.get(pct_key, match.group(0))
        if named_key is not None:
            return named_table.get(named_key, match.group(0))
        return "%"  # the %% escape

    return _TOKEN_RE.sub(_sub, pattern)


# ---------------------------------------------------------------------------
# v0.3 image-template token resolver
# ---------------------------------------------------------------------------
# The v0.2 API above (TokenContext / resolve_tokens) remains unchanged for
# filename templates.  The functions below form the v0.3 resolver used by
# the image-template compositor.  They share the same two-pass regex logic
# but draw from the richer QSOState / AppConfig / TXContext context objects
# instead of the lean filename-only TokenContext.
# ---------------------------------------------------------------------------

# Tokens whose *resolved value* is a callsign and should receive the
# slashed-zero transform when TextLayer.slashed_zero is True.
_CALLSIGN_PERCENT_KEYS: frozenset[str] = frozenset(["c", "o"])
_CALLSIGN_NAMED_KEYS: frozenset[str] = frozenset(["callsign", "tocall"])

def _hz_to_band(freq_hz: float) -> str:
    """Return the amateur-radio band label for a frequency, or '' if unknown.

    v0.6.14 audit: this used a lower-edges-only table, so any frequency
    above a band's top (7.5 MHz, 450 MHz) reported the band below.  It now
    shares ``open_sstv.bands`` with the ADIF exporter: out-of-band → "".
    """
    from open_sstv.bands import hz_to_band  # noqa: PLC0415

    return hz_to_band(freq_hz) or ""


def _apply_slashed_zero(value: str) -> str:
    """Replace ASCII '0' with 'Ø' (U+00D8) — the ham-radio convention."""
    return value.replace("0", "\u00d8")


def _v3_percent_table(
    qso_state: QSOState,
    app_config: AppConfig,
    tx_context: TXContext,
    *,
    now_utc: datetime.datetime,
    date_format: str,
    time_format: str,
    slashed_zero: bool,
) -> dict[str, str]:
    """Build the %x → resolved-string lookup for the v0.3 resolver."""
    callsign = app_config.callsign.upper()
    tocall = qso_state.tocall.upper()
    freq_str = (
        f"{tx_context.frequency_hz / 1_000_000:.4f} MHz"
        if tx_context.frequency_hz is not None
        else ""
    )
    band_str = (
        _hz_to_band(tx_context.frequency_hz)
        if tx_context.frequency_hz is not None
        else ""
    )
    table: dict[str, str] = {
        "c": _apply_slashed_zero(callsign) if slashed_zero else callsign,
        "g": getattr(app_config, "grid_square", "") or "",
        "n": getattr(app_config, "operator_name", "") or "",
        "o": _apply_slashed_zero(tocall) if slashed_zero else tocall,
        "r": qso_state.rst,
        "m": tx_context.mode_display_name,
        "d": now_utc.strftime(date_format),
        "t": now_utc.strftime(time_format),
        "f": freq_str,
        "b": band_str,
        # ``%q`` is the QSO serial; QTH only has the named form ``{qth}``
        # to avoid breaking templates that already use ``%q`` for serial.
        "q": str(qso_state.serial),
        "v": _open_sstv_version(),
        # Same vocabulary as filename templates (v0.6.14 audit): image
        # templates only ever render for TX, so the direction is fixed.
        "ts": str(int(now_utc.timestamp())),
        "rx_tx": "TX",
    }
    # Multi-char percent tokens
    table["name_o"] = qso_state.tocall_name
    table["note"] = qso_state.note
    return table


def _v3_named_table(
    qso_state: QSOState,
    app_config: AppConfig,
    tx_context: TXContext,
    *,
    now_utc: datetime.datetime,
    date_format: str,
    time_format: str,
    slashed_zero: bool,
) -> dict[str, str]:
    """Build the {name} → resolved-string lookup for the v0.3 resolver."""
    callsign = app_config.callsign.upper()
    tocall = qso_state.tocall.upper()
    freq_str = (
        f"{tx_context.frequency_hz / 1_000_000:.4f} MHz"
        if tx_context.frequency_hz is not None
        else ""
    )
    band_str = (
        _hz_to_band(tx_context.frequency_hz)
        if tx_context.frequency_hz is not None
        else ""
    )
    return {
        "callsign": _apply_slashed_zero(callsign) if slashed_zero else callsign,
        "grid": getattr(app_config, "grid_square", "") or "",
        "name": getattr(app_config, "operator_name", "") or "",
        "qth": getattr(app_config, "qth", "") or "",
        "tocall": _apply_slashed_zero(tocall) if slashed_zero else tocall,
        "rst": qso_state.rst,
        "tocallname": qso_state.tocall_name,
        "note": qso_state.note,
        "mode": tx_context.mode_display_name,
        "date": now_utc.strftime(date_format),
        "time": now_utc.strftime(time_format),
        "freq": freq_str,
        "band": band_str,
        "qso_serial": str(qso_state.serial),
        "version": _open_sstv_version(),
        "timestamp": str(int(now_utc.timestamp())),
        "direction": "TX",
    }


# v0.3 uses a superset alternation: %c, %name_o, %note, %rx_tx, %ts, …,
# ``{name}``, the ``%%`` escape, and a literal backslash-n (``\n``) line
# break — matched in one pass so substituted values are never re-scanned
# (v0.6.14 audit: double expansion of ``{callsign}`` inside a note).
_V3_TOKEN_RE = re.compile(
    r"%%|\\n|%(name_o|note|rx_tx|ts|[a-z])|\{([a-z_]+)\}"
)


def _open_sstv_version() -> str:
    try:
        import open_sstv
        return open_sstv.__version__
    except Exception:  # noqa: BLE001
        return ""


def resolve_text(
    text: str,
    qso_state: QSOState,
    app_config: AppConfig,
    tx_context: TXContext,
    *,
    slashed_zero: bool = True,
    date_format: str = "%Y-%m-%d",
    time_format: str = "%H:%M",
    now_utc: datetime.datetime | None = None,
) -> str:
    """Resolve v0.3 image-template tokens in *text*.

    Supports both ``%c`` (MMSSTV-style) and ``{callsign}`` (named) forms.
    Unknown tokens pass through unchanged for forward-compatibility.

    Parameters
    ----------
    text:
        Raw template text, e.g. ``"CQ de %c"`` or ``"de {callsign}"``
    qso_state:
        Per-QSO dynamic fields (ToCall, RST, Name, etc.)
    app_config:
        User configuration (own callsign, grid, name, …)
    tx_context:
        TX-time context (mode name, frame size, rig frequency)
    slashed_zero:
        When True, ASCII ``0`` in *callsign-valued tokens only* is
        replaced with ``Ø`` (U+00D8).  Does not affect RST, grid, etc.
    date_format:
        strftime pattern for ``%d`` / ``{date}`` tokens.
    time_format:
        strftime pattern for ``%t`` / ``{time}`` tokens.
    now_utc:
        UTC timestamp for date/time tokens.  Defaults to the current
        wall-clock time; pass an explicit value to make tests deterministic.

    Returns
    -------
    str
        Fully resolved text.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.UTC)

    pct_table = _v3_percent_table(
        qso_state,
        app_config,
        tx_context,
        now_utc=now_utc,
        date_format=date_format,
        time_format=time_format,
        slashed_zero=slashed_zero,
    )

    named_table = _v3_named_table(
        qso_state,
        app_config,
        tx_context,
        now_utc=now_utc,
        date_format=date_format,
        time_format=time_format,
        slashed_zero=slashed_zero,
    )

    def _sub(match: re.Match[str]) -> str:
        pct_key, named_key = match.group(1), match.group(2)
        if pct_key is not None:
            return pct_table.get(pct_key, match.group(0))
        if named_key is not None:
            return named_table.get(named_key, match.group(0))
        if match.group(0) == "%%":
            return "%"
        # A literal backslash-n typed into the (single-line) editor field
        # is a line break, as the token cheat sheet promises (v0.6.14
        # audit).  Only template text is scanned, never substituted values.
        return "\n"

    return _V3_TOKEN_RE.sub(_sub, text)


__all__ = [
    "TokenContext",
    "resolve_text",
    "resolve_tokens",
]

# Deferred imports for static analysis (mypy / IDE / pyright) only.
#
# These imports MUST live behind ``TYPE_CHECKING`` because the
# ``open_sstv.templates.model`` and ``open_sstv.config.schema`` modules
# both transitively re-import token utilities from this file at module
# load time — a direct top-level import would deadlock the importer.
#
# ``from __future__ import annotations`` above defers all annotation
# evaluation to call-site reflection (which we don't do at runtime), so
# the types are never resolved without an explicit ``get_type_hints``
# call — meaning the runtime cost is zero.  Type-checkers still see the
# block and resolve ``QSOState`` / ``AppConfig`` / ``TXContext`` in the
# function signatures at lines 219, 264, 320, etc.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from open_sstv.config.schema import AppConfig
    from open_sstv.templates.model import QSOState, TXContext
