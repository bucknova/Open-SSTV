# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the v0.2 → v0.3 migration logic.

Covers:
- Already-populated templates dir → "already_populated", nothing changed.
- Fresh install (no v0.2 file) → "starter_pack_installed", 8 templates written.
- v0.2 default texts → starter pack installed (not legacy migration).
- Custom v0.2 templates → "legacy_migrated:N", token translation applied.
- Corrupt v0.2 templates.toml → falls through to starter pack.
- Multiple v0.2 templates with mixed default/custom entries.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from open_sstv.templates.manager import STARTER_TEMPLATE_FILENAMES, starter_pack_installed
from open_sstv.templates.migration import _V2_DEFAULT_TEXTS, run_migration
from open_sstv.templates.toml_io import load_template


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V2_DEFAULT_TEXT = next(iter(_V2_DEFAULT_TEXTS))  # any one default text


def _write_v2_templates(config_dir: Path, entries: list[dict]) -> Path:
    """Write a v0.2-style templates.toml with the given overlay entries.

    Each entry: {"name": str, "overlays": [{"text": str}, ...]}
    """
    lines = []
    for entry in entries:
        name = entry.get("name", "unnamed")
        lines.append(f'[[template]]')
        lines.append(f'name = {name!r}')
        for ov in entry.get("overlays", []):
            lines.append(f'[[template.overlay]]')
            lines.append(f'text = {ov["text"]!r}')
    path = config_dir / "templates.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Already-populated
# ---------------------------------------------------------------------------


class TestAlreadyPopulated:
    def test_returns_already_populated(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "existing.toml").write_text("[template]\nname='x'\n")
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "already_populated"

    def test_does_not_modify_existing_templates(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        sentinel = b"SENTINEL_CONTENT"
        p = tdir / "existing.toml"
        p.write_bytes(sentinel)
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert p.read_bytes() == sentinel

    def test_does_not_add_files(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "only.toml").write_text("[template]\nname='x'\n")
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert list(tdir.glob("*.toml")) == [tdir / "only.toml"]


# ---------------------------------------------------------------------------
# Fresh install (no v0.2 file)
# ---------------------------------------------------------------------------


class TestFreshInstall:
    def test_installs_starter_pack(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "starter_pack_installed"

    def test_creates_templates_dir(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        assert not tdir.exists()
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert tdir.is_dir()

    def test_all_starter_templates_written(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        for fname in STARTER_TEMPLATE_FILENAMES:
            assert (tdir / fname).exists(), f"Missing starter: {fname}"

    def test_all_starter_templates_loadable(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        for fname in STARTER_TEMPLATE_FILENAMES:
            t = load_template(tdir / fname)
            assert t.name


# ---------------------------------------------------------------------------
# v0.2 default texts → starter pack (not legacy migration)
# ---------------------------------------------------------------------------


class TestV2DefaultTexts:
    def test_default_texts_use_starter_pack(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "CQ",
                "overlays": [{"text": "CQ CQ CQ DE {mycall} {mycall} K"}],
            },
            {
                "name": "73",
                "overlays": [{"text": "{theircall} 73 DE {mycall} SK"}],
            },
        ])
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "starter_pack_installed"

    def test_all_v2_default_texts_trigger_starter(self, tmp_path: Path) -> None:
        overlays = [{"text": t} for t in _V2_DEFAULT_TEXTS]
        _write_v2_templates(tmp_path, [{"name": "All Defaults", "overlays": overlays}])
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "starter_pack_installed"


# ---------------------------------------------------------------------------
# Custom v0.2 templates → legacy migration
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def test_returns_legacy_migrated_count(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "CQ", "overlays": [{"text": "W0AEZ SSTV DE {mycall}"}]},
            {"name": "73", "overlays": [{"text": "{theircall} 73 DE {mycall} SK W0AEZ"}]},
        ])
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "legacy_migrated:2"

    def test_writes_toml_files(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "My CQ", "overlays": [{"text": "MY CUSTOM CQ DE {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        tomls = list(tdir.glob("*.toml"))
        # 1 migrated + 8 starter pack files
        assert len(tomls) == 1 + len(STARTER_TEMPLATE_FILENAMES)

    def test_migrated_template_loadable(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "Custom CQ", "overlays": [{"text": "W0AEZ ON THE AIR {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "custom_cq.toml")
        assert t.name

    def test_token_translation_mycall(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "CQ", "overlays": [{"text": "CQ DE {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "cq.toml")
        # Find the TextLayer and check token was translated
        from open_sstv.templates.model import TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers, "No TextLayer in migrated template"
        assert "%c" in text_layers[0].text_raw
        assert "{mycall}" not in text_layers[0].text_raw

    def test_token_translation_theircall(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "73", "overlays": [{"text": "{theircall} 73 73 CUSTOM DE {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "73.toml")
        from open_sstv.templates.model import TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers
        text = text_layers[0].text_raw
        assert "%o" in text
        assert "%c" in text
        assert "{theircall}" not in text
        assert "{mycall}" not in text

    def test_token_translation_all_tokens(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Full",
                "overlays": [{"text": "{mycall} {theircall} {rst} {date} {time}"}],
            },
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "full.toml")
        from open_sstv.templates.model import TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers
        text = text_layers[0].text_raw
        for old in ("{mycall}", "{theircall}", "{rst}", "{date}", "{time}"):
            assert old not in text, f"Old token {old!r} not translated"
        for new in ("%c", "%o", "%r", "%d", "%t"):
            assert new in text, f"New token {new!r} missing"

    def test_migrated_template_keeps_original_name(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "My Template", "overlays": [{"text": "W0AEZ CUSTOM"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "my_template.toml")
        assert t.name == "My Template"
        assert "(migrated)" not in t.name

    def test_starter_pack_installed_after_legacy_migration(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "Custom CQ", "overlays": [{"text": "W0AEZ CUSTOM {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        for fname in STARTER_TEMPLATE_FILENAMES:
            assert (tdir / fname).exists(), f"Starter template missing after legacy migration: {fname}"

    def test_mixed_default_and_custom_only_migrates_custom(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Default CQ",
                "overlays": [{"text": "CQ CQ CQ DE {mycall} {mycall} K"}],
            },
            {
                "name": "Custom",
                "overlays": [{"text": "MY CUSTOM TEXT {mycall}"}],
            },
        ])
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "legacy_migrated:1"

    def test_creates_templates_dir_if_missing(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {"name": "CQ", "overlays": [{"text": "CUSTOM {mycall}"}]},
        ])
        tdir = tmp_path / "templates"
        assert not tdir.exists()
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert tdir.is_dir()


# ---------------------------------------------------------------------------
# Corrupt v0.2 templates.toml
# ---------------------------------------------------------------------------


class TestCorruptV2File:
    def test_corrupt_falls_through_to_starter_pack(self, tmp_path: Path) -> None:
        p = tmp_path / "templates.toml"
        p.write_text("THIS IS NOT VALID TOML !!!", encoding="utf-8")
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "starter_pack_installed"

    def test_empty_v2_file_falls_through_to_starter_pack(self, tmp_path: Path) -> None:
        p = tmp_path / "templates.toml"
        p.write_text("", encoding="utf-8")
        tdir = tmp_path / "templates"
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "starter_pack_installed"
