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

- ``coordinator``  — builds draft QSOs at TX/RX completion; owns the
                     app's store instance; ADIF import dedupe.

UI lives in ``ui/logbook_dialog.py`` + ``ui/log_qso_dialog.py``; this
package stays Qt-free so the whole data + capture layer tests headless.
"""
from __future__ import annotations

from open_sstv.logbook.adif import (
    AdifParseError,
    export_adif,
    import_adif,
)
from open_sstv.logbook.coordinator import (
    MODE_DISPLAY_NAMES,
    LogbookCoordinator,
    mode_display_name,
    qso_dedupe_key,
)
from open_sstv.logbook.model import DIRECTIONS, QSO, Direction, StationInfo
from open_sstv.logbook.store import (
    LogbookStore,
    SchemaTooNewError,
    default_db_path,
)

__all__ = [
    "DIRECTIONS",
    "MODE_DISPLAY_NAMES",
    "AdifParseError",
    "Direction",
    "LogbookCoordinator",
    "LogbookStore",
    "QSO",
    "SchemaTooNewError",
    "StationInfo",
    "default_db_path",
    "export_adif",
    "import_adif",
    "mode_display_name",
    "qso_dedupe_key",
]
