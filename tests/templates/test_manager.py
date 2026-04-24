# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the template manager (list, load, save, delete, starter pack)."""
from __future__ import annotations

from pathlib import Path

import pytest

from open_sstv.templates.manager import (
    STARTER_TEMPLATE_FILENAMES,
    delete,
    get_templates_by_role,
    install_starter_pack,
    list_templates,
    load_by_path,
    save,
    starter_pack_installed,
)
from open_sstv.templates.model import PhotoLayer, RectLayer, Template, TextLayer
from open_sstv.templates.toml_io import load_template, save_template


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_template(name: str = "Test Template", role: str = "cq") -> Template:
    return Template(
        name=name,
        role=role,
        description="test",
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            TextLayer(
                id="text",
                text_raw="%c",
                anchor="BC",
                font_family="DejaVu Sans Bold",
                font_size_pct=8.0,
                fill=(255, 255, 255, 255),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# starter_pack_installed
# ---------------------------------------------------------------------------


class TestStarterPackInstalled:
    def test_false_when_dir_missing(self, tmp_path: Path) -> None:
        assert starter_pack_installed(tmp_path / "nonexistent") is False

    def test_false_when_dir_empty(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        assert starter_pack_installed(tdir) is False

    def test_true_when_toml_present(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "foo.toml").write_text("[template]\nname='x'\n")
        assert starter_pack_installed(tdir) is True

    def test_ignores_non_toml_files(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "readme.txt").write_text("hello")
        assert starter_pack_installed(tdir) is False


# ---------------------------------------------------------------------------
# install_starter_pack
# ---------------------------------------------------------------------------


class TestInstallStarterPack:
    def test_creates_dir_and_writes_all(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        written = install_starter_pack(tdir)
        assert tdir.is_dir()
        assert len(written) == len(STARTER_TEMPLATE_FILENAMES)
        for fname in STARTER_TEMPLATE_FILENAMES:
            assert (tdir / fname).exists()

    def test_returns_written_paths(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        written = install_starter_pack(tdir)
        assert all(isinstance(p, Path) for p in written)
        assert all(p.parent == tdir for p in written)

    def test_does_not_overwrite_by_default(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        fname = STARTER_TEMPLATE_FILENAMES[0]
        sentinel = b"CUSTOM"
        (tdir / fname).write_bytes(sentinel)
        written = install_starter_pack(tdir)
        # existing file should not be in written list
        written_names = [p.name for p in written]
        assert fname not in written_names
        # content unchanged
        assert (tdir / fname).read_bytes() == sentinel

    def test_overwrites_when_flag_set(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        fname = STARTER_TEMPLATE_FILENAMES[0]
        (tdir / fname).write_bytes(b"CUSTOM")
        written = install_starter_pack(tdir, overwrite=True)
        written_names = [p.name for p in written]
        assert fname in written_names
        assert (tdir / fname).read_bytes() != b"CUSTOM"

    def test_all_installed_templates_loadable(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        install_starter_pack(tdir)
        for fname in STARTER_TEMPLATE_FILENAMES:
            t = load_template(tdir / fname)
            assert t.name  # non-empty name
            assert t.role in ("cq", "reply", "closing", "custom")

    def test_second_install_writes_nothing_by_default(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        install_starter_pack(tdir)
        written2 = install_starter_pack(tdir)
        assert written2 == []


# ---------------------------------------------------------------------------
# list_templates
# ---------------------------------------------------------------------------


class TestListTemplates:
    def test_empty_when_dir_missing(self, tmp_path: Path) -> None:
        assert list_templates(tmp_path / "gone") == []

    def test_empty_when_dir_empty(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        assert list_templates(tdir) == []

    def test_returns_name_role_path_tuples(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        t = _make_template("Alpha", "cq")
        save_template(t, tdir / "alpha.toml")
        results = list_templates(tdir)
        assert len(results) == 1
        name, role, path = results[0]
        assert name == "Alpha"
        assert role == "cq"
        assert path == tdir / "alpha.toml"

    def test_sorted_by_filename(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        for letter in ("charlie", "alpha", "bravo"):
            save_template(_make_template(letter.capitalize()), tdir / f"{letter}.toml")
        names = [n for n, _, _ in list_templates(tdir)]
        assert names == ["Alpha", "Bravo", "Charlie"]

    def test_skips_corrupt_file(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        (tdir / "bad.toml").write_text("NOT VALID TOML !!!")
        save_template(_make_template("Good"), tdir / "good.toml")
        results = list_templates(tdir)
        assert len(results) == 1
        assert results[0][0] == "Good"

    def test_lists_starter_templates(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        install_starter_pack(tdir)
        results = list_templates(tdir)
        assert len(results) == len(STARTER_TEMPLATE_FILENAMES)


# ---------------------------------------------------------------------------
# load_by_path
# ---------------------------------------------------------------------------


class TestLoadByPath:
    def test_loads_valid_template(self, tmp_path: Path) -> None:
        t = _make_template("LoadMe", "reply")
        p = tmp_path / "load_me.toml"
        save_template(t, p)
        loaded = load_by_path(p)
        assert loaded is not None
        assert loaded.name == "LoadMe"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert load_by_path(tmp_path / "gone.toml") is None

    def test_returns_none_for_corrupt_file(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.toml"
        p.write_text("NOT VALID TOML !!!")
        assert load_by_path(p) is None

    def test_returns_none_for_future_schema(self, tmp_path: Path) -> None:
        p = tmp_path / "future.toml"
        p.write_text("[template]\nname='x'\nrole='cq'\nschema_version=999\n")
        assert load_by_path(p) is None


# ---------------------------------------------------------------------------
# get_templates_by_role
# ---------------------------------------------------------------------------


class TestGetTemplatesByRole:
    def test_filters_by_role(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        save_template(_make_template("CQ1", "cq"), tdir / "cq1.toml")
        save_template(_make_template("CQ2", "cq"), tdir / "cq2.toml")
        save_template(_make_template("Reply", "reply"), tdir / "reply.toml")
        results = get_templates_by_role("cq", tdir)
        assert len(results) == 2
        assert all(t.role == "cq" for t in results)

    def test_empty_when_no_match(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        save_template(_make_template("CQ1", "cq"), tdir / "cq1.toml")
        assert get_templates_by_role("closing", tdir) == []

    def test_returns_template_objects(self, tmp_path: Path) -> None:
        tdir = tmp_path / "templates"
        tdir.mkdir()
        save_template(_make_template("T", "reply"), tdir / "t.toml")
        results = get_templates_by_role("reply", tdir)
        assert all(isinstance(t, Template) for t in results)


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


class TestSave:
    def test_derives_filename_from_name(self, tmp_path: Path) -> None:
        t = _make_template("My Cool Template", "cq")
        path = save(t, tmp_path)
        assert path.name == "my_cool_template.toml"
        assert path.exists()

    def test_explicit_filename(self, tmp_path: Path) -> None:
        t = _make_template("Whatever", "cq")
        path = save(t, tmp_path, filename="custom_name.toml")
        assert path.name == "custom_name.toml"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        t1 = _make_template("Original", "cq")
        path = save(t1, tmp_path)
        t2 = _make_template("Updated", "reply")
        save(t2, tmp_path, filename=path.name)
        loaded = load_template(path)
        assert loaded.name == "Updated"

    def test_returns_path(self, tmp_path: Path) -> None:
        t = _make_template()
        result = save(t, tmp_path)
        assert isinstance(result, Path)
        assert result.exists()

    def test_special_chars_stripped_from_filename(self, tmp_path: Path) -> None:
        t = _make_template("Hello/World! 2024", "cq")
        path = save(t, tmp_path)
        assert "/" not in path.name
        assert "!" not in path.name
        assert path.exists()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_removes_file(self, tmp_path: Path) -> None:
        p = tmp_path / "tpl.toml"
        save_template(_make_template(), p)
        assert p.exists()
        delete(p)
        assert not p.exists()

    def test_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            delete(tmp_path / "nonexistent.toml")
