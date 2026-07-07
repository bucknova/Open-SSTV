# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite-backed CRUD + queries for the QSO logbook.

Storage choice rationale (from ``docs/v0.4-plan.md``):

- **SQLite** (stdlib ``sqlite3``) is the source of truth.  Indexable
  for fast filter/search (callsign, date range, mode), survives
  thousands of QSOs accumulated over years, no new pip dep.
- **ADIF** is the interchange format only — generated on demand by
  ``logbook.adif.export_adif``, never the primary store.

Schema versioning uses ``PRAGMA user_version``.  Fresh DB → run the
v1 schema, stamp version 1.  Opening a DB with a higher version than
this build knows raises ``SchemaTooNewError`` so a downgraded app
doesn't silently corrupt a future-schema log.

Concurrency: callers should use a single ``LogbookStore`` per process
and treat it as thread-affine (UI thread).  Background-thread writes
should marshal back to the UI thread via Qt signals — the coordinator
in PR #2 will do this.  ``sqlite3`` connections aren't shareable
across threads with the default ``check_same_thread=True``, which we
leave on as a safety net.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import platformdirs

from open_sstv.logbook.model import DIRECTIONS, QSO

_log = logging.getLogger(__name__)

_APP_NAME = "open_sstv"
_DB_FILENAME = "logbook.db"

#: Bump when the schema changes in an incompatible way and add a
#: migration step in ``_init_schema``.  v0.4.0 ships with v1.
SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS qsos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    direction       TEXT    NOT NULL,
    callsign        TEXT    NOT NULL DEFAULT '',
    time_utc        TEXT    NOT NULL,
    mode            TEXT    NOT NULL DEFAULT '',
    frequency_hz    INTEGER,
    rsv_sent        TEXT    NOT NULL DEFAULT '',
    rsv_received    TEXT    NOT NULL DEFAULT '',
    name            TEXT    NOT NULL DEFAULT '',
    qth             TEXT    NOT NULL DEFAULT '',
    grid            TEXT    NOT NULL DEFAULT '',
    comment         TEXT    NOT NULL DEFAULT '',
    image_path      TEXT,
    audio_path      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qsos_callsign ON qsos(callsign);
CREATE INDEX IF NOT EXISTS idx_qsos_time     ON qsos(time_utc);
CREATE INDEX IF NOT EXISTS idx_qsos_mode     ON qsos(mode);
"""


class SchemaTooNewError(RuntimeError):
    """Raised when the DB on disk has a higher schema version than this build."""


def default_db_path() -> Path:
    """Absolute path to the default logbook DB (may not exist yet)."""
    return Path(platformdirs.user_data_dir(_APP_NAME)) / _DB_FILENAME


# ---------------------------------------------------------------------------
# ISO-8601 helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Serialize a UTC datetime as ISO-8601 with explicit ``+00:00``.

    SQLite stores datetimes as text; ISO-8601 sorts lexicographically
    in UTC, which makes date-range queries trivial without parsing.
    """
    if dt.tzinfo is None:
        # Lenient: assume naive datetimes are UTC rather than crash.
        # Logged so the bug shows up in diagnostics.
        _log.warning("naive datetime passed to logbook store; assuming UTC")
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string back into a tz-aware UTC datetime.

    Accepts both ``+00:00`` and ``Z`` suffix for forward-compat with
    ADIF-imported rows.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _path_or_none(s: str | None) -> Path | None:
    return Path(s) if s else None


def _path_str(p: Path | None) -> str | None:
    """POSIX path string for cross-OS-stable storage."""
    if p is None:
        return None
    return p.as_posix()


def _like_escape(text: str) -> str:
    r"""Escape SQL LIKE metacharacters in user-typed filter text.

    Without this, a ``_`` typed into the callsign filter matches every
    row (any-one-char wildcard) and ``%`` matches everything (audit
    #6).  Pairs with an ``ESCAPE '\'`` clause on the LIKE.
    """
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


# ---------------------------------------------------------------------------
# Row <-> QSO marshalling
# ---------------------------------------------------------------------------

def _row_to_qso(row: sqlite3.Row) -> QSO:
    return QSO(
        id=row["id"],
        direction=row["direction"],  # already "RX" or "TX"
        callsign=row["callsign"],
        time_utc=_parse_iso(row["time_utc"]),
        mode=row["mode"],
        frequency_hz=row["frequency_hz"],
        rsv_sent=row["rsv_sent"],
        rsv_received=row["rsv_received"],
        name=row["name"],
        qth=row["qth"],
        grid=row["grid"],
        comment=row["comment"],
        image_path=_path_or_none(row["image_path"]),
        audio_path=_path_or_none(row["audio_path"]),
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
    )


