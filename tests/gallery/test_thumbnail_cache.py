# SPDX-License-Identifier: GPL-3.0-or-later
"""gallery.thumbnail_cache — generation, mtime invalidation, degrade, prune."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from open_sstv.gallery.thumbnail_cache import ThumbnailCache


def _img(path: Path, size: tuple[int, int], color=(120, 60, 30)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


class TestGeneration:
    def test_creates_thumbnail_within_bounds(self, tmp_path: Path) -> None:
        src = _img(tmp_path / "big.png", (640, 480))
        cache = ThumbnailCache(cache_dir=tmp_path / "cache", size=(160, 120))
        thumb = cache.get_or_create(src)
        assert thumb is not None and thumb.is_file()
        with Image.open(thumb) as im:
            assert im.width <= 160 and im.height <= 120
            assert im.width == 160  # 4:3 source fills the 160×120 box

    def test_second_call_hits_cache(self, tmp_path: Path) -> None:
        src = _img(tmp_path / "s.png", (320, 240))
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        first = cache.get_or_create(src)
        mtime1 = first.stat().st_mtime_ns
        second = cache.get_or_create(src)
        assert second == first
        assert second.stat().st_mtime_ns == mtime1  # not regenerated

    def test_source_change_regenerates_under_new_key(self, tmp_path: Path) -> None:
        src = tmp_path / "s.png"
        _img(src, (320, 240))
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        first = cache.get_or_create(src)
        # Rewrite the source with different content + bump mtime.
        _img(src, (320, 240), color=(0, 200, 0))
        os.utime(src, (first.stat().st_mtime + 1000, first.stat().st_mtime + 1000))
        second = cache.get_or_create(src)
        assert second is not None
        assert second != first  # different (path,mtime,size) key


class TestDegrade:
    def test_corrupt_source_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.png"
        bad.write_bytes(b"this is not a PNG")
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        assert cache.get_or_create(bad) is None

    def test_missing_source_returns_none(self, tmp_path: Path) -> None:
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        assert cache.get_or_create(tmp_path / "gone.png") is None


class TestPrune:
    def test_prune_keeps_newest(self, tmp_path: Path) -> None:
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        made: list[Path] = []
        for i in range(5):
            src = _img(tmp_path / f"img{i}.png", (32, 32), color=(i * 10, 0, 0))
            thumb = cache.get_or_create(src)
            assert thumb is not None
            # Stagger mtimes so "newest" is deterministic.
            os.utime(thumb, (1000 + i, 1000 + i))
            made.append(thumb)
        removed = cache.prune(max_files=2)
        assert removed == 3
        surviving = {p.name for p in cache.cache_dir.glob("*.png")}
        assert made[4].name in surviving and made[3].name in surviving
        assert made[0].name not in surviving

    def test_prune_under_cap_is_noop(self, tmp_path: Path) -> None:
        cache = ThumbnailCache(cache_dir=tmp_path / "cache")
        cache.get_or_create(_img(tmp_path / "one.png", (32, 32)))
        assert cache.prune(max_files=100) == 0
