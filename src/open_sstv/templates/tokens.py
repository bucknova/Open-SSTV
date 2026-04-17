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
        2000, 1, 1, tzinfo=datetime.timezone.utc
    )
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H%M%S"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


# Regex for each syntax.  Run in a specific order inside ``resolve_tokens``
# so that ``%%`` processes before ``{...}`` can interfere.
_PERCENT_TOKEN_RE = re.compile(r"%(rx_tx|ts|[a-z])")
_NAMED_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")


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
    # --- Pass 0: escape literal %% before any other processing ---
    # Use a sentinel that can't collide with user input.
    SENTINEL = "\x00LITERAL_PERCENT\x00"
    pattern = pattern.replace("%%", SENTINEL)

    # --- Pass 1: %x tokens ---
    pct_table = _percent_table(ctx)

    def _pct_sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return pct_table.get(key, match.group(0))

    pattern = _PERCENT_TOKEN_RE.sub(_pct_sub, pattern)

    # --- Pass 2: {name} tokens ---
    named_table = _named_table(ctx)

    def _named_sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return named_table.get(key, match.group(0))

    pattern = _NAMED_TOKEN_RE.sub(_named_sub, pattern)

    # --- Restore escaped literal percents ---
    return pattern.replace(SENTINEL, "%")


__all__ = ["TokenContext", "resolve_tokens"]
