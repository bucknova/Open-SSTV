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

from pathlib import Path

from open_sstv.templates.manager import STARTER_TEMPLATE_FILENAMES
from open_sstv.templates.migration import _V2_DEFAULT_TEXTS, run_migration
from open_sstv.templates.toml_io import load_template

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V2_DEFAULT_TEXT = next(iter(_V2_DEFAULT_TEXTS))  # any one default text


def _write_v2_templates(config_dir: Path, entries: list[dict]) -> Path:
    """Write a v0.2-style templates.toml with the given overlay entries.

    Each entry: {"name": str, "overlays": [{"text": str,
    "color": [r, g, b]?, "position": str?}, ...]} — color/position are
    optional, matching what the v0.2 editor wrote (M5 tests use them).
    """
    lines = []
    for entry in entries:
        name = entry.get("name", "unnamed")
        lines.append('[[template]]')
        lines.append(f'name = {name!r}')
        for ov in entry.get("overlays", []):
            lines.append('[[template.overlay]]')
            lines.append(f'text = {ov["text"]!r}')
            if "color" in ov:
                lines.append(f'color = {list(ov["color"])!r}')
            if "position" in ov:
                lines.append(f'position = {ov["position"]!r}')
    path = config_dir / "templates.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Already-populated
# ---------------------------------------------------------------------------


class TestAlreadyPopulated:
    """v0.3.22: ``starter_pack_installed`` now only treats the dir as
    populated when at least one *real* v0.3 starter filename is present.
    The pre-v0.3.22 tests wrote arbitrary names like ``existing.toml`` /
    ``only.toml`` and expected ``run_migration`` to short-circuit;
    that's no longer the contract.  Use a real starter filename for
    the positive cases."""

    def test_returns_already_populated(self, tmp_path: Path) -> None:
        from open_sstv.templates.manager import STARTER_TEMPLATE_FILENAMES
        tdir = tmp_path / "templates"
        tdir.mkdir()
        # Use a real starter name so the v0.3.22 gate fires correctly.
        (tdir / STARTER_TEMPLATE_FILENAMES[0]).write_text(
            "[template]\nname='cqsstv'\n"
        )
        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        assert result == "already_populated"

    def test_does_not_modify_existing_templates(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        sentinel = b"SENTINEL_CONTENT"
        p = tdir / "existing.toml"
        p.write_bytes(sentinel)
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        # Non-starter file content must be preserved across migration even
        # when the starter pack ends up being installed alongside it.
        assert p.read_bytes() == sentinel

    def test_does_not_add_files(self, tmp_path: Path) -> None:
        """When a real v0.3 starter is already present, no extra files."""
        from open_sstv.templates.manager import STARTER_TEMPLATE_FILENAMES
        tdir = tmp_path / "templates"
        tdir.mkdir()
        present = STARTER_TEMPLATE_FILENAMES[0]
        (tdir / present).write_text("[template]\nname='cqsstv'\n")
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        # Only the one file we put there should still be present (migration
        # treats the dir as already-populated and skips install).
        assert sorted(p.name for p in tdir.glob("*.toml")) == [present]

    def test_stale_non_starter_toml_triggers_install(self, tmp_path: Path) -> None:
        """v0.3.22 regression test: stale cq.toml from a v0.2.x install.

        The pre-v0.3.22 ``starter_pack_installed`` saw the non-starter
        ``.toml`` and skipped the install entirely, so a real user
        upgrading from v0.2.x ended up with zero v0.3 starters in their
        gallery despite the bundled templates shipping correctly.
        Diagnostics zip from 2026-05-28 caught the exact state.
        """
        from open_sstv.templates.manager import STARTER_TEMPLATE_FILENAMES
        tdir = tmp_path / "templates"
        tdir.mkdir()
        # Simulate the user's reported state: a single non-starter .toml.
        (tdir / "cq.toml").write_text("[template]\nname='stale from v0.2'\n")

        result = run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        # Should NOT be "already_populated" — install must run.
        assert result == "starter_pack_installed"
        # All 8 starters now present.
        for fn in STARTER_TEMPLATE_FILENAMES:
            assert (tdir / fn).exists(), f"missing starter: {fn}"
        # And the user's stale cq.toml was left untouched.
        assert (tdir / "cq.toml").exists()


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


class TestV2CustomisationPreserved:
    """M5 (v0.3 audit): the migration must carry the user's v0.2 color
    and named position into the v0.3 TextLayer instead of hardcoding
    white / bottom-center."""

    def test_custom_color_preserved(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Red CQ",
                "overlays": [
                    {"text": "W0AEZ CUSTOM {mycall}", "color": [255, 32, 32]},
                ],
            },
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "red_cq.toml")
        from open_sstv.templates.model import TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers[0].fill == (255, 32, 32, 255)

    def test_custom_position_preserved(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Top Banner",
                "overlays": [
                    {"text": "W0AEZ CUSTOM TOP", "position": "Top Center"},
                ],
            },
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "top_banner.toml")
        from open_sstv.templates.model import RectLayer, TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers[0].anchor == "TC"
        # The dark backing strip is bottom-only; top-anchored text
        # must not get a bottom rect behind nothing.
        assert not any(isinstance(la, RectLayer) for la in t.layers)

    def test_default_position_keeps_backing_strip(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Classic",
                "overlays": [{"text": "W0AEZ CUSTOM CLASSIC"}],
            },
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "classic.toml")
        from open_sstv.templates.model import RectLayer, TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers[0].anchor == "BC"
        assert text_layers[0].fill == (255, 255, 255, 255)
        assert any(isinstance(la, RectLayer) for la in t.layers)

    def test_unknown_position_falls_back_to_bc(self, tmp_path: Path) -> None:
        _write_v2_templates(tmp_path, [
            {
                "name": "Weird",
                "overlays": [
                    {"text": "W0AEZ CUSTOM WEIRD", "position": "Diagonal"},
                ],
            },
        ])
        tdir = tmp_path / "templates"
        run_migration(templates_dir=tdir, user_config_dir=tmp_path)
        t = load_template(tdir / "weird.toml")
        from open_sstv.templates.model import TextLayer
        text_layers = [la for la in t.layers if isinstance(la, TextLayer)]
        assert text_layers[0].anchor == "BC"


