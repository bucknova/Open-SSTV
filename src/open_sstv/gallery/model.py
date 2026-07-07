# SPDX-License-Identifier: GPL-3.0-or-later
"""GalleryItem — one browsable image, optionally enriched by a QSO.

The v0.5 gallery is a **filesystem × logbook left-join** (see
``docs/v0.5-plan.md``): the item is always a file on disk; a matching
logbook ``QSO`` record, when one exists, *enriches* it with the
authoritative callsign / mode / frequency / time.

Resolution priority for the display fields is therefore:

    linked QSO  >  parsed-from-filename  >  file mtime

so a logged image shows exactly what the operator recorded, while an
auto-saved image the user never logged still shows a sensible date and
(when the filename carries it) a mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from open_sstv.logbook.model import QSO


@dataclass
class GalleryItem:
    """A single image in the gallery.

    ``qso`` is left ``None`` by the scanner and filled by the index
    layer (``gallery.index.enrich``) once the logbook join is built —
    the scanner stays pure filesystem, no logbook dependency.
    """

    path: Path
    #: Filesystem modification time, epoch seconds.  Always present;
    #: the pattern-independent fallback for "when" (an auto-saved
    #: image's mtime is essentially its decode-completion time).
    mtime: float
    size_bytes: int
    #: ``Mode`` enum *value* (e.g. ``"scottie_s1"``) parsed from the
    #: filename, or ``None`` if the name carried no recognisable mode.
    parsed_mode: str | None = None
    #: UTC date parsed from a ``YYYY-MM-DD`` run in the filename (the
    #: default ``%d`` token), or ``None``.
    parsed_date: date | None = None
    #: Linked logbook record, or ``None`` for an unlogged file.
    qso: QSO | None = None

    # -- resolved display views (qso > parsed > mtime) -----------------

    @property
    def timestamp_utc(self) -> datetime:
        """Best available UTC timestamp for sorting / grouping by date."""
        if self.qso is not None:
            return self.qso.time_utc.astimezone(UTC)
        if self.parsed_date is not None:
            return datetime(
                self.parsed_date.year,
                self.parsed_date.month,
                self.parsed_date.day,
                tzinfo=UTC,
            )
        return datetime.fromtimestamp(self.mtime, tz=UTC)

    @property
    def display_mode(self) -> str:
        """Human mode string, or ``"Unknown"`` when nothing carries it.

        The linked QSO's mode is already in display form ("Martin M1");
        a parsed enum value ("martin_m1") is returned verbatim — the UI
        can prettify, but the raw value is a stable grouping key.
        """
        if self.qso is not None and self.qso.mode:
            return self.qso.mode
        if self.parsed_mode is not None:
            return self.parsed_mode
        return "Unknown"

    @property
    def callsign(self) -> str:
        """Worked callsign, from the linked QSO only (empty otherwise)."""
        return self.qso.callsign if self.qso is not None else ""

    @property
    def direction(self) -> str | None:
        """``"RX"`` / ``"TX"`` from the linked QSO, or ``None`` if unlogged."""
        return self.qso.direction if self.qso is not None else None

    @property
    def is_logged(self) -> bool:
        return self.qso is not None


__all__ = ["GalleryItem"]
