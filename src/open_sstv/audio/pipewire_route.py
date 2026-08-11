# SPDX-License-Identifier: GPL-3.0-or-later
"""Route Open-SSTV's own TX audio stream onto a specific PipeWire sink.

Why this exists: PortAudio only exposes PipeWire's own named sinks (a user's
virtual "Radio" routing sink, say) under its "JACK Audio Connection Kit" host
API — PipeWire ships a JACK-API-compatible shim (``pipewire-jack``) that
PortAudio links against. Targeting that host API directly for playback looked
like the obvious fix, but live testing proved PortAudio's *blocking*
``OutputStream.write()`` over it corrupts real audio (verified: a WAV export
of the exact same buffer is clean; the same buffer played over the ALSA
"default" pass-through is clean; only writing to the JACK-hostapi device
directly produces broadband noise). ``sd.play()``'s *callback*-based path
does not exhibit this, but Open-SSTV's TX path needs the blocking API's
progress/gain/stop/health-check hooks.

The workaround sidesteps the bug instead of fighting it: never open the
OutputStream against the JACK host API at all. Always open the safe ALSA
"default" device (the one Open-SSTV has always used), then use PipeWire's
PulseAudio-compatible control protocol — ``pactl move-sink-input <id>
<target-sink>`` — to move *that specific, already-open* playback stream onto
the user's chosen sink, after the fact. Verified end-to-end with real SSTV
audio: spectrogram of what comes out of the target sink's monitor is
indistinguishable from the reference WAV.

This also only retargets Open-SSTV's own stream — unlike making "Radio" the
PipeWire *default* sink (``wpctl set-default``), every other application's
undirected audio is untouched.

Linux/PipeWire only. Every public function degrades to "nothing to route"
(``False`` / ``None`` / ``[]``) on any failure — pactl not installed, no
PipeWire session, a timeout, unparseable output — never raises. A routing
failure must fall back to exactly today's existing behaviour (play on the
system default), never to something worse.

Public API:
    is_pipewire_active() -> bool
    list_pipewire_sinks() -> list[PipeWireSink]
    find_pipewire_sink_by_name(name) -> PipeWireSink | None
    snapshot_sink_input_ids() -> set[int]
    route_active_stream_to_sink(target, before_ids) -> bool
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

#: How long ``route_active_stream_to_sink`` waits for the new sink-input to
#: register before giving up. Generous relative to the < 100 ms observed in
#: testing — a slow/loaded system still gets a fair shot before we fall back.
_ROUTE_TIMEOUT_S: float = 1.0
_ROUTE_POLL_INTERVAL_S: float = 0.02

#: Timeout for each individual pactl subprocess call. pactl talking to a
#: live local PipeWire socket is normally sub-10ms; this is a safety net
#: against a wedged/unresponsive server, not a tuning knob.
_PACTL_TIMEOUT_S: float = 2.0


@dataclass(frozen=True, slots=True)
class PipeWireSink:
    """One PipeWire sink, as seen through pactl's PulseAudio-compatible view.

    ``id`` is pactl's numeric sink index *at listing time* — PipeWire node
    ids churn across sessions/graph changes, so callers should re-resolve
    (via ``find_pipewire_sink_by_name``) right before use rather than
    caching this across calls, mirroring how ``audio/devices.py`` treats
    PortAudio device indices.
    """

    id: int
    name: str
    description: str


def is_pipewire_active() -> bool:
    """Best-effort check for whether PipeWire's own daemon is live.

    Same detection ``audio/devices.py`` used for its (since-reverted)
    JACK-hostapi filter carve-out: PipeWire's daemon binds a Unix-domain
    socket at ``$XDG_RUNTIME_DIR/pipewire-0`` for as long as it's running.
    Never raises — an unset env var or an unstattable path just means "no".
    """
    try:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime_dir:
            return False
        return (Path(runtime_dir) / "pipewire-0").exists()
    except (OSError, ValueError):
        return False


def _run_pactl(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run ``pactl`` with the given args. ``None`` on any failure, logged."""
    try:
        return subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        _log.debug("pactl not found — PipeWire sink routing unavailable")
        return None
    except subprocess.TimeoutExpired:
        _log.warning("pactl %s timed out after %.1fs", args, _PACTL_TIMEOUT_S)
        return None
    except OSError as exc:  # noqa: BLE001 — never let a routing helper crash TX
        _log.warning("pactl %s failed: %s", args, exc)
        return None


