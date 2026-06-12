# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the QSO + StationInfo dataclasses."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_sstv.logbook.model import DIRECTIONS, QSO, StationInfo


class TestDirections:
    def test_directions_set_has_rx_and_tx(self) -> None:
        assert DIRECTIONS == frozenset({"RX", "TX"})


class TestQSO:
    def test_construct_with_direction_only(self) -> None:
        q = QSO(direction="TX")
        assert q.direction == "TX"
        assert q.callsign == ""
        assert q.mode == ""
        assert q.frequency_hz is None
        assert q.image_path is None
        assert q.audio_path is None
        assert q.id is None
        assert q.created_at is None
        assert q.updated_at is None

    def test_default_time_is_timezone_aware_utc(self) -> None:
        q = QSO(direction="RX")
        assert q.time_utc.tzinfo is not None
        # Should be very close to "now"
        delta = abs((q.time_utc - datetime.now(UTC)).total_seconds())
        assert delta < 5

    def test_construct_full(self) -> None:
        when = datetime(2026, 5, 28, 17, 30, 0, tzinfo=UTC)
        q = QSO(
            direction="TX",
            callsign="N0CALL",
            time_utc=when,
            mode="Martin M1",
            frequency_hz=14_230_000,
            rsv_sent="595",
            rsv_received="585",
            name="Alice",
            qth="Boulder, CO",
            grid="DM79",
            comment="great signal",
            image_path=Path("/tmp/img.png"),
            audio_path=Path("/tmp/aud.wav"),
        )
        assert q.callsign == "N0CALL"
        assert q.frequency_hz == 14_230_000
        assert q.image_path == Path("/tmp/img.png")

    def test_mutability(self) -> None:
        """QSO is intentionally mutable for the edit-in-dialog flow."""
        q = QSO(direction="RX")
        q.callsign = "W0AEZ"
        assert q.callsign == "W0AEZ"

    def test_dataclass_decorator(self) -> None:
        """QSO must be a dataclass so callers can use dataclasses.replace etc."""
        assert dataclasses.is_dataclass(QSO)


class TestStationInfo:
    def test_default_all_empty(self) -> None:
        s = StationInfo()
        assert s.callsign == ""
        assert s.grid == ""
        assert s.qth == ""
        assert s.name == ""

    def test_construct_full(self) -> None:
        s = StationInfo(callsign="W0AEZ", grid="EM48", qth="St Louis", name="Kevin")
        assert s.callsign == "W0AEZ"

    def test_is_frozen(self) -> None:
        s = StationInfo(callsign="W0AEZ")
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.callsign = "K1ABC"  # type: ignore[misc]
