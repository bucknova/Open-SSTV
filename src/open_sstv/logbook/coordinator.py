# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture-flow coordinator — turns TX/RX completions into draft QSOs.

``MainWindow`` calls ``build_tx_draft`` / ``build_rx_draft`` from its
existing completion handlers (``_on_tx_complete`` /
``_on_rx_image_complete``), then either hands the draft to
``LogQsoDialog`` (default) or persists it silently via ``save_draft``
when ``AppConfig.auto_log_qsos`` is set.

Design notes
────────────
- **Pure Python, no Qt.**  The original plan sketched the coordinator
  as a signal listener, but completion handling already converges in
  ``MainWindow``'s slots — having those slots call plain methods keeps
  this module unit-testable without ``pytest-qt`` and avoids a second
  cross-thread hop.
- **Frequency comes from the caller, not from ``Rig.get_freq()``.**
  The plan suggested capturing ``get_freq()`` on the worker thread at
  completion, but (a) ``RxWorker`` deliberately has no rig reference,
  and (b) the 1 Hz rig poll is *suspended* during TX (OP-47: CAT reads
  interleaved with PTT writes drop USB-CODEC rigs off the bus), so a
  completion-time read from a second thread would reintroduce exactly
  the race OP-47 closed.  ``MainWindow`` instead caches the latest
  poll result and passes it in — the cached value is ≤1 s old during
  RX and is the pre-TX frequency during TX, which is the frequency the
  contact actually happened on.
- **The store opens lazily** on first use so operators who never open
  the logbook never get a ``logbook.db`` created, and a corrupt /
  future-schema DB surfaces as an error at the moment of use rather
  than blocking app startup.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from open_sstv.logbook.adif import mode_to_submode
from open_sstv.logbook.model import QSO, StationInfo
from open_sstv.logbook.store import LogbookStore, default_db_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from open_sstv.config.schema import AppConfig

#: Canonical human-readable names for the 22 supported modes, keyed by
#: the ``core.modes.Mode`` enum *value* (Qt unwraps ``StrEnum`` members
#: to bare strings through queued signals, so plain-string keys cover
#: both arrival shapes).  These follow the MMSSTV naming convention
#: where one exists ("Scottie 1", not "Scottie S1") because the ADIF
#: SUBMODE round-trip (``mode_to_submode`` → other loggers → import)
#: is the main consumer.
MODE_DISPLAY_NAMES: dict[str, str] = {
    "robot_36": "Robot 36",
    "martin_m1": "Martin M1",
    "martin_m2": "Martin M2",
    "martin_m3": "Martin M3",
    "martin_m4": "Martin M4",
    "scottie_s1": "Scottie 1",
    "scottie_s2": "Scottie 2",
    "scottie_s3": "Scottie 3",
    "scottie_s4": "Scottie 4",
    "scottie_dx": "Scottie DX",
    "pd_50": "PD 50",
    "pd_90": "PD 90",
    "pd_120": "PD 120",
    "pd_160": "PD 160",
    "pd_180": "PD 180",
    "pd_240": "PD 240",
    "pd_290": "PD 290",
    "wraase_sc2_120": "Wraase SC2-120",
    "wraase_sc2_180": "Wraase SC2-180",
    "pasokon_p3": "Pasokon P3",
    "pasokon_p5": "Pasokon P5",
    "pasokon_p7": "Pasokon P7",
}


def mode_display_name(mode: object) -> str:
    """Resolve a ``Mode`` enum member or raw string to its display name.

    Unknown strings (ADIF imports of modes we don't transmit, e.g.
    "Robot 72") pass through unchanged so nothing is lost on round-trip.
    """
    key = str(mode)
    return MODE_DISPLAY_NAMES.get(key, key)


def qso_dedupe_key(qso: QSO) -> tuple[str, str, str]:
    """Identity key for import dedupe: (callsign, time_utc, mode).

    Mode is compared in SUBMODE-compact uppercase form so "Martin M1",
    "MartinM1", and "martin m1" all collide — different loggers format
    the same mode differently and a re-import of our own export must
    never duplicate rows.
    """
    return (
        qso.callsign.strip().upper(),
        qso.time_utc.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
        mode_to_submode(qso.mode).upper(),
    )


