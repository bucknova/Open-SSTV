# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.templates.filename``.

Covers:

* ``sanitize_filename_component`` strips Windows/macOS-forbidden chars,
  collapses whitespace, and trims separators.
* ``build_autosave_filename`` round-trips typical patterns to concrete
  paths with the correct extension.
* Collision suffix ``_001``, ``_002``, … appends cleanly when files
  already exist in the target directory.
* Empty-callsign case doesn't produce ugly double-underscore filenames.
* Unknown file formats fall back to PNG rather than silently producing
  unopenable files.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from open_sstv.templates.filename import (
    build_autosave_filename,
    sanitize_filename_component,
)
from open_sstv.templates.tokens import TokenContext


def _ctx(
    *,
    callsign: str = "W0AEZ",
    mode: str = "Scottie-S1",
    direction: str = "RX",
) -> TokenContext:
    return TokenContext(
        callsign=callsign,
        mode=mode,
        direction=direction,  # type: ignore[arg-type]
        now_utc=datetime.datetime(
            2026, 4, 17, 21, 35, 12, tzinfo=datetime.timezone.utc
        ),
    )


# === sanitize_filename_component ===


def test_sanitize_strips_windows_forbidden_chars() -> None:
    """Windows forbids /, \\, :, *, ?, ", <, >, | — all must go."""
    assert sanitize_filename_component('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_sanitize_strips_nul_byte() -> None:
    assert sanitize_filename_component("before\x00after") == "beforeafter"


def test_sanitize_collapses_whitespace_to_underscore() -> None:
    assert sanitize_filename_component("two  spaces") == "two_spaces"


def test_sanitize_collapses_duplicate_separators() -> None:
    """An empty token produces ``a__b`` — collapse to ``a_b``."""
    assert sanitize_filename_component("a__b") == "a_b"
    assert sanitize_filename_component("a--b") == "a-b"


def test_sanitize_trims_edge_separators() -> None:
    assert sanitize_filename_component("_filename_") == "filename"
    assert sanitize_filename_component("--name--") == "name"


def test_sanitize_caps_length_at_200() -> None:
    raw = "a" * 500
    assert len(sanitize_filename_component(raw)) == 200


def test_sanitize_empty_result_falls_back_to_sstv() -> None:
    """All-forbidden input still needs to produce *something* openable."""
    assert sanitize_filename_component("///***???") == "sstv"
    assert sanitize_filename_component("") == "sstv"


def test_sanitize_preserves_dashes_and_underscores() -> None:
    """Typical resolved tokens contain dashes (``Scottie-S1``) and
    underscores as separators — both must survive as-is."""
    assert (
        sanitize_filename_component("2026-04-17_213512_W0AEZ_Scottie-S1")
        == "2026-04-17_213512_W0AEZ_Scottie-S1"
    )


# === build_autosave_filename — happy path ===


def test_build_default_pattern(tmp_path: Path) -> None:
    """Default pattern ``%d_%t_%m`` produces expected sortable filename."""
    path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx())
    assert path == tmp_path / "2026-04-17_213512_Scottie-S1.png"


def test_build_with_callsign(tmp_path: Path) -> None:
    path = build_autosave_filename("%d_%t_%c_%m", tmp_path, _ctx())
    assert path == tmp_path / "2026-04-17_213512_W0AEZ_Scottie-S1.png"


def test_build_respects_file_format(tmp_path: Path) -> None:
    """Passing ``file_format='jpg'`` yields a .jpg path."""
    path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx(), file_format="jpg")
    assert path.suffix == ".jpg"


def test_build_accepts_uppercase_format(tmp_path: Path) -> None:
    """File format is case-insensitive at the boundary."""
    path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx(), file_format="PNG")
    assert path.suffix == ".png"


def test_build_unknown_format_falls_back_to_png(tmp_path: Path) -> None:
    path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx(), file_format="bmp")
    assert path.suffix == ".png"