def _qso_to_params(qso: QSO, *, now: datetime) -> dict[str, Any]:
    """Build the parameter dict for INSERT / UPDATE statements.

    ``created_at`` is set on insert only (caller supplies the prior
    value for updates).  ``updated_at`` is always ``now``.
    """
    if qso.direction not in DIRECTIONS:
        raise ValueError(f"invalid direction: {qso.direction!r}")
    return {
        "direction": qso.direction,
        "callsign": qso.callsign,
        "time_utc": _iso(qso.time_utc),
        "mode": qso.mode,
        "frequency_hz": qso.frequency_hz,
        "rsv_sent": qso.rsv_sent,
        "rsv_received": qso.rsv_received,
        "name": qso.name,
        "qth": qso.qth,
        "grid": qso.grid,
        "comment": qso.comment,
        "image_path": _path_str(qso.image_path),
        "audio_path": _path_str(qso.audio_path),
        "updated_at": _iso(now),
    }


# ---------------------------------------------------------------------------
# LogbookStore
# ---------------------------------------------------------------------------

class LogbookStore:
    """SQLite-backed CRUD for the QSO log.

    Open one instance per process and keep it for the app lifetime.
    Use ``with LogbookStore(...) as store:`` in tests for clean
    teardown.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``isolation_level=None`` puts sqlite3 in autocommit mode; our
        # ops are single-statement and atomicity comes from sqlite's
        # own transaction-per-statement.  Saves the explicit commit()
        # calls and avoids accidental long-held transactions if a
        # caller forgets to close.
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Enforce CHECK constraints, foreign keys (none yet but cheap).
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # -- schema --------------------------------------------------------

    def _init_schema(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current == 0:
            _log.info("initializing logbook schema v%d at %s", SCHEMA_VERSION, self._db_path)
            self._conn.executescript(_SCHEMA_V1)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if current > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"logbook DB at {self._db_path} is schema v{current}, "
                f"but this build only knows v{SCHEMA_VERSION}.  "
                "Upgrade Open-SSTV or move the file aside."
            )
        # current == SCHEMA_VERSION → no migration needed in v1.
        # Future versions add `if current < N: <migrate>` blocks here.

    # -- CRUD ----------------------------------------------------------

    _INSERT_SQL = """
        INSERT INTO qsos (
            direction, callsign, time_utc, mode, frequency_hz,
            rsv_sent, rsv_received, name, qth, grid, comment,
            image_path, audio_path, created_at, updated_at
        ) VALUES (
            :direction, :callsign, :time_utc, :mode, :frequency_hz,
            :rsv_sent, :rsv_received, :name, :qth, :grid, :comment,
            :image_path, :audio_path, :created_at, :updated_at
        )
    """

    def insert(self, qso: QSO) -> QSO:
        """Insert a new QSO, returning a copy with id/created_at/updated_at filled.

        Does NOT mutate the input ``qso`` — returns a separately-fetched
        row so callers see exactly what the database holds.
        """
        now = datetime.now(UTC)
        params = _qso_to_params(qso, now=now)
        params["created_at"] = _iso(now)
        cur = self._conn.execute(self._INSERT_SQL, params)
        new_id = cur.lastrowid
        assert new_id is not None
        got = self.get(new_id)
        assert got is not None, "row vanished immediately after insert"
        return got

    def insert_many(self, qsos: Iterable[QSO]) -> int:
        """Insert many QSOs in ONE transaction.  Returns the count.

        The connection runs in autocommit (``isolation_level=None``),
        so looping over ``insert()`` costs one fsync'd transaction per
        row — minutes of GUI freeze on a 10k-record ADIF import (audit
        #10).  An explicit BEGIN…COMMIT batches the writes, and any
        failure rolls back the whole batch so a half-imported file
        can't leave the log in a guess-which-rows-made-it state.
        """
        now = datetime.now(UTC)
        self._conn.execute("BEGIN")
        try:
            count = 0
            for qso in qsos:
                params = _qso_to_params(qso, now=now)
                params["created_at"] = _iso(now)
                self._conn.execute(self._INSERT_SQL, params)
                count += 1
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return count

    def update(self, qso: QSO) -> QSO:
        """Update an existing QSO by id, refreshing ``updated_at``.

        Raises ``ValueError`` if ``qso.id`` is None or the row doesn't
        exist.
        """
        if qso.id is None:
            raise ValueError("cannot update QSO with id=None; use insert() first")
        now = datetime.now(UTC)
        params = _qso_to_params(qso, now=now)
        params["id"] = qso.id
        cur = self._conn.execute(
            """
            UPDATE qsos SET
                direction = :direction,
                callsign = :callsign,
                time_utc = :time_utc,
                mode = :mode,
                frequency_hz = :frequency_hz,
                rsv_sent = :rsv_sent,
                rsv_received = :rsv_received,
                name = :name,
                qth = :qth,
                grid = :grid,
                comment = :comment,
                image_path = :image_path,
                audio_path = :audio_path,
                updated_at = :updated_at
            WHERE id = :id
            """,
            params,
        )
        if cur.rowcount == 0:
            raise ValueError(f"QSO id={qso.id} not found")
        got = self.get(qso.id)
        assert got is not None
        return got

    def delete(self, qso_id: int) -> None:
        """Delete a QSO by id.  Raises ``ValueError`` if not found."""
        cur = self._conn.execute("DELETE FROM qsos WHERE id = ?", (qso_id,))
        if cur.rowcount == 0:
            raise ValueError(f"QSO id={qso_id} not found")

    def get(self, qso_id: int) -> QSO | None:
        """Return the QSO with the given id, or ``None`` if not present."""
        row = self._conn.execute(
            "SELECT * FROM qsos WHERE id = ?", (qso_id,)
        ).fetchone()
        return _row_to_qso(row) if row is not None else None

    def find_by_image_path(self, image_path: Path | str) -> QSO | None:
        """Return the QSO linking *image_path*, or ``None`` (v0.5 gallery).

        Matches the stored representation exactly: ``image_path`` is
        persisted as ``Path.as_posix()`` (see ``_path_str``), so the
        lookup normalises the argument the same way.  If several rows
        somehow share an image (manual edits), the most recent by id
        wins — the freshest link is the useful one.
        """
        posix = _path_str(Path(image_path))
        if posix is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM qsos WHERE image_path = ? ORDER BY id DESC LIMIT 1",
            (posix,),
        ).fetchone()
        return _row_to_qso(row) if row is not None else None

    # -- queries -------------------------------------------------------

    def list_qsos(
        self,
        *,
        callsign: str | None = None,
        direction: str | None = None,
        mode: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        order: str = "time_desc",
    ) -> list[QSO]:
        """Filtered list of QSOs.

        - ``callsign``: case-insensitive substring match.
        - ``direction``: exact "RX" or "TX".
        - ``mode``: case-insensitive substring match (so "Martin" matches
          "Martin M1", "Martin M2", …).
        - ``since`` / ``until``: time_utc range (inclusive of since,
          exclusive of until — half-open interval).
        - ``order``: ``"time_desc"`` (default), ``"time_asc"``,
          ``"callsign_asc"``.
        """
        sql = "SELECT * FROM qsos"
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if callsign:
            clauses.append(r"UPPER(callsign) LIKE :callsign ESCAPE '\'")
            params["callsign"] = f"%{_like_escape(callsign.upper())}%"
        if direction:
            if direction not in DIRECTIONS:
                raise ValueError(f"invalid direction filter: {direction!r}")
            clauses.append("direction = :direction")
            params["direction"] = direction
        if mode:
            clauses.append(r"UPPER(mode) LIKE :mode ESCAPE '\'")
            params["mode"] = f"%{_like_escape(mode.upper())}%"
        if since is not None:
            clauses.append("time_utc >= :since")
            params["since"] = _iso(since)
        if until is not None:
            clauses.append("time_utc < :until")
            params["until"] = _iso(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        order_map = {
            "time_desc": "time_utc DESC, id DESC",
            "time_asc": "time_utc ASC, id ASC",
            "callsign_asc": "callsign ASC, time_utc DESC",
        }
        if order not in order_map:
            raise ValueError(f"unknown order: {order!r}")
        sql += f" ORDER BY {order_map[order]}"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_qso(r) for r in rows]

    def count(self) -> int:
        """Total number of QSOs in the log."""
        return int(self._conn.execute("SELECT COUNT(*) FROM qsos").fetchone()[0])

    def list_dedupe_fields(self) -> list[tuple[str, str, str]]:
        """``(callsign, time_utc_iso, mode)`` for every row with a callsign.

        Import dedupe needs only these three columns; materialising
        full ``QSO`` objects (three datetime parses each) for a
        10k-row log just to build identity keys is wasted work
        (audit #10).  Empty-callsign drafts are excluded — they never
        participate in dedupe.
        """
        rows = self._conn.execute(
            "SELECT callsign, time_utc, mode FROM qsos WHERE callsign != ''"
        ).fetchall()
        return [(r["callsign"], r["time_utc"], r["mode"]) for r in rows]

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- context manager ----------------------------------------------

    def __enter__(self) -> LogbookStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