def list_pipewire_sinks() -> list[PipeWireSink]:
    """All PipeWire sinks pactl currently knows about.

    Empty list (never raises) if PipeWire isn't active, ``pactl`` isn't
    installed, or its output can't be parsed — callers treat that exactly
    like "no PipeWire sinks available" rather than an error.
    """
    if not is_pipewire_active():
        return []
    result = _run_pactl(["-f", "json", "list", "sinks"])
    if result is None or result.returncode != 0:
        return []
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        _log.warning("pactl list sinks produced unparseable JSON")
        return []

    out: list[PipeWireSink] = []
    for entry in raw:
        try:
            sink_id = int(entry["index"])
            name = str(entry["name"])
        except (KeyError, TypeError, ValueError):
            continue
        # pactl reports a description of either JSON null or the literal
        # string "(null)" for some sinks (seen on a real system for a bare
        # ALSA hardware sink with no nicer PipeWire description set) — fall
        # back to the raw name in both cases so the picker always has
        # something sensible to show instead of the literal text "(null)".
        description = entry.get("description")
        if not description or description == "(null)":
            description = name
        out.append(PipeWireSink(id=sink_id, name=name, description=str(description)))
    return out


def find_pipewire_sink_by_name(name: str | None) -> PipeWireSink | None:
    """Look up a sink by its saved description string.

    Always re-queries (``list_pipewire_sinks()`` is cheap, one pactl call)
    rather than caching, since sink ids/availability can change between
    when a name was saved to config and when it's used for playback.
    """
    if not name:
        return None
    for sink in list_pipewire_sinks():
        if sink.description == name:
            return sink
    return None


def snapshot_sink_input_ids() -> set[int]:
    """The set of currently-live pactl sink-input indices.

    Call this immediately *before* opening the PortAudio stream that will
    be routed — ``route_active_stream_to_sink`` diffs against it to find
    the one new sink-input our own stream creates. Empty set (never
    raises) if PipeWire/pactl is unavailable.
    """
    if not is_pipewire_active():
        return set()
    result = _run_pactl(["-f", "json", "list", "sink-inputs"])
    if result is None or result.returncode != 0:
        return set()
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    ids: set[int] = set()
    for entry in raw:
        try:
            ids.add(int(entry["index"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def route_active_stream_to_sink(
    target: PipeWireSink,
    before_ids: set[int],
    timeout_s: float = _ROUTE_TIMEOUT_S,
    poll_interval_s: float = _ROUTE_POLL_INTERVAL_S,
) -> bool:
    """Move the sink-input that appeared since ``before_ids`` onto ``target``.

    Polls ``pactl list sink-inputs`` for a new index not present in
    ``before_ids`` — that's Open-SSTV's own just-opened playback stream,
    identified by process of elimination rather than by name matching
    (PortAudio's ALSA-pulse-bridge client name varies by build/platform).
    If more than one new id shows up in the same poll (another app started
    a stream in the same narrow window), the lowest new index is used —
    deterministic, and logged so it's diagnosable — since Open-SSTV itself
    never opens more than one TX OutputStream at a time.

    Returns ``False`` (never raises) if no new sink-input appears within
    ``timeout_s``, or if the move itself fails — the caller's playback
    simply continues on whatever device it already opened.
    """
    if not is_pipewire_active():
        return False
    deadline = time.monotonic() + timeout_s
    new_id: int | None = None
    while time.monotonic() < deadline:
        after_ids = snapshot_sink_input_ids()
        new_ids = after_ids - before_ids
        if new_ids:
            new_id = min(new_ids)
            if len(new_ids) > 1:
                _log.warning(
                    "route_active_stream_to_sink: %d new sink-inputs appeared "
                    "at once (%s) — picking the lowest id (%d)",
                    len(new_ids), sorted(new_ids), new_id,
                )
            break
        time.sleep(poll_interval_s)

    if new_id is None:
        _log.warning(
            "route_active_stream_to_sink: no new sink-input appeared within "
            "%.1fs — TX audio stays on the system default output",
            timeout_s,
        )
        return False

    result = _run_pactl(["move-sink-input", str(new_id), str(target.id)])
    if result is None or result.returncode != 0:
        _log.warning(
            "route_active_stream_to_sink: pactl move-sink-input %d -> %r "
            "(id %d) failed%s",
            new_id, target.description, target.id,
            f": {result.stderr.strip()}" if result is not None else "",
        )
        return False

    _log.info(
        "TX audio routed: sink-input %d -> %r (pactl sink id %d)",
        new_id, target.description, target.id,
    )
    return True


__all__ = [
    "PipeWireSink",
    "find_pipewire_sink_by_name",
    "is_pipewire_active",
    "list_pipewire_sinks",
    "route_active_stream_to_sink",
    "snapshot_sink_input_ids",
]
