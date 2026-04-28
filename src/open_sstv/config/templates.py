# SPDX-License-Identifier: GPL-3.0-or-later
"""QSO template data model, persistence, and placeholder resolution.

Templates let operators burn preconfigured text layouts onto TX images
with one click during a live QSO.  Each template has a name (shown as a
button label) and one or more text overlays with placeholder variables
like ``{mycall}`` or ``{theircall}``.

Storage is a separate ``templates.toml`` alongside ``config.toml`` in the
platformdirs config directory.  If the file is missing or empty, the
three built-in defaults (CQ, Exchange, 73) are returned.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

import platformdirs
import tomli_w

_APP_NAME = "open_sstv"
_TEMPLATES_FILENAME = "templates.toml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class QSOTemplateOverlay:
    """A single text element within a template.

    When *x* and *y* are both set, they override the named *position*
    preset with explicit pixel coordinates.  ``None`` (the default)
    means "compute from the position name at render time."  This keeps
    existing templates backward-compatible — only templates saved with
    the new X/Y spin boxes will carry explicit coordinates.
    """

    text: str = ""
    position: str = "Bottom Center"
    size: int = 24
    color: tuple[int, int, int] = (255, 255, 255)
    x: int | None = None
    y: int | None = None


@dataclass
class QSOTemplate:
    """A named collection of text overlays applied to a TX image."""

    name: str = ""
    overlays: list[QSOTemplateOverlay] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def default_templates() -> list[QSOTemplate]:
    """Return the three built-in templates shipped on first launch."""
    return [
        QSOTemplate(
            name="CQ",
            overlays=[
                QSOTemplateOverlay(
                    text="CQ CQ CQ DE {mycall} {mycall} K",
                    position="Bottom Center",
                    size=24,
                    color=(255, 255, 255),
                ),
            ],
        ),
        QSOTemplate(
            name="Exchange",
            overlays=[
                QSOTemplateOverlay(
                    text="{theircall} DE {mycall}",
                    position="Top Center",
                    size=24,
                    color=(255, 255, 255),
                ),
                QSOTemplateOverlay(
                    text="UR {rst} {date}",
                    position="Bottom Center",
                    size=20,
                    color=(255, 255, 200),
                ),
            ],
        ),
        QSOTemplate(
            name="73",
            overlays=[
                QSOTemplateOverlay(
                    text="{theircall} 73 DE {mycall} SK",
                    position="Bottom Center",
                    size=24,
                    color=(255, 255, 255),
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------

def needs_user_input(template: QSOTemplate) -> set[str]:
    """Return the set of placeholder names that require user input.

    Currently ``{theircall}`` and ``{rst}`` need the operator to type
    something; everything else auto-fills.
    """
    user_vars: set[str] = set()
    for overlay in template.overlays:
        if "{theircall}" in overlay.text:
            user_vars.add("theircall")
        if "{rst}" in overlay.text:
            user_vars.add("rst")
    return user_vars


def resolve_placeholders(
    text: str,
    *,
    mycall: str = "",
    theircall: str = "",
    rst: str = "59",
) -> str:
    """Substitute placeholder variables in *text*.

    Unknown placeholders are left as-is (no crash).
    """
    now_utc = datetime.now(UTC)
    return (
        text
        .replace("{mycall}", mycall)
        .replace("{theircall}", theircall)
        .replace("{rst}", rst)
        .replace("{date}", now_utc.strftime("%Y-%m-%d"))
        .replace("{time}", now_utc.strftime("%H:%MZ"))
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def templates_path() -> Path:
    """Absolute path to the templates TOML file (may not exist yet)."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / _TEMPLATES_FILENAME


def load_templates(path: Path | None = None) -> list[QSOTemplate]:
    """Load templates from *path* (default: ``templates_path()``).

    Returns ``default_templates()`` if the file doesn't exist or is empty.
    """
    if path is None:
        path = templates_path()
    if not path.is_file():
        return default_templates()

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)

        templates: list[QSOTemplate] = []
        for tpl_raw in raw.get("template", []):
            overlays: list[QSOTemplateOverlay] = []
            for ov_raw in tpl_raw.get("overlay", []):
                color_raw = ov_raw.get("color", [255, 255, 255])
                overlays.append(
                    QSOTemplateOverlay(
                        text=ov_raw.get("text", ""),
                        position=ov_raw.get("position", "Bottom Center"),
                        size=ov_raw.get("size", 24),
                        color=tuple(color_raw[:3]) if len(color_raw) >= 3 else (255, 255, 255),
                        x=ov_raw.get("x"),
                        y=ov_raw.get("y"),
                    )
                )
            templates.append(
                QSOTemplate(name=tpl_raw.get("name", ""), overlays=overlays)
            )

        return templates if templates else default_templates()
    except Exception:  # noqa: BLE001 — corrupt file must never crash startup
        _log.warning("Templates file %s is corrupt or unreadable — using defaults", path)
        return default_templates()


def save_templates(
    templates: list[QSOTemplate], path: Path | None = None
) -> None:
    """Write *templates* to *path* (default: ``templates_path()``).

    Raises
    ------
    OSError
        If the file cannot be created or written (disk full, permissions,
        etc.).  Callers are expected to catch this and show a user-facing
        error dialog.
    """
    if path is None:
        path = templates_path()
    data: dict = {"template": []}
    for tpl in templates:
        tpl_dict: dict = {"name": tpl.name, "overlay": []}
        for ov in tpl.overlays:
            ov_dict: dict = {
                "text": ov.text,
                "position": ov.position,
                "size": ov.size,
                "color": list(ov.color),
            }
            if ov.x is not None:
                ov_dict["x"] = ov.x
            if ov.y is not None:
                ov_dict["y"] = ov.y
            tpl_dict["overlay"].append(ov_dict)
        data["template"].append(tpl_dict)

    # OP2-07: write atomically via a sibling .tmp + os.replace so a
    # SIGKILL mid-write never leaves a truncated templates file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        _log.error("Failed to save templates to %s: %s", path, exc)
        tmp.unlink(missing_ok=True)
        raise


__all__ = [
    "QSOTemplate",
    "QSOTemplateOverlay",
    "default_templates",
    "load_templates",
    "needs_user_input",
    "resolve_placeholders",
    "save_templates",
    "templates_path",
]
