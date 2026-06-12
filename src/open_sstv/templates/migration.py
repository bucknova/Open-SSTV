# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.2 → v0.3 template migration.

Called once at startup (or whenever the app detects the templates directory
is absent/empty).  See docs/design/v0.3_templates.md §13.

Migration decision tree
───────────────────────
1. If ``{config_dir}/templates/`` exists and contains at least one ``.toml``
   file → user already has v0.3 templates; do nothing.

2. Otherwise, check legacy v0.2 QSO template strings in the old
   ``config.toml``-adjacent ``templates.toml``.  If the user had customised
   their v0.2 templates (i.e., the file exists), auto-generate a minimal
   v0.3 TextLayer template for each one so their callsign text survives.

3. If no v0.2 customisation is found → install the starter pack instead
   (five MMSSTV-style templates).

Rollback safety
───────────────
The v0.2 ``templates.toml`` is never modified or deleted.  If the user
downgrades they still have their old file.
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import platformdirs

from open_sstv.templates.manager import (
    default_templates_dir,
    install_starter_pack,
    starter_pack_installed,
)
from open_sstv.templates.model import (
    RectLayer,
    StrokeSpec,
    Template,
    TextLayer,
)
from open_sstv.templates.toml_io import save_template

_log = logging.getLogger(__name__)

_APP_NAME = "open_sstv"

# v0.2 default template texts — if the loaded v0.2 templates match these
# exactly, we skip legacy migration and install the starter pack instead.
_V2_DEFAULT_TEXTS: frozenset[str] = frozenset([
    "CQ CQ CQ DE {mycall} {mycall} K",
    "{theircall} DE {mycall}",
    "UR {rst} {date}",
    "{theircall} 73 DE {mycall} SK",
])


def _v2_templates_path(user_config_dir: Path | None = None) -> Path:
    base = user_config_dir or Path(platformdirs.user_config_dir(_APP_NAME))
    return base / "templates.toml"


def _load_v2_texts(v2_path: Path) -> list[tuple[str, str, dict]]:
    """Return ``[(overlay_text, template_name, overlay_raw), ...]`` from a
    v0.2 templates.toml.

    M5 (v0.3 audit): the raw overlay dict rides along so the migration
    can preserve the user's custom color and position — previously only
    the text survived and every migrated template came out white /
    bottom-center regardless of what the user had configured.

    Returns an empty list if the file is absent, corrupt, or all-defaults.
    """
    if not v2_path.is_file():
        return []
    try:
        with v2_path.open("rb") as f:
            raw = tomllib.load(f)
    except Exception:  # noqa: BLE001
        _log.warning("v0.2 templates.toml could not be read — skipping legacy migration")
        return []

    results: list[tuple[str, str, dict]] = []
    for tpl_raw in raw.get("template", []):
        name = tpl_raw.get("name", "")
        for ov_raw in tpl_raw.get("overlay", []):
            text = ov_raw.get("text", "").strip()
            if text and text not in _V2_DEFAULT_TEXTS:
                results.append((text, name, dict(ov_raw)))
    return results


#: v0.2 named positions → v0.3 layer anchors.  The v0.2 system had no
#: center-left/right presets, so this covers every value the old UI
#: could write.  Unknown strings (hand-edited files) fall back to "BC".
_V2_POSITION_TO_ANCHOR: dict[str, str] = {
    "Top Left": "TL",
    "Top Center": "TC",
    "Top Right": "TR",
    "Center": "C",
    "Bottom Left": "BL",
    "Bottom Center": "BC",
    "Bottom Right": "BR",
}


