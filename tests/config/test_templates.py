# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for QSO template data model, persistence, and placeholder resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from open_sstv.config.templates import (
    QSOTemplate,
    QSOTemplateOverlay,
    default_templates,
    load_templates,
    needs_user_input,
    resolve_placeholders,
    save_templates,
)


class TestDefaultTemplates:
    def test_returns_three_templates(self) -> None:
        t = default_templates()
        assert len(t) == 3

    def test_template_names(self) -> None:
        names = [t.name for t in default_templates()]
        assert names == ["CQ", "Exchange", "73"]

    def test_cq_has_one_overlay(self) -> None:
        cq = default_templates()[0]
        assert len(cq.overlays) == 1
        assert "{mycall}" in cq.overlays[0].text

    def test_exchange_has_two_overlays(self) -> None:
        ex = default_templates()[1]
        assert len(ex.overlays) == 2


class TestRoundTrip:
    def test_save_load_preserves_all_fields(self, tmp_path: Path) -> None:
        original = default_templates()
        path = tmp_path / "templates.toml"
        save_templates(original, path)
        loaded = load_templates(path)

        assert len(loaded) == len(original)
        for orig, load in zip(original, loaded):
            assert orig.name == load.name
            assert len(orig.overlays) == len(load.overlays)
            for oo, lo in zip(orig.overlays, load.overlays):
                assert oo.text == lo.text
                assert oo.position == lo.position
                assert oo.size == lo.size
                assert oo.color == lo.color

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        loaded = load_templates(tmp_path / "nonexistent.toml")
        assert len(loaded) == 3
        assert loaded[0].name == "CQ"

    def test_empty_template_list_returns_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "templates.toml"
        save_templates([], path)
        loaded = load_templates(path)
        assert len(loaded) == 3  # falls back to defaults

    def test_custom_template_survives_round_trip(self, tmp_path: Path) -> None:
        custom = [
            QSOTemplate(
                name="Test",
                overlays=[
                    QSOTemplateOverlay(
                        text="Hello {mycall}",
                        position="Top Left",
                        size=18,
                        color=(255, 0, 128),
                    ),
                ],
            ),
        ]
        path = tmp_path / "templates.toml"
        save_templates(custom, path)
        loaded = load_templates(path)
        assert len(loaded) == 1
        assert loaded[0].name == "Test"
        assert loaded[0].overlays[0].color == (255, 0, 128)


class TestResolvePlaceholders:
    def test_all_variables(self) -> None:
        result = resolve_placeholders(
            "{theircall} DE {mycall} UR {rst} {date} {time}",
            mycall="W0AEZ",
            theircall="N0CALL",
            rst="59",
        )
        assert "N0CALL" in result
        assert "W0AEZ" in result
        assert "59" in result
        # date and time should be resolved (not left as placeholders)
        assert "{date}" not in result
        assert "{time}" not in result

    def test_missing_theircall_leaves_empty(self) -> None:
        result = resolve_placeholders("CQ DE {mycall}", mycall="W0AEZ")
        assert result == "CQ DE W0AEZ"

    def test_no_placeholders(self) -> None:
        result = resolve_placeholders("Plain text", mycall="W0AEZ")
        assert result == "Plain text"


class TestNeedsUserInput:
    def test_cq_needs_nothing(self) -> None:
        cq = default_templates()[0]
        assert needs_user_input(cq) == set()

    def test_exchange_needs_theircall_and_rst(self) -> None:
        ex = default_templates()[1]
        assert needs_user_input(ex) == {"theircall", "rst"}

    def test_73_needs_theircall(self) -> None:
        t73 = default_templates()[2]
        assert needs_user_input(t73) == {"theircall"}