class LogbookCoordinator:
    """Builds draft QSOs at TX/RX completion and owns the app's store.

    ``config_getter`` is a zero-arg callable returning the *current*
    ``AppConfig`` — MainWindow replaces its config object on settings
    save, so holding a direct reference would go stale.
    """

    def __init__(self, config_getter: Callable[[], AppConfig]) -> None:
        self._config_getter = config_getter
        self._store: LogbookStore | None = None

    # -- config-derived properties --------------------------------------

    @property
    def auto_log(self) -> bool:
        """True when drafts should be persisted silently (no dialog)."""
        return bool(getattr(self._config_getter(), "auto_log_qsos", False))

    def db_path(self) -> Path:
        """Resolve the logbook DB path: config override or platform default."""
        raw = (getattr(self._config_getter(), "logbook_db_path", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        return default_db_path()

    @property
    def store(self) -> LogbookStore:
        """The open ``LogbookStore``, created on first access.

        Raises ``SchemaTooNewError`` / ``sqlite3.Error`` / ``OSError``
        on a bad DB; callers surface the failure at point of use.
        """
        if self._store is None:
            self._store = LogbookStore(self.db_path())
        return self._store

    @property
    def store_is_open(self) -> bool:
        return self._store is not None

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def station_info(self) -> StationInfo:
        """Operator identity for ADIF export, from the live config."""
        cfg = self._config_getter()
        return StationInfo(
            callsign=(cfg.callsign or "").strip().upper(),
            grid=(cfg.grid_square or "").strip(),
            qth=(cfg.qth or "").strip(),
            name=(cfg.operator_name or "").strip(),
        )

    def mode_table(self) -> tuple[str, ...]:
        """Canonical mode names for ADIF SUBMODE normalisation on import."""
        return tuple(MODE_DISPLAY_NAMES.values())

    # -- draft builders ---------------------------------------------------

    def build_tx_draft(
        self,
        *,
        mode: object,
        frequency_hz: int | None = None,
        image_path: Path | None = None,
        tocall: str = "",
        rst_sent: str = "",
        to_name: str = "",
        note: str = "",
    ) -> QSO:
        """Draft QSO for a completed transmission.

        ``tocall`` / ``rst_sent`` / ``to_name`` / ``note`` come from the
        TX panel's QSO-state bar, so a contact the operator already
        described there arrives in the log dialog pre-filled.
        """
        return QSO(
            direction="TX",
            callsign=tocall.strip().upper(),
            time_utc=datetime.now(UTC),
            mode=mode_display_name(mode),
            frequency_hz=frequency_hz,
            rsv_sent=rst_sent.strip(),
            name=to_name.strip(),
            comment=note.strip(),
            image_path=image_path,
        )

    def build_rx_draft(
        self,
        *,
        mode: object,
        frequency_hz: int | None = None,
        image_path: Path | None = None,
        audio_path: Path | None = None,
    ) -> QSO:
        """Draft QSO for a completed reception (callsign unknown yet)."""
        return QSO(
            direction="RX",
            time_utc=datetime.now(UTC),
            mode=mode_display_name(mode),
            frequency_hz=frequency_hz,
            image_path=image_path,
            audio_path=audio_path,
        )

    # -- persistence ------------------------------------------------------

    def save_draft(self, qso: QSO) -> QSO:
        """Insert *qso* and return the stored row (id/timestamps filled)."""
        return self.store.insert(qso)

    def import_qsos(self, qsos: Iterable[QSO]) -> tuple[int, int]:
        """Insert *qsos*, skipping duplicates.  Returns ``(added, skipped)``.

        A duplicate is a row whose ``(callsign, time_utc, mode)`` key
        matches an existing row *or* an earlier entry in the same batch
        — re-importing a previously exported ADIF must be a no-op.
        Rows with an empty callsign are never treated as duplicates of
        each other (two un-filled RX drafts are distinct contacts).
        """
        seen = {
            qso_dedupe_key(existing)
            for existing in self.store.list_qsos()
            if existing.callsign.strip()
        }
        added = 0
        skipped = 0
        for qso in qsos:
            key = qso_dedupe_key(qso)
            if qso.callsign.strip() and key in seen:
                skipped += 1
                continue
            if qso.callsign.strip():
                seen.add(key)
            self.store.insert(qso)
            added += 1
        return (added, skipped)


__all__ = [
    "MODE_DISPLAY_NAMES",
    "LogbookCoordinator",
    "mode_display_name",
    "qso_dedupe_key",
]
