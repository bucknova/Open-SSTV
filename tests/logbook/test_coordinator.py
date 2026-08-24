# SPDX-License-Identifier: GPL-3.0-or-later
"""LogbookCoordinator — draft building, lazy store, import dedupe."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import Mode
from open_sstv.logbook.coordinator import (
    MODE_DISPLAY_NAMES,
    LogbookCoordinator,
    mode_display_name,
    qso_dedupe_key,
)
from open_sstv.logbook.model import QSO
from open_sstv.logbook.store import SchemaTooNewError


def _coordinator(tmp_path: Path, **config_kwargs: object) -> LogbookCoordinator:
    cfg = AppConfig(**config_kwargs)  # type: ignore[arg-type]
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    return LogbookCoordinator(lambda: cfg)


def _q(callsign: str = "K1ABC", mode: str = "Martin M1", **kw: object) -> QSO:
    defaults: dict[str, object] = {
        "direction": "TX",
        "time_utc": datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(kw)
    return QSO(callsign=callsign, mode=mode, **defaults)  # type: ignore[arg-type]


class TestModeDisplayName:
    def test_covers_all_22_modes(self) -> None:
        assert len(MODE_DISPLAY_NAMES) == 22
        for mode in Mode:
            assert mode.value in MODE_DISPLAY_NAMES

    def test_enum_member_resolves(self) -> None:
        assert mode_display_name(Mode.MARTIN_M1) == "Martin M1"
        assert mode_display_name(Mode.SCOTTIE_S1) == "Scottie 1"
        assert mode_display_name(Mode.SCOTTIE_DX) == "Scottie DX"
        assert mode_display_name(Mode.PD_120) == "PD 120"
        assert mode_display_name(Mode.WRAASE_SC2_180) == "Wraase SC2-180"
        assert mode_display_name(Mode.PASOKON_P7) == "Pasokon P7"

    def test_bare_string_resolves(self) -> None:
        # Qt unwraps StrEnum through queued signals — bare values must work.
        assert mode_display_name("robot_36") == "Robot 36"

    def test_unknown_passes_through(self) -> None:
        assert mode_display_name("Robot 72") == "Robot 72"
        assert mode_display_name("RTTY") == "RTTY"


class TestDedupeKey:
    def test_format_variants_collide(self) -> None:
        a = _q(mode="Martin M1")
        b = _q(mode="MartinM1")
        c = _q(mode="martin m1")
        assert qso_dedupe_key(a) == qso_dedupe_key(b) == qso_dedupe_key(c)

    def test_callsign_case_insensitive(self) -> None:
        assert qso_dedupe_key(_q(callsign="k1abc")) == qso_dedupe_key(_q(callsign="K1ABC"))

    def test_different_time_distinct(self) -> None:
        a = _q()
        b = _q(time_utc=datetime(2026, 6, 1, 12, 0, 1, tzinfo=UTC))
        assert qso_dedupe_key(a) != qso_dedupe_key(b)


class TestConfigDerived:
    def test_auto_log_defaults_false(self, tmp_path: Path) -> None:
        assert _coordinator(tmp_path).auto_log is False

    def test_auto_log_reads_config(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path, auto_log_qsos=True)
        assert coord.auto_log is True

    def test_db_path_from_config(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        assert coord.db_path() == tmp_path / "logbook.db"

    def test_db_path_empty_falls_back_to_default(self) -> None:
        cfg = AppConfig()
        cfg.logbook_db_path = ""
        coord = LogbookCoordinator(lambda: cfg)
        # Platform default, not cwd-relative.
        assert coord.db_path().name == "logbook.db"
        assert coord.db_path().is_absolute()

    def test_config_getter_sees_live_object(self, tmp_path: Path) -> None:
        # MainWindow swaps its config on settings save; the getter
        # indirection must pick up the new object.
        cfg_box = [AppConfig()]
        cfg_box[0].logbook_db_path = str(tmp_path / "logbook.db")
        coord = LogbookCoordinator(lambda: cfg_box[0])
        assert coord.auto_log is False
        new_cfg = AppConfig(auto_log_qsos=True)
        new_cfg.logbook_db_path = str(tmp_path / "logbook.db")
        cfg_box[0] = new_cfg
        assert coord.auto_log is True

    def test_station_info_from_config(self, tmp_path: Path) -> None:
        coord = _coordinator(
            tmp_path,
            callsign="w0aez",
            operator_name="Kevin",
            grid_square="EN34",
            qth="St. Paul, MN",
        )
        info = coord.station_info()
        assert info.callsign == "W0AEZ"
        assert info.name == "Kevin"
        assert info.grid == "EN34"
        assert info.qth == "St. Paul, MN"

    def test_mode_table_matches_display_names(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        assert set(coord.mode_table()) == set(MODE_DISPLAY_NAMES.values())


class TestLazyStore:
    def test_store_not_opened_until_used(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        assert coord.store_is_open is False
        assert not (tmp_path / "logbook.db").exists()

    def test_store_opens_on_access(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        assert coord.store.count() == 0
        assert coord.store_is_open is True
        assert (tmp_path / "logbook.db").exists()
        coord.close()
        assert coord.store_is_open is False

    def test_schema_too_new_propagates(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "logbook.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA user_version = 999")
        conn.close()
        coord = _coordinator(tmp_path)
        with pytest.raises(SchemaTooNewError):
            _ = coord.store


class TestDraftBuilders:
    def test_tx_draft_fields(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        img = tmp_path / "tx.png"
        draft = coord.build_tx_draft(
            mode=Mode.MARTIN_M1,
            frequency_hz=14_230_000,
            image_path=img,
            tocall="k1abc",
            rst_sent="595",
            to_name="Sam",
            note="nice colour bars",
        )
        assert draft.direction == "TX"
        assert draft.callsign == "K1ABC"
        assert draft.mode == "Martin M1"
        assert draft.frequency_hz == 14_230_000
        assert draft.rsv_sent == "595"
        assert draft.rsv_received == ""
        assert draft.name == "Sam"
        assert draft.comment == "nice colour bars"
        assert draft.image_path == img
        assert draft.id is None
        assert draft.time_utc.tzinfo is not None

    def test_tx_draft_carries_contact_fields(self, tmp_path: Path) -> None:
        """RSTr / QTH / Grid from the QSO bar must reach the draft.

        Regression: these were added to the bar in v0.6.7 for the UDP
        External Log and wired only to that path, so typing a QTH and
        grid then logging the QSO silently dropped both — even though
        the capture dialog has rows for them.
        """
        draft = _coordinator(tmp_path).build_tx_draft(
            mode=Mode.MARTIN_M1,
            tocall="k1abc",
            rst_sent="595",
            rst_received="589",
            qth="Springfield",
            grid="en34",
        )
        assert draft.rsv_received == "589"
        assert draft.qth == "Springfield"
        assert draft.grid == "EN34"  # upper-cased like the bar does

    def test_tx_draft_minimal(self, tmp_path: Path) -> None:
        draft = _coordinator(tmp_path).build_tx_draft(mode="scottie_s1")
        assert draft.mode == "Scottie 1"
        assert draft.callsign == ""
        assert draft.rsv_received == ""
        assert draft.qth == ""
        assert draft.grid == ""
        assert draft.frequency_hz is None
        assert draft.image_path is None

    def test_rx_draft_fields(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        img = tmp_path / "rx.png"
        wav = tmp_path / "rx.wav"
        draft = coord.build_rx_draft(
            mode="pd_120",
            frequency_hz=7_171_000,
            image_path=img,
            audio_path=wav,
        )
        assert draft.direction == "RX"
        assert draft.callsign == ""
        assert draft.mode == "PD 120"
        assert draft.frequency_hz == 7_171_000
        assert draft.image_path == img
        assert draft.audio_path == wav


class TestSaveAndImport:
    def test_save_draft_round_trip(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        saved = coord.save_draft(coord.build_tx_draft(mode="martin_m1", tocall="K1ABC"))
        assert saved.id is not None
        assert coord.store.count() == 1

    def test_import_skips_existing(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        coord.store.insert(_q())
        added, skipped = coord.import_qsos([_q(), _q(callsign="N0XYZ")])
        assert (added, skipped) == (1, 1)
        assert coord.store.count() == 2

    def test_import_skips_within_batch(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        added, skipped = coord.import_qsos([_q(), _q(mode="MartinM1")])
        assert (added, skipped) == (1, 1)

    def test_reimport_of_export_is_noop(self, tmp_path: Path) -> None:
        from open_sstv.logbook.adif import export_adif, import_adif

        coord = _coordinator(tmp_path)
        coord.store.insert(_q())
        coord.store.insert(_q(callsign="N0XYZ", mode="PD 120"))
        text = export_adif(coord.store.list_qsos(), station=coord.station_info())
        added, skipped = coord.import_qsos(import_adif(text, mode_table=coord.mode_table()))
        assert (added, skipped) == (0, 2)
        assert coord.store.count() == 2

    def test_empty_callsign_drafts_never_dedupe(self, tmp_path: Path) -> None:
        coord = _coordinator(tmp_path)
        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        a = QSO(direction="RX", time_utc=t, mode="Martin M1")
        b = QSO(direction="RX", time_utc=t, mode="Martin M1")
        added, skipped = coord.import_qsos([a, b])
        assert (added, skipped) == (2, 0)
