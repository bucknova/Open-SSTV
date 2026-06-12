# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ADIF 3.1.5 reader/writer."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from open_sstv.logbook.adif import (
    AdifParseError,
    datetime_to_qso_date,
    datetime_to_time_on,
    export_adif,
    hz_to_band,
    hz_to_mhz_str,
    import_adif,
    mhz_str_to_hz,
    mode_to_submode,
    parse_qso_date_time,
    submode_to_mode,
)
from open_sstv.logbook.model import QSO, StationInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KNOWN_MODES = (
    "Martin M1", "Martin M2", "Martin M3", "Martin M4",
    "Scottie 1", "Scottie 2", "Scottie DX",
    "Robot 36", "Robot 72",
    "PD 50", "PD 90", "PD 120", "PD 160", "PD 180", "PD 240", "PD 290",
)


def _q(
    *,
    direction: str = "TX",
    callsign: str = "W0AEZ",
    mode: str = "Martin M1",
    freq_hz: int | None = 14_230_000,
    when: datetime | None = None,
    rsv_sent: str = "595",
    rsv_received: str = "589",
    name: str = "",
    qth: str = "",
    grid: str = "",
    comment: str = "",
) -> QSO:
    return QSO(
        direction=direction,  # type: ignore[arg-type]
        callsign=callsign,
        time_utc=when or datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC),
        mode=mode,
        frequency_hz=freq_hz,
        rsv_sent=rsv_sent,
        rsv_received=rsv_received,
        name=name,
        qth=qth,
        grid=grid,
        comment=comment,
    )


# ---------------------------------------------------------------------------
# Mode ↔ Submode conversion
# ---------------------------------------------------------------------------


class TestModeSubmode:
    def test_mode_to_submode_strips_spaces(self) -> None:
        assert mode_to_submode("Martin M1") == "MartinM1"
        assert mode_to_submode("Scottie DX") == "ScottieDX"
        assert mode_to_submode("PD 120") == "PD120"

    def test_mode_to_submode_strips_dashes(self) -> None:
        assert mode_to_submode("Wraase SC2-180") == "WraaseSC2180"

    def test_submode_to_mode_with_table(self) -> None:
        assert submode_to_mode("MartinM1", table=_KNOWN_MODES) == "Martin M1"
        assert submode_to_mode("martinm1", table=_KNOWN_MODES) == "Martin M1"
        assert submode_to_mode("MARTINM1", table=_KNOWN_MODES) == "Martin M1"

    def test_submode_to_mode_unknown_passes_through(self) -> None:
        assert submode_to_mode("CustomMode", table=_KNOWN_MODES) == "CustomMode"

    def test_submode_to_mode_no_table(self) -> None:
        assert submode_to_mode("MartinM1") == "MartinM1"


# ---------------------------------------------------------------------------
# Band derivation
# ---------------------------------------------------------------------------


class TestBandDerivation:
    @pytest.mark.parametrize(
        "freq_hz,expected_band",
        [
            (1_840_000, "160m"),
            (3_590_000, "80m"),
            (7_171_000, "40m"),
            (10_140_000, "30m"),
            (14_230_000, "20m"),  # SSTV calling freq
            (18_157_500, "17m"),
            (21_340_000, "15m"),
            (24_975_000, "12m"),
            (28_680_000, "10m"),
            (50_510_000, "6m"),
            (144_500_000, "2m"),
            (432_500_000, "70cm"),
        ],
    )
    def test_known_bands(self, freq_hz: int, expected_band: str) -> None:
        assert hz_to_band(freq_hz) == expected_band

    def test_out_of_band_returns_none(self) -> None:
        assert hz_to_band(100_000_000) is None  # gap between 6m and 2m

    def test_none_freq_returns_none(self) -> None:
        assert hz_to_band(None) is None

    def test_zero_freq_returns_none(self) -> None:
        assert hz_to_band(0) is None


