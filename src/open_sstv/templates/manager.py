# SPDX-License-Identifier: GPL-3.0-or-later
"""Template manager: list, load, save, delete, and install the starter pack.

Directory layout
────────────────
User templates live in:
    {user_config_dir}/open_sstv/templates/

Bundled starter templates are shipped in:
    open_sstv/assets/templates/   (accessed via importlib.resources)

The manager always prefers user templates; the bundled set is only used
for ``install_starter_pack()``.

All public functions accept an optional ``templates_dir`` override so
callers (and tests) can redirect to a temporary directory without touching
the real user config.
"""
from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

import platformdirs

from open_sstv.templates.model import Template
from open_sstv.templates.toml_io import (
    SchemaVersionError,
    TemplateLoadError,
    load_template,
    save_template,
)

_log = logging.getLogger(__name__)

_APP_NAME = "open_sstv"

# Filenames of the starter templates bundled in assets/templates/, in
# install order (determines gallery display order on first launch).
STARTER_TEMPLATE_FILENAMES: tuple[str, ...] = (
    "cqsstv.toml",
    "cq_de_call.toml",
    "reply_exchange.toml",
    "reply_simple.toml",
    "seventy_three.toml",
    "cqsstv_vertical.toml",
    "seventy_three_card.toml",
    "seventy_three_vertical.toml",
)


# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------


def default_templates_dir() -> Path:
    """Return the user-config templates directory (may not exist yet)."""
    return Path(platformdirs.user_config_dir(_APP_NAME)) / "templates"


def default_station_assets_dir() -> Path:
    """Return the user-config station assets directory (may not exist yet).

    StationImageLayer.path values are resolved relative to this directory,
    and the renderer rejects any resolved path that escapes it.
    """
    return Path(platformdirs.user_config_dir(_APP_NAME)) / "assets"


def _bundled_templates_dir() -> Path:
    """Return the path to the shipped assets/templates directory."""
    anchor = importlib.resources.files("open_sstv") / "assets" / "templates"
    with importlib.resources.as_file(anchor) as p:
        return Path(p)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def list_templates(
    templates_dir: Path | None = None,
) -> list[tuple[str, str, Path]]:
    """Return ``[(name, role, path), ...]`` for every loadable template in *templates_dir*.

    Templates that fail to load (corrupt TOML, future schema version) are
    logged and skipped — the list always contains only valid entries.
    Sorted by filename for a stable gallery order.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    if not tdir.is_dir():
        return []

    results: list[tuple[str, str, Path]] = []
    for path in sorted(tdir.glob("*.toml")):
        try:
            t = load_template(path)
            results.append((t.name, t.role, path))
        except SchemaVersionError as exc:
            _log.warning("Skipping %s: %s", path.name, exc)
        except (TemplateLoadError, Exception) as exc:  # noqa: BLE001
            _log.warning("Could not read template %s: %s", path.name, exc)
    return results


def load_by_path(path: Path) -> Template | None:
    """Load a single template from *path*, returning ``None`` on any error."""
    try:
        return load_template(path)
    except (SchemaVersionError, TemplateLoadError, OSError, Exception) as exc:  # noqa: BLE001
        _log.warning("Failed to load template %s: %s", path, exc)
        return None


def get_templates_by_role(
    role: str,
    templates_dir: Path | None = None,
) -> list[Template]:
    """Return all loadable templates with the given *role*.

    *role* is one of ``"cq"``, ``"reply"``, ``"closing"``, ``"custom"``.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    results: list[Template] = []
    for _name, r, path in list_templates(tdir):
        if r == role:
            t = load_by_path(path)
            if t is not None:
                results.append(t)
    return results


