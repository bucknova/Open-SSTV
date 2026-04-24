# SPDX-License-Identifier: GPL-3.0-or-later
"""Font resolution for the v0.3 template compositor.

Resolution order
────────────────
1. User-imported fonts: ``{user_config_dir}/open_sstv/fonts/``
2. Shipped Tier-1 fonts: ``src/open_sstv/assets/fonts/`` (bundled)
3. Fallback: DejaVu Sans Bold (always available from the bundle)

Family names are matched case-insensitively, ignoring internal spacing
differences ("DejaVu Sans Bold" == "dejavusansbold").  This is intentional
— SSTV operators copy templates from forums and the casing is inconsistent.

Missing-font handling
─────────────────────
If a requested family is not found in either search path, the loader
returns the DejaVu Sans Bold path and logs a warning.  Callers that need
to signal a missing font to the user (e.g., the template editor) should
call ``is_font_available(family)`` before rendering.
"""
from __future__ import annotations

import importlib.resources
import logging
import re
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shipped font registry
# ---------------------------------------------------------------------------

# Maps normalised family name → TTF filename inside assets/fonts/.
_SHIPPED_FONTS: dict[str, str] = {
    "dejavusansbold": "DejaVuSans-Bold.ttf",
    "inter": "Inter-Bold.ttf",
    "interbold": "Inter-Bold.ttf",
    "pressstart2p": "PressStart2P-Regular.ttf",
    "pressstart": "PressStart2P-Regular.ttf",
}

_FALLBACK_FAMILY = "dejavusansbold"


def _normalise(family: str) -> str:
    """Lowercase, strip spaces/hyphens/underscores for fuzzy matching."""
    return re.sub(r"[\s\-_]", "", family).lower()


# ---------------------------------------------------------------------------
# Path finders
# ---------------------------------------------------------------------------


def _shipped_fonts_dir() -> Path:
    """Return the absolute path to the bundled assets/fonts directory."""
    # importlib.resources traversal for the installed package.
    anchor = importlib.resources.files("open_sstv") / "assets" / "fonts"
    # Materialise as a filesystem path (works for both editable and wheel installs).
    with importlib.resources.as_file(anchor) as p:
        return Path(p)


def _user_fonts_dir() -> Path | None:
    """Return the user's custom font directory, or None if it doesn't exist."""
    try:
        import platformdirs
        d = Path(platformdirs.user_config_dir("open_sstv")) / "fonts"
        return d if d.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_font_path(
    family: str,
    user_fonts_dir: Path | None = None,
) -> Path:
    """Return the TTF path for *family*, with fallback to DejaVu Sans Bold.

    Parameters
    ----------
    family:
        Font family name as stored in a TextLayer, e.g.
        ``"DejaVu Sans Bold"``, ``"Inter Bold"``, ``"Press Start 2P"``.
    user_fonts_dir:
        Override for the user font search directory.  ``None`` uses
        the platform default (``{user_config_dir}/open_sstv/fonts/``).

    Returns
    -------
    Path
        Absolute path to a TTF file that exists on disk.  Always valid
        (falls back to DejaVu Sans Bold if nothing else matches).
    """
    key = _normalise(family)

    # 1. User-imported fonts (exact filename or normalised family match)
    udir = user_fonts_dir if user_fonts_dir is not None else _user_fonts_dir()
    if udir is not None and udir.is_dir():
        for ttf in udir.glob("*.ttf"):
            if _normalise(ttf.stem) == key:
                return ttf
        for otf in udir.glob("*.otf"):
            if _normalise(otf.stem) == key:
                return otf

    # 2. Shipped Tier-1 fonts
    shipped_dir = _shipped_fonts_dir()
    filename = _SHIPPED_FONTS.get(key)
    if filename:
        candidate = shipped_dir / filename
        if candidate.exists():
            return candidate

    # 3. Fallback: DejaVu Sans Bold
    if key != _FALLBACK_FAMILY:
        _log.warning(
            "Font %r not found — falling back to DejaVu Sans Bold.", family
        )
    fallback = shipped_dir / _SHIPPED_FONTS[_FALLBACK_FAMILY]
    if fallback.exists():
        return fallback

    raise FileNotFoundError(
        f"Fallback font not found at {fallback}. "
        "The Open-SSTV installation may be incomplete."
    )


def is_font_available(
    family: str,
    user_fonts_dir: Path | None = None,
) -> bool:
    """Return True if *family* resolves without falling back to the default."""
    key = _normalise(family)
    if key == _FALLBACK_FAMILY:
        return True

    udir = user_fonts_dir if user_fonts_dir is not None else _user_fonts_dir()
    if udir is not None and udir.is_dir():
        for ttf in udir.glob("*.ttf"):
            if _normalise(ttf.stem) == key:
                return True
        for otf in udir.glob("*.otf"):
            if _normalise(otf.stem) == key:
                return True

    shipped_dir = _shipped_fonts_dir()
    filename = _SHIPPED_FONTS.get(key)
    if filename and (shipped_dir / filename).exists():
        return True

    return False


def list_available_fonts(user_fonts_dir: Path | None = None) -> list[str]:
    """Return display names of all fonts available for template use."""
    names: list[str] = []

    # Shipped fonts
    for display in [
        "DejaVu Sans Bold",
        "Inter Bold",
        "Press Start 2P",
    ]:
        if is_font_available(display, user_fonts_dir):
            names.append(display)

    # User-imported fonts
    udir = user_fonts_dir if user_fonts_dir is not None else _user_fonts_dir()
    if udir is not None and udir.is_dir():
        for ttf in sorted(udir.glob("*.ttf")):
            display = ttf.stem.replace("-", " ").replace("_", " ")
            if display not in names:
                names.append(display)

    return names


__all__ = [
    "is_font_available",
    "list_available_fonts",
    "resolve_font_path",
]