class TestFreqFormatting:
    def test_hz_to_mhz_str_precision(self) -> None:
        assert hz_to_mhz_str(14_230_000) == "14.230000"

    def test_hz_to_mhz_str_none(self) -> None:
        assert hz_to_mhz_str(None) is None
        assert hz_to_mhz_str(0) is None

    def test_mhz_str_to_hz_roundtrip(self) -> None:
        assert mhz_str_to_hz("14.230000") == 14_230_000

    def test_mhz_str_to_hz_invalid(self) -> None:
        assert mhz_str_to_hz("not a number") is None
        assert mhz_str_to_hz("") is None


# ---------------------------------------------------------------------------
# Date / time parsing
# ---------------------------------------------------------------------------


class TestDateTimeParsing:
    def test_qso_date_format(self) -> None:
        dt = datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)
        assert datetime_to_qso_date(dt) == "20260528"

    def test_time_on_format(self) -> None:
        dt = datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)
        assert datetime_to_time_on(dt) == "173045"

    def test_parse_hhmm(self) -> None:
        dt = parse_qso_date_time("20260528", "1730")
        assert dt == datetime(2026, 5, 28, 17, 30, 0, tzinfo=UTC)

    def test_parse_hhmmss(self) -> None:
        dt = parse_qso_date_time("20260528", "173045")
        assert dt == datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)

    def test_parse_bad_date(self) -> None:
        with pytest.raises(AdifParseError, match="QSO_DATE"):
            parse_qso_date_time("notadate", "1730")

    def test_parse_bad_time(self) -> None:
        with pytest.raises(AdifParseError, match="TIME_ON"):
            parse_qso_date_time("20260528", "abc")

    def test_parse_handles_whitespace(self) -> None:
        dt = parse_qso_date_time("  20260528 ", " 173045 ")
        assert dt == datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_header_has_adif_ver(self) -> None:
        text = export_adif([])
        assert "<ADIF_VER:5>3.1.5" in text
        assert "<EOH>" in text

    def test_header_has_program_id(self) -> None:
        text = export_adif([])
        assert "<PROGRAMID:9>Open-SSTV" in text

    def test_header_includes_program_version_when_supplied(self) -> None:
        text = export_adif([], program_version="0.4.0")
        assert "<PROGRAMVERSION:5>0.4.0" in text

    def test_single_qso_basics(self) -> None:
        text = export_adif([_q()])
        assert "<CALL:5>W0AEZ" in text
        assert "<QSO_DATE:8>20260528" in text
        assert "<TIME_ON:6>173045" in text
        assert "<MODE:4>SSTV" in text
        assert "<SUBMODE:8>MartinM1" in text
        assert "<BAND:3>20m" in text
        assert "<FREQ:9>14.230000" in text
        assert "<RST_SENT:3>595" in text
        assert "<RST_RCVD:3>589" in text
        assert "<EOR>" in text

    def test_skips_draft_by_default(self) -> None:
        draft = _q(callsign="")
        text = export_adif([draft])
        # Header EOH must still be present; record EOR should NOT
        assert "<EOH>" in text
        assert "<EOR>" not in text

    def test_includes_draft_when_flag_set(self) -> None:
        draft = _q(callsign="")
        text = export_adif([draft], include_drafts=True)
        # Now there's a record, even though it has no CALL field.
        assert "<EOR>" in text

    def test_rx_direction_gets_app_tag(self) -> None:
        text = export_adif([_q(direction="RX", callsign="K1ABC")])
        assert "APP_OPENSSTV_DIRECTION" in text.upper()

    def test_tx_direction_omits_app_tag(self) -> None:
        text = export_adif([_q(direction="TX", callsign="K1ABC")])
        assert "APP_OPENSSTV_DIRECTION" not in text.upper()

    def test_station_info_included(self) -> None:
        station = StationInfo(callsign="W0AEZ", grid="EM48", qth="St Louis", name="Kevin")
        text = export_adif([_q()], station=station)
        assert "<STATION_CALLSIGN:5>W0AEZ" in text
        assert "<MY_GRIDSQUARE:4>EM48" in text
        assert "<MY_CITY:8>St Louis" in text
        assert "<OPERATOR:5>Kevin" in text

    def test_empty_fields_omitted(self) -> None:
        # name/qth/grid/comment are blank — corresponding tags should NOT appear
        text = export_adif([_q(name="", qth="", grid="", comment="")])
        assert "<NAME:" not in text
        assert "<QTH:" not in text
        assert "<GRIDSQUARE:" not in text
        assert "<COMMENT:" not in text

    def test_unicode_byte_length_correct(self) -> None:
        # "日本語" = 3 chars, 9 bytes UTF-8
        text = export_adif([_q(comment="日本語")])
        assert "<COMMENT:9>日本語" in text

    def test_none_freq_omits_band_and_freq(self) -> None:
        text = export_adif([_q(freq_hz=None)])
        assert "<BAND:" not in text
        assert "<FREQ:" not in text

    def test_multiple_records(self) -> None:
        qsos = [_q(callsign="W0AEZ"), _q(callsign="K1ABC"), _q(callsign="N0CALL")]
        text = export_adif(qsos)
        assert text.count("<EOR>") == 3


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class TestImport:
    def test_minimum_required_fields(self) -> None:
        text = "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        qsos = import_adif(text)
        assert len(qsos) == 1
        q = qsos[0]
        assert q.callsign == "W0AEZ"
        assert q.time_utc == datetime(2026, 5, 28, 17, 30, 45, tzinfo=UTC)

    def test_skip_record_missing_call(self) -> None:
        text = "<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        assert import_adif(text) == []

    def test_skip_record_missing_date(self) -> None:
        text = "<CALL:5>W0AEZ<TIME_ON:6>173045<EOR>"
        assert import_adif(text) == []

    def test_lenient_case_in_tags(self) -> None:
        text = "<call:5>W0AEZ<Qso_Date:8>20260528<TIME_ON:6>173045<EOR>"
        qsos = import_adif(text)
        assert len(qsos) == 1
        assert qsos[0].callsign == "W0AEZ"

    def test_lenient_extra_whitespace(self) -> None:
        text = """
        <CALL:5>W0AEZ
        <QSO_DATE:8>20260528
        <TIME_ON:6>173045
        <EOR>
        """
        qsos = import_adif(text)
        assert len(qsos) == 1

    def test_skip_header_section(self) -> None:
        text = (
            "Comment header\n"
            "<ADIF_VER:5>3.1.5<EOH>\n"
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        )
        qsos = import_adif(text)
        assert len(qsos) == 1

    def test_no_eoh_treats_whole_doc_as_records(self) -> None:
        text = "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        qsos = import_adif(text)
        assert len(qsos) == 1

    def test_unknown_tags_ignored(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<FOOBAR:3>xyz<WHATEVER:4>1234<EOR>"
        )
        qsos = import_adif(text)
        assert len(qsos) == 1

    def test_submode_normalized_via_table(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<MODE:4>SSTV<SUBMODE:8>MartinM1<EOR>"
        )
        qsos = import_adif(text, mode_table=_KNOWN_MODES)
        assert qsos[0].mode == "Martin M1"

    def test_app_direction_rx(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<APP_OPENSSTV_DIRECTION:2>RX<EOR>"
        )
        qsos = import_adif(text)
        assert qsos[0].direction == "RX"

    def test_app_direction_defaults_to_tx(self) -> None:
        text = "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        qsos = import_adif(text)
        assert qsos[0].direction == "TX"

    def test_invalid_direction_defaults_to_tx(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<APP_OPENSSTV_DIRECTION:9>SIDEWAYS<EOR>"
        )
        qsos = import_adif(text)
        assert qsos[0].direction == "TX"

    def test_freq_parsed(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<FREQ:9>14.230000<EOR>"
        )
        qsos = import_adif(text)
        assert qsos[0].frequency_hz == 14_230_000

    def test_unicode_value_byte_length(self) -> None:
        # 9 bytes for "日本語"
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
            "<COMMENT:9>日本語<EOR>"
        )
        qsos = import_adif(text)
        assert qsos[0].comment == "日本語"

    def test_bytes_input_accepted(self) -> None:
        data = b"<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        qsos = import_adif(data)
        assert len(qsos) == 1

    def test_multiple_records(self) -> None:
        text = (
            "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
            "<CALL:5>K1ABC<QSO_DATE:8>20260528<TIME_ON:6>180000<EOR>"
            "<CALL:6>N0CALL<QSO_DATE:8>20260528<TIME_ON:6>183000<EOR>"
        )
        qsos = import_adif(text)
        assert len(qsos) == 3

    def test_trailing_record_without_eor(self) -> None:
        text = "<CALL:5>W0AEZ<QSO_DATE:8>20260528<TIME_ON:6>173045"
        qsos = import_adif(text)
        # Should still be picked up.
        assert len(qsos) == 1

    def test_malformed_length_skipped(self) -> None:
        # Length=abc is malformed; should be skipped, but the rest of the
        # record (CALL, QSO_DATE, TIME_ON) still parses.
        text = (
            "<CALL:5>W0AEZ<JUNK:abc>xyz"
            "<QSO_DATE:8>20260528<TIME_ON:6>173045<EOR>"
        )
        qsos = import_adif(text)
        assert len(qsos) == 1
        assert qsos[0].callsign == "W0AEZ"


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_basic_roundtrip(self) -> None:
        original = [
            _q(callsign="W0AEZ", mode="Martin M1"),
            _q(callsign="K1ABC", mode="Scottie 1", direction="RX"),
            _q(callsign="N0CALL", mode="PD 120", comment="great signal"),
        ]
        text = export_adif(original)
        loaded = import_adif(text, mode_table=_KNOWN_MODES)
        assert len(loaded) == 3
        for orig, got in zip(original, loaded, strict=True):
            assert got.callsign == orig.callsign
            assert got.time_utc == orig.time_utc
            assert got.mode == orig.mode
            assert got.frequency_hz == orig.frequency_hz
            assert got.direction == orig.direction
            assert got.comment == orig.comment

    def test_roundtrip_unicode(self) -> None:
        original = _q(callsign="W0AEZ", name="アリス", qth="東京", comment="日本語 test")
        text = export_adif([original])
        loaded = import_adif(text)
        assert loaded[0].name == "アリス"
        assert loaded[0].qth == "東京"
        assert loaded[0].comment == "日本語 test"

    def test_roundtrip_no_freq(self) -> None:
        original = _q(callsign="W0AEZ", freq_hz=None)
        text = export_adif([original])
        loaded = import_adif(text)
        assert loaded[0].frequency_hz is None

    def test_roundtrip_preserves_rsv(self) -> None:
        original = _q(callsign="W0AEZ", rsv_sent="595", rsv_received="479")
        text = export_adif([original])
        loaded = import_adif(text)
        assert loaded[0].rsv_sent == "595"
        assert loaded[0].rsv_received == "479"


class TestOutOfRangeDateTime:
    """Audit #1: digit-valid but out-of-range dates must skip the one
    record, not abort the whole import."""

    def test_feb_31_raises_adif_parse_error(self) -> None:
        with pytest.raises(AdifParseError):
            parse_qso_date_time("20260231", "120000")

    def test_hour_25_raises_adif_parse_error(self) -> None:
        with pytest.raises(AdifParseError):
            parse_qso_date_time("20260601", "250000")

    def test_one_bad_record_does_not_abort_import(self) -> None:
        doc = (
            "<CALL:5>K1BAD <QSO_DATE:8>20260231 <TIME_ON:6>120000 <EOR>\n"
            "<CALL:6>K9GOOD <QSO_DATE:8>20260601 <TIME_ON:6>120000 "
            "<MODE:4>SSTV <EOR>\n"
        )
        qsos = import_adif(doc)
        assert [q.callsign for q in qsos] == ["K9GOOD"]
