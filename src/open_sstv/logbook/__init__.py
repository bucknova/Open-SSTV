# SPDX-License-Identifier: GPL-3.0-or-later
"""Logbook (QSO log) — data layer.

v0.4 introduces a contact log that captures every TX and RX as a row
with mode / time / frequency / callsign / RSV / image / notes.  ADIF
3.1.5 export/import makes the log interoperable with HRD, N1MM, LoTW,
eQSL, Club Log, QRZ.com.

Package layout
──────────────
- ``model``       — ``QSO`` dataclass + ``StationInfo`` for ADIF export.
- ``store``       — ``LogbookStore`` — SQLite-backed CRUD + queries.
- ``adif``        — ADIF 3.1.5 reader/writer.  Pure-Python, no deps.

Higher layers (added in PR #2 + #3 of the v0.4 milestone):
- ``coordinator`` — listens to RX/TX completion signals, builds drafts.
- UI:               ``ui/logbook_dialog.py`` + ``ui/log_qso_dialog.py``.

This PR (v0.4 PR #1) ships only the data layer — no user-visible
behaviour change.  The package is import-safe but nothing in the
running app touches it yet.
"""
from __future__ import annotations

from open_sstv.logbook.adif import (
    AdifParseError,
    export_adif,
    import_adif,
)
from open_sstv.logbook.model import DIRECTIONS, QSO, Direction, StationInfo
from open_sstv.logbook.store import (
    LogbookStore,
    SchemaTooNewError,
    default_db_path,
)

__all__ = [
    "DIRECTIONS",
    "AdifParseError",
    "Direction",
    "LogbookStore",
    "QSO",
    "SchemaTooNewError",
    "StationInfo",
    "default_db_path",
    "export_adif",
    "import_adif",
]
