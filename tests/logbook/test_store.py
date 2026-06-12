# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for LogbookStore (SQLite CRUD + filtered queries)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import (
    SCHEMA_VERSION,
    LogbookStore,
    SchemaTooNewError,
    default_db_path,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    db = tmp_path / "logbook.db"
    with LogbookStore(db) as s:
        yield s


def _q(
    *,
    direction: str = "TX",
    callsign: str = "N0CALL",
    mode: str = "Martin M1",
    freq_hz: int | None = 14_230_000,
    when: datetime | None = None,
) -> QSO:
    return QSO(
        direction=direction,  # type: ignore[arg-type]
        callsign=callsign,
        time_utc=when if when is not None else datetime.now(UTC),
        mode=mode,
        frequency_hz=freq_hz,
    )


# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_db_path_has_filename(self) -> None:
        p = default_db_path()
        assert p.name == "logbook.db"
        # Don't assert on parent existence — platformdirs is OS-specific
        # and we don't want test pollution.


# ---------------------------------------------------------------------------
# Schema init / versioning
# ---------------------------------------------------------------------------


class TestSchema:
    def test_fresh_db_gets_schema_v1(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        with LogbookStore(db) as s:
            assert s.schema_version == SCHEMA_VERSION
            assert s.count() == 0

    def test_existing_db_does_not_reinit(self, tmp_path: Path) -> None:
        db = tmp_path / "existing.db"
        with LogbookStore(db) as s:
            s.insert(_q(callsign="W0AEZ"))
        # Reopen — count should persist
        with LogbookStore(db) as s2:
            assert s2.count() == 1
            assert s2.schema_version == SCHEMA_VERSION

    def test_future_schema_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "future.db"
        with LogbookStore(db):
            pass
        # Manually bump user_version
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        with pytest.raises(SchemaTooNewError):
            LogbookStore(db)

    def test_path_property(self, tmp_path: Path) -> None:
        db = tmp_path / "p.db"
        with LogbookStore(db) as s:
            assert s.path == db

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "logbook.db"
        with LogbookStore(nested) as s:
            assert nested.exists()
            assert s.count() == 0


# ---------------------------------------------------------------------------
# Insert / Get
# ---------------------------------------------------------------------------


class TestInsertGet:
    def test_insert_assigns_id_and_timestamps(self, store: LogbookStore) -> None:
        q = _q(callsign="W0AEZ")
        saved = store.insert(q)
        assert saved.id is not None and saved.id > 0
        assert saved.created_at is not None and saved.created_at.tzinfo is not None
        assert saved.updated_at is not None
        assert saved.callsign == "W0AEZ"

    def test_insert_does_not_mutate_input(self, store: LogbookStore) -> None:
        q = _q(callsign="W0AEZ")
        store.insert(q)
        # The original dataclass should still have id=None
        assert q.id is None

    def test_get_round_trips_all_fields(self, store: LogbookStore) -> None:
        when = datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)
        original = QSO(
            direction="RX",
            callsign="W0AEZ",
            time_utc=when,
            mode="Scottie 1",
            frequency_hz=14_233_000,
            rsv_sent="595",
            rsv_received="589",
            name="Kevin",
            qth="St Louis, MO",
            grid="EM48",
            comment="first QSO of the year",
            image_path=Path("/tmp/rx.png"),
            audio_path=Path("/tmp/rx.wav"),
        )
        saved = store.insert(original)
        got = store.get(saved.id)  # type: ignore[arg-type]
        assert got is not None
        assert got.direction == "RX"
        assert got.callsign == "W0AEZ"
        assert got.time_utc == when
        assert got.mode == "Scottie 1"
        assert got.frequency_hz == 14_233_000
        assert got.rsv_sent == "595"
        assert got.rsv_received == "589"
        assert got.name == "Kevin"
        assert got.qth == "St Louis, MO"
        assert got.grid == "EM48"
        assert got.comment == "first QSO of the year"
        assert got.image_path == Path("/tmp/rx.png")
        assert got.audio_path == Path("/tmp/rx.wav")

    def test_get_missing_returns_none(self, store: LogbookStore) -> None:
        assert store.get(99999) is None

    def test_insert_with_none_freq_and_paths(self, store: LogbookStore) -> None:
        q = QSO(direction="TX", callsign="N0CALL", frequency_hz=None)
        saved = store.insert(q)
        got = store.get(saved.id)  # type: ignore[arg-type]
        assert got is not None
        assert got.frequency_hz is None
        assert got.image_path is None
        assert got.audio_path is None

    def test_insert_rejects_invalid_direction(self, store: LogbookStore) -> None:
        bad = QSO(direction="SIDEWAYS")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="invalid direction"):
            store.insert(bad)

    def test_naive_datetime_assumed_utc_with_warning(
        self, store: LogbookStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        naive = datetime(2026, 5, 28, 17, 30, 0)  # no tz
        q = QSO(direction="TX", callsign="N0CALL", time_utc=naive)
        with caplog.at_level("WARNING", logger="open_sstv.logbook.store"):
            saved = store.insert(q)
        assert any("naive datetime" in r.message for r in caplog.records)
        got = store.get(saved.id)  # type: ignore[arg-type]
        assert got is not None
        assert got.time_utc.tzinfo is not None
        # Should match the naive components, interpreted as UTC
        assert got.time_utc == naive.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Update / Delete
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_refreshes_updated_at(self, store: LogbookStore) -> None:
        saved = store.insert(_q(callsign="N0CALL"))
        original_updated = saved.updated_at
        saved.callsign = "W0AEZ"
        # Ensure timestamp advances by sleeping >1 sec resolution
        # (our format is YYYY-MM-DDTHH:MM:SS+00:00 → 1s resolution)
        import time as _time
        _time.sleep(1.1)
        updated = store.update(saved)
        assert updated.callsign == "W0AEZ"
        assert updated.updated_at is not None
        assert original_updated is not None
        assert updated.updated_at > original_updated

    def test_update_preserves_created_at(self, store: LogbookStore) -> None:
        saved = store.insert(_q(callsign="N0CALL"))
        original_created = saved.created_at
        saved.qth = "Boulder"
        updated = store.update(saved)
        assert updated.created_at == original_created

    def test_update_rejects_none_id(self, store: LogbookStore) -> None:
        q = _q()  # id=None
        with pytest.raises(ValueError, match="id=None"):
            store.update(q)

    def test_update_missing_id_raises(self, store: LogbookStore) -> None:
        q = _q()
        q.id = 99999
        with pytest.raises(ValueError, match="not found"):
            store.update(q)


class TestDelete:
    def test_delete_removes_row(self, store: LogbookStore) -> None:
        saved = store.insert(_q())
        store.delete(saved.id)  # type: ignore[arg-type]
        assert store.get(saved.id) is None  # type: ignore[arg-type]
        assert store.count() == 0

    def test_delete_missing_raises(self, store: LogbookStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.delete(99999)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@pytest.fixture
def populated_store(store: LogbookStore) -> LogbookStore:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # Insert in non-time order so we can verify sorting
    store.insert(_q(callsign="W0AEZ", mode="Martin M1", when=base + timedelta(days=2)))
    store.insert(_q(callsign="K1ABC", mode="Scottie 1", when=base, direction="RX"))
    store.insert(_q(callsign="N0CALL", mode="PD 120", when=base + timedelta(days=1)))
    store.insert(_q(callsign="W0AEZ", mode="Martin M2", when=base + timedelta(days=3)))
    return store


class TestListQsos:
    def test_no_filter_returns_all(self, populated_store: LogbookStore) -> None:
        all_qsos = populated_store.list_qsos()
        assert len(all_qsos) == 4

    def test_default_order_is_time_desc(self, populated_store: LogbookStore) -> None:
        all_qsos = populated_store.list_qsos()
        times = [q.time_utc for q in all_qsos]
        assert times == sorted(times, reverse=True)

    def test_order_time_asc(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(order="time_asc")
        times = [q.time_utc for q in rows]
        assert times == sorted(times)

    def test_order_callsign_asc(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(order="callsign_asc")
        calls = [q.callsign for q in rows]
        assert calls == sorted(calls)

    def test_order_invalid_raises(self, populated_store: LogbookStore) -> None:
        with pytest.raises(ValueError, match="unknown order"):
            populated_store.list_qsos(order="random")

    def test_filter_callsign_substring(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(callsign="w0")
        assert len(rows) == 2
        assert all("W0" in q.callsign for q in rows)

    def test_filter_callsign_case_insensitive(self, populated_store: LogbookStore) -> None:
        upper = populated_store.list_qsos(callsign="K1ABC")
        lower = populated_store.list_qsos(callsign="k1abc")
        assert len(upper) == len(lower) == 1

    def test_filter_direction_rx(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(direction="RX")
        assert len(rows) == 1
        assert rows[0].callsign == "K1ABC"

    def test_filter_direction_tx(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(direction="TX")
        assert len(rows) == 3

    def test_filter_direction_invalid_raises(self, populated_store: LogbookStore) -> None:
        with pytest.raises(ValueError, match="invalid direction"):
            populated_store.list_qsos(direction="SIDEWAYS")

    def test_filter_mode_substring(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(mode="Martin")
        assert len(rows) == 2

    def test_filter_since(self, populated_store: LogbookStore) -> None:
        cutoff = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
        rows = populated_store.list_qsos(since=cutoff)
        # Should include qsos at days 1, 2, 3 (since is inclusive)
        assert len(rows) == 3

    def test_filter_until(self, populated_store: LogbookStore) -> None:
        cutoff = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
        rows = populated_store.list_qsos(until=cutoff)
        # Should include qsos at day 0 only (until is exclusive)
        assert len(rows) == 1

    def test_filter_since_and_until_range(self, populated_store: LogbookStore) -> None:
        since = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
        until = datetime(2026, 1, 4, 0, 0, 0, tzinfo=UTC)
        rows = populated_store.list_qsos(since=since, until=until)
        assert len(rows) == 2  # days 1 and 2 (day 3 is at hour 12 which is past day 3 00:00)

    def test_filter_combined(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(callsign="w0", mode="Martin", direction="TX")
        assert len(rows) == 2

    def test_limit(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(limit=2)
        assert len(rows) == 2

    def test_empty_callsign_filter_returns_all(self, populated_store: LogbookStore) -> None:
        rows = populated_store.list_qsos(callsign="")
        assert len(rows) == 4


class TestCount:
    def test_count_empty(self, store: LogbookStore) -> None:
        assert store.count() == 0

    def test_count_after_inserts(self, store: LogbookStore) -> None:
        for i in range(5):
            store.insert(_q(callsign=f"N{i}CALL"))
        assert store.count() == 5


# ---------------------------------------------------------------------------
# Cross-OS path serialization
# ---------------------------------------------------------------------------


class TestPathSerialization:
    def test_paths_round_trip_as_posix(self, store: LogbookStore) -> None:
        """Cross-OS-stable: store as POSIX, regardless of input OS style."""
        q = QSO(
            direction="TX",
            callsign="W0AEZ",
            image_path=Path("/tmp/test/img.png"),
        )
        saved = store.insert(q)
        got = store.get(saved.id)  # type: ignore[arg-type]
        assert got is not None
        # The Path comparison happens at Python level so platform-specific
        # construction is normalized.
        assert got.image_path is not None
        assert "img.png" in str(got.image_path)


class TestLikeEscaping:
    """Audit #6: % and _ typed into filters are literals, not wildcards."""

    def _store_with(self, tmp_path, *callsigns):
        store = LogbookStore(tmp_path / "esc.db")
        for c in callsigns:
            store.insert(_q(callsign=c))
        return store

    def test_underscore_is_literal(self, tmp_path) -> None:
        store = self._store_with(tmp_path, "W0AEZ", "W0_EZ")
        got = [q.callsign for q in store.list_qsos(callsign="_")]
        assert got == ["W0_EZ"]
        store.close()

    def test_percent_is_literal(self, tmp_path) -> None:
        store = self._store_with(tmp_path, "W0AEZ")
        assert store.list_qsos(callsign="%") == []
        store.close()

    def test_underscore_does_not_wildcard_match(self, tmp_path) -> None:
        store = self._store_with(tmp_path, "W0AEZ")
        assert store.list_qsos(callsign="W0_EZ") == []
        store.close()

    def test_mode_filter_escaped_too(self, tmp_path) -> None:
        store = LogbookStore(tmp_path / "esc2.db")
        store.insert(_q(mode="Martin M1"))
        assert store.list_qsos(mode="M_rtin") == []
        assert len(store.list_qsos(mode="Martin")) == 1
        store.close()


class TestInsertMany:
    """Audit #10: batched import — one transaction, all-or-nothing."""

    def test_inserts_all_and_returns_count(self, tmp_path) -> None:
        store = LogbookStore(tmp_path / "many.db")
        n = store.insert_many([_q(callsign=f"K{i}AAA") for i in range(5)])
        assert n == 5
        assert store.count() == 5
        store.close()

    def test_failure_rolls_back_whole_batch(self, tmp_path) -> None:
        store = LogbookStore(tmp_path / "many2.db")
        bad = _q()
        bad.direction = "SIDEWAYS"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            store.insert_many([_q(callsign="K1OK"), bad])
        assert store.count() == 0, "partial import must roll back"
        # Connection is healthy afterwards — normal inserts still work.
        store.insert(_q(callsign="K2OK"))
        assert store.count() == 1
        store.close()

    def test_empty_batch_is_noop(self, tmp_path) -> None:
        store = LogbookStore(tmp_path / "many3.db")
        assert store.insert_many([]) == 0
        store.close()


class TestListDedupeFields:
    def test_returns_key_columns_excluding_drafts(self, tmp_path) -> None:
        store = LogbookStore(tmp_path / "keys.db")
        store.insert(_q(callsign="K1ABC", mode="Martin M1"))
        store.insert(_q(callsign=""))  # draft — excluded
        fields = store.list_dedupe_fields()
        assert len(fields) == 1
        callsign, time_iso, mode = fields[0]
        assert callsign == "K1ABC"
        assert mode == "Martin M1"
        assert time_iso.endswith("+00:00")
        store.close()