def _make_v3_from_v2(
    text: str, name: str, index: int, overlay_raw: dict | None = None
) -> Template:
    """Wrap a v0.2 overlay text in a minimal v0.3 Template.

    Produces a PhotoLayer + TextLayer.  Tokens are translated from v0.2
    style ({mycall} → %c, {theircall} → %o, {rst} → %r, {date} → %d,
    {time} → %t).

    M5 (v0.3 audit): when *overlay_raw* (the raw v0.2 overlay dict) is
    given, the user's custom color and named position are preserved —
    previously the migration hardcoded white / bottom-center and the
    user's customisations silently vanished on upgrade.  Explicit
    pixel ``x``/``y`` coordinates have no portable equivalent in the
    v0.3 percent-offset anchor system, so those still migrate to the
    nearest named anchor (logged when it happens).
    """
    from open_sstv.templates.model import PhotoLayer

    # Translate v0.2 placeholder syntax to v0.3 token syntax
    v2_to_v3 = {
        "{mycall}": "%c",
        "{theircall}": "%o",
        "{rst}": "%r",
        "{date}": "%d",
        "{time}": "%t",
    }
    translated = text
    for old, new in v2_to_v3.items():
        translated = translated.replace(old, new)

    role_hint = "custom"
    name_lower = name.lower()
    if "cq" in name_lower:
        role_hint = "cq"
    elif any(k in name_lower for k in ("exchange", "reply", "73")):
        role_hint = "reply" if "exchange" in name_lower or "reply" in name_lower else "closing"

    # M5: carry the v0.2 color and position forward.
    overlay_raw = overlay_raw or {}
    color_raw = overlay_raw.get("color", [255, 255, 255])
    if isinstance(color_raw, list | tuple) and len(color_raw) >= 3:
        fill = (int(color_raw[0]), int(color_raw[1]), int(color_raw[2]), 255)
    else:
        fill = (255, 255, 255, 255)
    v2_position = overlay_raw.get("position", "Bottom Center")
    anchor = _V2_POSITION_TO_ANCHOR.get(v2_position, "BC")
    if overlay_raw.get("x") is not None or overlay_raw.get("y") is not None:
        _log.info(
            "Migrating v0.2 template '%s': explicit x/y coordinates have "
            "no v0.3 equivalent — using the '%s' anchor instead",
            name, v2_position,
        )
    # The dark backing strip only makes sense behind bottom-anchored
    # text; for any other position it would sit in the wrong place.
    bottom_anchored = anchor in ("BL", "BC", "BR")
    # Offsets push the text inward from its anchor edge (see the
    # model.py anchor docs); centered text needs none.
    offset_y = 4.0 if anchor != "C" else 0.0

    layers: list = [PhotoLayer(id="base_photo", anchor="FILL", fit="cover")]
    if bottom_anchored:
        layers.append(
            RectLayer(
                id="text_bg",
                anchor="BL",
                width_pct=100.0,
                height_pct=18.0,
                fill=(0, 0, 0, 160),
            )
        )
    layers.append(
        TextLayer(
            id="main_text",
            text_raw=translated,
            anchor=anchor,  # type: ignore[arg-type]
            offset_y_pct=offset_y,
            font_family="DejaVu Sans Bold",
            font_size_pct=8.0,
            fill=fill,
            stroke=StrokeSpec(color=(0, 0, 0, 200), width_px=1),
            align="center",
            slashed_zero=True,
        )
    )

    return Template(
        name=name if name else f"Template {index}",
        role=role_hint,
        description=f"Auto-migrated from v0.2 template '{name}'. Original text: {text!r}",
        layers=layers,
    )


def run_migration(
    templates_dir: Path | None = None,
    user_config_dir: Path | None = None,
) -> str:
    """Run the v0.2 → v0.3 migration if needed.

    Parameters
    ----------
    templates_dir:
        Override for the v0.3 templates directory (for testing).
    user_config_dir:
        Override for the user config root (for testing).

    Returns
    -------
    str
        A human-readable description of what was done:
        ``"already_populated"``, ``"starter_pack_installed"``, or
        ``"legacy_migrated:<N>"``.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()

    # Step 1: Already populated — nothing to do.
    if starter_pack_installed(tdir):
        _log.debug("v0.3 templates already populated — skipping migration")
        return "already_populated"

    # Step 2: Check for user-customised v0.2 templates.
    v2_path = _v2_templates_path(user_config_dir)
    legacy_texts = _load_v2_texts(v2_path)

    if legacy_texts:
        tdir.mkdir(parents=True, exist_ok=True)
        count = 0
        for i, (text, name, overlay_raw) in enumerate(legacy_texts, start=1):
            t = _make_v3_from_v2(text, name, i, overlay_raw)
            safe = t.name.replace(" ", "_").replace("(", "").replace(")", "").lower()
            safe = "".join(c for c in safe if c.isalnum() or c in "_-")
            path = tdir / f"{safe}.toml"
            try:
                save_template(t, path)
                count += 1
                _log.info("Migrated v0.2 template '%s' → %s", name, path.name)
            except OSError as exc:
                _log.error("Failed to write migrated template %s: %s", path, exc)
        install_starter_pack(tdir)
        return f"legacy_migrated:{count}"

    # Step 3: No legacy customisations — install the starter pack.
    written = install_starter_pack(tdir)
    _log.info("Installed %d starter templates", len(written))
    return "starter_pack_installed"


__all__ = ["run_migration"]
