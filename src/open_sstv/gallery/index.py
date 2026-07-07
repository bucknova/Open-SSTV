# SPDX-License-Identifier: GPL-3.0-or-later
"""The filesystem × logbook join for the gallery.

Builds a ``{image_path: QSO}`` index from the logbook once (on gallery
open) and enriches scanned ``GalleryItem`` records with their matching
QSO.  This is the left-join half of the model in ``docs/v0.5-plan.md``:
the scanner supplies the files, this supplies the QSO enrichment, and
files without a QSO simply stay unenriched.

Join key is ``Path.as_posix()`` — matching how ``LogbookStore`` persists
``image_path`` (via ``_path_str``).  Both the scanned item paths and the
stored QSO paths are built from the same ``images_save_dir`` config, so
the unresolved posix form matches exactly; resolving would risk
diverging on symlinked or ``..``-containing base dirs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from open_sstv.gallery.model import GalleryItem
    from open_sstv.logbook.model import QSO
    from open_sstv.logbook.store import LogbookStore


def build_qso_index(store: LogbookStore) -> dict[str, QSO]:
    """Map ``image_path.as_posix()`` → QSO for every image-linked row.

    Rows with no ``image_path`` are skipped (nothing to join to).  If
    two rows link the same image, the later one by id wins — the same
    "freshest link" rule as ``LogbookStore.find_by_image_path``.
    """
    index: dict[str, QSO] = {}
    for qso in store.list_qsos(order="time_asc"):
        if qso.image_path is None:
            continue
        # time_asc → later id/time overwrites earlier for a shared image.
        index[qso.image_path.as_posix()] = qso
    return index


def enrich(items: Iterable[GalleryItem], index: dict[str, QSO]) -> None:
    """Set ``item.qso`` in place from *index* for each matching item."""
    for item in items:
        item.qso = index.get(item.path.as_posix())


__all__ = ["build_qso_index", "enrich"]