# === build_autosave_filename — empty tokens ===


def test_build_empty_callsign_collapses_separators(tmp_path: Path) -> None:
    """Listening-only op with no callsign: the pattern ``%d_%t_%c_%m``
    must not produce ``2026-04-17_213512__Scottie-S1.png`` with the
    awkward double-underscore."""
    path = build_autosave_filename(
        "%d_%t_%c_%m", tmp_path, _ctx(callsign="")
    )
    assert path == tmp_path / "2026-04-17_213512_Scottie-S1.png"


def test_build_direction_token(tmp_path: Path) -> None:
    """``%rx_tx`` resolves to literal RX or TX."""
    rx_path = build_autosave_filename(
        "%rx_tx_%d_%t_%m", tmp_path, _ctx(direction="RX")
    )
    tx_path = build_autosave_filename(
        "%rx_tx_%d_%t_%m", tmp_path, _ctx(direction="TX")
    )
    assert rx_path.name.startswith("RX_")
    assert tx_path.name.startswith("TX_")


# === build_autosave_filename — collision resolution ===


def test_build_first_collision_gets_001_suffix(tmp_path: Path) -> None:
    """If the target exists, second call appends ``_001``."""
    first = build_autosave_filename("%d_%t_%m", tmp_path, _ctx())
    first.touch()

    second = build_autosave_filename("%d_%t_%m", tmp_path, _ctx())
    assert second.name == "2026-04-17_213512_Scottie-S1_001.png"


def test_build_second_collision_gets_002_suffix(tmp_path: Path) -> None:
    """Third call after two existing files appends ``_002``."""
    (tmp_path / "2026-04-17_213512_Scottie-S1.png").touch()
    (tmp_path / "2026-04-17_213512_Scottie-S1_001.png").touch()

    path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx())
    assert path.name == "2026-04-17_213512_Scottie-S1_002.png"


def test_build_collision_does_not_overwrite_user_data(tmp_path: Path) -> None:
    """Repeated calls must yield distinct paths — never the same one."""
    paths: list[Path] = []
    for _ in range(5):
        path = build_autosave_filename("%d_%t_%m", tmp_path, _ctx())
        path.touch()
        paths.append(path)
    # All five paths distinct, all five on disk.
    assert len(set(paths)) == 5
    for p in paths:
        assert p.exists()


def test_build_compact_timestamp_pattern(tmp_path: Path) -> None:
    """The 'Compact' preset pattern resolves to a Unix-epoch filename."""
    path = build_autosave_filename("%ts_%m", tmp_path, _ctx())
    # 2026-04-17 21:35:12 UTC == 1776462912
    expected_ts = int(_ctx().now_utc.timestamp())
    assert path == tmp_path / f"{expected_ts}_Scottie-S1.png"


@pytest.mark.parametrize(
    "pattern,expected_stem_template",
    [
        # MMSSTV-style compact — ``{ts}`` is a format-string placeholder that
        # expands to the Unix-epoch value for the fixture clock.  Computing
        # the expected value at test time (rather than hard-coding) avoids
        # bit-rot if the fixture time changes and keeps the test focused on
        # "the token expands correctly", not on "is this specific integer right".
        ("%m_%ts", "Scottie-S1_{ts}"),
        # Callsign prefix (ham-radio folder organisation)
        ("%c_%d_%t_%m", "W0AEZ_2026-04-17_213512_Scottie-S1"),
        # Minimal debug pattern
        ("%m", "Scottie-S1"),
    ],
)
def test_build_various_patterns(
    tmp_path: Path, pattern: str, expected_stem_template: str
) -> None:
    ctx = _ctx()
    expected_stem = expected_stem_template.format(ts=int(ctx.now_utc.timestamp()))
    path = build_autosave_filename(pattern, tmp_path, ctx)
    assert path == tmp_path / f"{expected_stem}.png"