class TestReRunSafety:
    """v0.4.1 audit high #8: the legacy migration is one-time and never
    clobbers — re-running (starters deleted, legacy file still present)
    must not revert user edits to migrated templates."""

    def _first_run(self, tmp_path: Path) -> tuple[Path, Path]:
        tdir = tmp_path / "templates"
        cfg = tmp_path / "config"
        cfg.mkdir()
        _write_v2_templates(
            cfg, [{"name": "My Custom", "overlays": [{"text": "CQ CQ de W0AEZ"}]}]
        )
        result = run_migration(templates_dir=tdir, user_config_dir=cfg)
        assert result.startswith("legacy_migrated:")
        return tdir, cfg

    def test_marker_written_after_legacy_migration(self, tmp_path: Path) -> None:
        tdir, _cfg = self._first_run(tmp_path)
        assert (tdir / ".v2_migration_done").exists()

    def test_rerun_after_starter_deletion_keeps_user_edits(
        self, tmp_path: Path
    ) -> None:
        tdir, cfg = self._first_run(tmp_path)
        migrated = tdir / "my_custom.toml"
        assert migrated.exists()
        # User edits their migrated template and curates away starters.
        edited = migrated.read_text().replace("CQ CQ de W0AEZ", "EDITED BY USER")
        migrated.write_text(edited)
        for starter in tdir.glob("*.toml"):
            if starter != migrated:
                starter.unlink()
        # Next launch: gate 1 fails (no starters), but the marker stops
        # the legacy re-migration — edits survive, starters reinstall.
        result = run_migration(templates_dir=tdir, user_config_dir=cfg)
        assert result == "starter_pack_installed"
        assert "EDITED BY USER" in migrated.read_text()

    def test_pre_marker_rerun_never_overwrites_existing_file(
        self, tmp_path: Path
    ) -> None:
        # Users who migrated under pre-v0.4.1 code have no marker: the
        # exists-skip is their protection on the one re-run that writes it.
        tdir, cfg = self._first_run(tmp_path)
        (tdir / ".v2_migration_done").unlink()
        migrated = tdir / "my_custom.toml"
        migrated.write_text(migrated.read_text().replace("CQ CQ de W0AEZ", "KEEP ME"))
        for starter in tdir.glob("*.toml"):
            if starter != migrated:
                starter.unlink()
        result = run_migration(templates_dir=tdir, user_config_dir=cfg)
        assert result.startswith("legacy_migrated:")
        assert "KEEP ME" in migrated.read_text()
        assert (tdir / ".v2_migration_done").exists()  # marker healed
