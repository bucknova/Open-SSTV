# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the v0.3 font registry and variable-font weight handling.

The renderer's ``_load_font`` snaps the Bold weight axis on shipped
variable fonts (Orbitron, Oswald, Exo 2) when the family name carries
Bold intent.  These tests pin that behavior plus the registry coverage
for the v0.3.x Tier-1 additions.
"""
from __future__ import annotations

import PIL.ImageFont
import pytest

from open_sstv.templates.fonts import (
    is_font_available,
    list_available_fonts,
    resolve_font_path,
)
from open_sstv.templates.renderer import _load_font


# ---------------------------------------------------------------------------
# Registry: every Tier-1 family the v0.3.x bundle ships must resolve
# ---------------------------------------------------------------------------


SHIPPED_BOLD_VARIABLE = ("Orbitron Bold", "Oswald Bold", "Exo 2 Bold")
SHIPPED_REGULAR = ("Bebas Neue", "Share Tech Mono")
ALL_NEW_FAMILIES = SHIPPED_BOLD_VARIABLE + SHIPPED_REGULAR


class TestShippedFontRegistry:
    @pytest.mark.parametrize("family", ALL_NEW_FAMILIES)
    def test_resolves_to_an_existing_ttf(self, family: str) -> None:
        path = resolve_font_path(family)
        assert path.exists(), f"{family!r} resolved to a missing path: {path}"
        assert path.suffix.lower() == ".ttf"

    @pytest.mark.parametrize("family", ALL_NEW_FAMILIES)
    def test_does_not_fall_back_to_default(self, family: str) -> None:
        """A registered family must report as available — i.e. the lookup
        does not silently fall through to DejaVu Sans Bold."""
        assert is_font_available(family) is True

    def test_list_available_fonts_includes_all_new_families(self) -> None:
        names = list_available_fonts()
        for f in ALL_NEW_FAMILIES:
            assert f in names, f"{f!r} missing from list_available_fonts()"

    def test_existing_tier1_still_listed(self) -> None:
        """Regression: registry expansion must not drop pre-v0.3.x fonts."""
        names = list_available_fonts()
        for f in ("DejaVu Sans Bold", "Inter Bold", "Press Start 2P"):
            assert f in names

    def test_orbitron_alias_resolves_same_file(self) -> None:
        """Both 'Orbitron' and 'Orbitron Bold' point at the variable TTF —
        the weight selection is the renderer's job, not the registry's."""
        assert resolve_font_path("Orbitron") == resolve_font_path("Orbitron Bold")


# ---------------------------------------------------------------------------
# Variable-font weight handling in _load_font
# ---------------------------------------------------------------------------


class TestVariableFontBoldVariation:
    """The renderer must snap the Weight axis to Bold for variable fonts
    whose family name carries Bold intent."""

    @pytest.mark.parametrize("family", SHIPPED_BOLD_VARIABLE)
    def test_bold_load_does_not_raise(self, family: str) -> None:
        font = _load_font(family, 24)
        assert isinstance(font, PIL.ImageFont.FreeTypeFont)

    @pytest.mark.parametrize("family", SHIPPED_BOLD_VARIABLE)
    def test_bold_glyph_is_heavier_than_regular(self, family: str) -> None:
        """Bold glyphs paint more dark pixels than Regular at the same size.
        Compare ink coverage of a known glyph between the Bold-named load
        (which triggers the variation snap) and a non-Bold load (default
        axis = Regular)."""
        bold = _load_font(family, 64)
        # Strip "Bold" from the family name so the renderer leaves the
        # weight axis at default (Regular).
        regular_name = family.removesuffix(" Bold")
        regular = _load_font(regular_name, 64)

        bold_mask = bold.getmask("M")
        regular_mask = regular.getmask("M")

        bold_ink = sum(1 for i in range(len(bold_mask)) if bold_mask[i] > 0)
        regular_ink = sum(
            1 for i in range(len(regular_mask)) if regular_mask[i] > 0
        )

        assert bold_ink > regular_ink, (
            f"{family!r}: Bold ink {bold_ink} not greater than "
            f"Regular ink {regular_ink} — weight axis didn't snap."
        )

    def test_static_bold_font_is_unaffected(self) -> None:
        """A static Bold font (e.g. Inter Bold) has no variation axes; the
        try/except around set_variation_by_name must swallow the OSError
        and return the font unchanged."""
        font = _load_font("Inter Bold", 24)
        assert isinstance(font, PIL.ImageFont.FreeTypeFont)

    def test_non_bold_family_does_not_snap_weight(self) -> None:
        """A family name without 'bold' must NOT call set_variation_by_name —
        otherwise loading 'Bebas Neue' or any custom non-Bold family would
        get unexpectedly mutated."""
        # Bebas Neue is a static font, but the test is about the code path:
        # if 'bold' isn't in the family name, no variation call is attempted.
        # We assert this by spying.  Since the font is static, a real call
        # would raise OSError, which we'd see in failure modes.
        called = []

        original = PIL.ImageFont.FreeTypeFont.set_variation_by_name

        def spy(self, name):  # type: ignore[no-untyped-def]
            called.append(name)
            return original(self, name)

        PIL.ImageFont.FreeTypeFont.set_variation_by_name = spy  # type: ignore[method-assign]
        try:
            _load_font("Bebas Neue", 24)
        finally:
            PIL.ImageFont.FreeTypeFont.set_variation_by_name = original  # type: ignore[method-assign]

        assert called == [], (
            f"Unexpected variation call for non-Bold family: {called!r}"
        )

    def test_bold_family_attempts_variation_snap(self) -> None:
        """Symmetric to above: a Bold-named family DOES trigger the
        variation call (even if the underlying font happens to be static
        and the call no-ops)."""
        called = []

        original = PIL.ImageFont.FreeTypeFont.set_variation_by_name

        def spy(self, name):  # type: ignore[no-untyped-def]
            called.append(name)
            try:
                return original(self, name)
            except OSError:
                raise

        PIL.ImageFont.FreeTypeFont.set_variation_by_name = spy  # type: ignore[method-assign]
        try:
            _load_font("Orbitron Bold", 24)
        finally:
            PIL.ImageFont.FreeTypeFont.set_variation_by_name = original  # type: ignore[method-assign]

        assert called == [b"Bold"], (
            f"Bold-named family did not trigger Bold variation: {called!r}"
        )
