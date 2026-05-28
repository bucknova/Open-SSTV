# SPDX-License-Identifier: GPL-3.0-or-later
"""QSO dataclass + StationInfo for ADIF export.

A ``QSO`` is a single contact log entry — one row in the logbook.
Fields are deliberately permissive (most default to empty) so that
draft QSOs created at TX/RX completion can be stored before the
user fills in callsign / RSV / notes.

Design notes
────────────
- **Direction** is a string literal (``"RX"`` or ``"TX"``) rather than
  an enum to keep ADIF roundtrip trivial and JSON-friendly.
- **Mode** is plain ``str`` (not the ``core.modes.Mode`` enum) so that
  ADIF imports containing non-SSTV modes (RTTY, CW, SSB, …) don't
  fail validation.  Callers converting from a ``Mode`` enum should
  pass ``mode_enum.value``.
- **frequency_hz** is integer Hz; ADIF export converts to MHz.  ``None``
  means the rig wasn't connected at QSO time.
- **time_utc** is timezone-aware UTC.  The store rejects naïve datetimes.
- **id / created_at / updated_at** are assigned by ``LogbookStore`` on
  insert; the dataclass leaves them ``None`` until then.
- **image_path / audio_path** store file references, not blobs.  If the
  user moves/deletes the underlying file outside the app, the row
  remains; UI shows a "missing image" indicator.

Validation lives in the UI layer (LogQsoDialog), not the model — the
model needs to be lenient for ADIF imports from other loggers that may
use non-canonical formatting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

#: TX = we transmitted, RX = we received.
Direction = Literal["RX", "TX"]

#: Set of valid direction strings.  Convenient for ``if d in DIRECTIONS``
#: checks at the UI/ADIF boundary.
DIRECTIONS: frozenset[str] = frozenset(get_args(Direction))


def _utc_now() -> datetime:
    """Return current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@dataclass
class QSO:
    """A single contact log entry.

    Mutable because the typical lifecycle is:

        draft = QSO(direction="RX", ...)         # auto-built from completion
        draft = store.insert(draft)              # gets id + created_at
        # user edits in LogQsoDialog
        draft.callsign = "W0AEZ"
        store.update(draft)                       # updated_at refreshed

    Frozen would force a ``dataclasses.replace`` round-trip per field
    edit, which is awkward for the dialog flow.
    """

    direction: Direction
    callsign: str = ""
    time_utc: datetime = field(default_factory=_utc_now)
    mode: str = ""
    frequency_hz: int | None = None
    rsv_sent: str = ""
    rsv_received: str = ""
    name: str = ""
    qth: str = ""
    grid: str = ""
    comment: str = ""
    image_path: Path | None = None
    audio_path: Path | None = None
    # Assigned by the store on insert; ``None`` for unsaved drafts.
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class StationInfo:
    """Operator's own identity at ADIF-export time.

    Pulled from ``AppConfig`` (callsign / operator_name / grid_square /
    qth) at the call site rather than stored per-QSO.  This is a
    deliberate simplification: hams who change callsign mid-log are
    rare enough that the cost of stamping every row with current
    station info isn't worth the model complexity.  If that changes,
    add ``station_callsign`` / ``my_gridsquare`` to ``QSO`` and bump
    the SQLite schema version.
    """

    callsign: str = ""
    grid: str = ""
    qth: str = ""
    name: str = ""