def save(
    template: Template,
    templates_dir: Path | None = None,
    *,
    filename: str | None = None,
) -> Path:
    """Save *template* to *templates_dir*, returning the path written.

    If *filename* is not provided, derives it from the template name:
    spaces → underscores, lowercased, ``.toml`` suffix.
    Existing files are overwritten.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    if filename is None:
        safe = template.name.strip().replace(" ", "_").replace("/", "_").lower()
        safe = "".join(c for c in safe if c.isalnum() or c in "_-")
        filename = (safe or "template") + ".toml"
    path = tdir / filename
    save_template(template, path)
    return path


def delete(path: Path) -> None:
    """Delete the template file at *path*.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    OSError
        On permission or I/O errors.
    """
    path.unlink()


def duplicate_template(path: Path) -> Path:
    """Create a copy of the template at *path*, returning the new file path.

    The copy's ``name`` field gets ``" (copy)"`` appended (or ``" (copy 2)"``,
    ``" (copy 3)"``, … if a sibling with that name already exists), and the
    written filename is derived from the new name through the same slug
    rules as :func:`save` so the gallery loads it on the next refresh.

    Raises
    ------
    FileNotFoundError
        If the source template does not exist.
    TemplateLoadError / SchemaVersionError
        If the source template cannot be loaded.
    OSError
        On permission or I/O errors writing the copy.
    """
    src = load_template(path)
    tdir = path.parent

    existing = {p.stem for p in tdir.glob("*.toml")}

    base_name = src.name + " (copy)"
    new_name = base_name
    n = 2
    while True:
        slug = "".join(
            c for c in new_name.replace(" ", "_").replace("/", "_").lower()
            if c.isalnum() or c in "_-"
        ) or "template"
        # Both checks are deliberate, not redundant.  ``existing`` was
        # snapshotted from one ``glob()`` call before the loop, so it can
        # miss a sibling template that another process (a parallel CLI run,
        # an editor "Save As") wrote between the snapshot and this iteration.
        # The ``path.exists()`` check covers that race.  Conversely, when the
        # candidate slug also collides with itself across iterations of this
        # very loop (we just appended " (copy 2)", " (copy 3)", …), the
        # in-memory ``existing`` set is the authoritative answer because the
        # files we'd be racing with don't exist yet — we haven't written
        # them.  Either alone leaves a real gap, so keep both.  The remaining
        # TOCTOU window between the check and ``save_template`` is bounded
        # by ``os.replace`` atomicity (overwrites are deliberate in ``save``)
        # and isn't worsened by this guard.
        if slug not in existing and not (tdir / f"{slug}.toml").exists():
            break
        new_name = f"{src.name} (copy {n})"
        n += 1

    src.name = new_name
    return save(src, tdir, filename=f"{slug}.toml")


# ---------------------------------------------------------------------------
# Starter pack
# ---------------------------------------------------------------------------


def starter_pack_installed(templates_dir: Path | None = None) -> bool:
    """Return True if the templates directory is non-empty."""
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    if not tdir.is_dir():
        return False
    return any(tdir.glob("*.toml"))


def install_starter_pack(
    templates_dir: Path | None = None,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Copy the bundled starter templates into *templates_dir*.

    Parameters
    ----------
    templates_dir:
        Target directory.  Created if it does not exist.
    overwrite:
        When ``False`` (default), existing files are not overwritten so
        user edits survive a re-install.  Pass ``True`` to force-reset.

    Returns
    -------
    list[Path]
        Paths of the files that were written (skipped files not included).
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    tdir.mkdir(parents=True, exist_ok=True)

    bundled = _bundled_templates_dir()
    written: list[Path] = []

    for filename in STARTER_TEMPLATE_FILENAMES:
        src = bundled / filename
        dst = tdir / filename
        if dst.exists() and not overwrite:
            _log.debug("Skipping existing template %s", filename)
            continue
        if not src.exists():
            _log.warning("Bundled starter template missing: %s", filename)
            continue
        dst.write_bytes(src.read_bytes())
        _log.info("Installed starter template: %s", filename)
        written.append(dst)

    return written


__all__ = [
    "STARTER_TEMPLATE_FILENAMES",
    "default_templates_dir",
    "delete",
    "duplicate_template",
    "get_templates_by_role",
    "install_starter_pack",
    "list_templates",
    "load_by_path",
    "save",
    "starter_pack_installed",
]
