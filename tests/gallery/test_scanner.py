# SPDX-License-Identifier: GPL-3.0-or-later
"""gallery.scanner — filename parsing + directory scan."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PIL import Image

from open_sstv.core.modes import Mode
from open_sstv.gallery.scanner import (
    parse_date,
    parse_mode,
    scan_dir,
    scan_dirs,
)


def _img(path: Path, size: tuple[int, int] = (16, 16)) -> Path:
    Image.new("RGB", size, (10, 20, 30)).save(path)
    return path


class TestParseMode:
    def test_all_22_modes_round_trip(self) -> None:
        # A default-pattern name for every mode must parse back to its value.
        for mode in Mode:
            stem = f"2026-04-17_213512_{mode.value}"
            assert parse_mode(stem) == mode.value, mode.value

    def test_mode_at_stem_start(self) -> None:
        # Custom pattern %m_%d — mode leads.
        assert parse_mode("scottie_s1_2026-04-17") == "scottie_s1"

    def test_longest_match_wins(self) -> None:
        # wraase_sc2_180 must not be truncated to a shorter prefix.
        assert parse_mode("2026-04-17_120000_wraase_sc2_180") == "wraase_sc2_180"

    def test_no_mode_returns_none(self) -> None:
        assert parse_mode("holiday_snapshot_2026") is None
        assert parse_mode("IMG_4823") is None


class TestParseDate:
    def test_default_pattern_date(self) -> None:
        assert parse_date("2026-04-17_213512_robot_36") == date(2026, 4, 17)

    def test_no_date_returns_none(self) -> None:
        assert parse_date("robot_36_snapshot") is None

    def test_invalid_date_digits_return_none(self) -> None:
        # Digits match the shape but aren't a real calendar date.
        assert parse_date("2026-13-40_120000_pd_120") is None


class TestScanDir:
    def test_finds_images_skips_other_files(self, tmp_path: Path) -> None:
        _img(tmp_path / "2026-04-17_213512_scottie_s1.png")
        _img(tmp_path / "2026-04-18_090000_pd_120.jpg")
        (tmp_path / "notes.txt").write_text("not an image")
        (tmp_path / "logbook.db").write_bytes(b"sqlite-ish")
        items = scan_dir(tmp_path)
        names = sorted(i.path.name for i in items)
        assert names == [
            "2026-04-17_213512_scottie_s1.png",
            "2026-04-18_090000_pd_120.jpg",
        ]

    def test_item_carries_parsed_metadata(self, tmp_path: Path) -> None:
        p = _img(tmp_path / "2026-04-17_213512_scottie_s1.png")
        (item,) = scan_dir(tmp_path)
        assert item.path == p
        assert item.parsed_mode == "scottie_s1"
        assert item.parsed_date == date(2026, 4, 17)
        assert item.size_bytes > 0
        assert item.mtime > 0
        assert item.qso is None  # scanner does not touch the logbook

    def test_unlogged_unnamed_image_still_scannable(self, tmp_path: Path) -> None:
        _img(tmp_path / "random.png")
        (item,) = scan_dir(tmp_path)
        assert item.parsed_mode is None
        assert item.parsed_date is None
        # mtime is always the timestamp fallback.
        assert item.timestamp_utc is not None
        assert item.display_mode == "Unknown"

    def test_missing_dir_is_empty_not_error(self, tmp_path: Path) -> None:
        assert scan_dir(tmp_path / "does-not-exist") == []


class TestScanDirs:
    def test_dedupes_overlapping_dirs(self, tmp_path: Path) -> None:
        _img(tmp_path / "a.png")
        # Same directory passed twice → each file once.
        items = scan_dirs([tmp_path, tmp_path])
        assert [i.path.name for i in items] == ["a.png"]

    def test_merges_distinct_dirs(self, tmp_path: Path) -> None:
        d1 = tmp_path / "rx"
        d2 = tmp_path / "extra"
        d1.mkdir()
        d2.mkdir()
        _img(d1 / "one.png")
        _img(d2 / "two.png")
        items = scan_dirs([d1, d2])
        assert sorted(i.path.name for i in items) == ["one.png", "two.png"]
