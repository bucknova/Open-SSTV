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
    """Return the path to the shipped assets/templates directory.

    v0.3.21 fix: previously this used
    ``importlib.resources.files(...) / "assets" / "templates"`` followed
    by an ``as_file()`` context manager and returned the path from
    inside the ``with`` block.  ``as_file()`` is documented for
    *file* resources; behaviour on a *directory* traversable is
    implementation-defined.  In the PyInstaller onedir bundle this
    returned a path that ``Path.exists()`` rejected, so
    ``install_starter_pack`` silently logged "Bundled starter template
    missing" for every file and copied zero templates to the user's
    config dir on first launch.  Wheel / pip-install layouts happened
    to work by coincidence because ``importlib.resources`` resolved to
    the actual on-disk path directly.

    The replacement anchors on ``open_sstv.__file__`` which is the
    package's ``__init__.py`` in every install layout we ship —
    editable ``pip install -e .``, wheel, PyInstaller onedir, and
    PyInstaller ``.app`` BUNDLE.  ``.parent`` is the package root and
    the subpath is just file-system arithmetic.  No fragile
    ``importlib.resources`` semantics involved.
    """
    import open_sstv  # noqa: PLC0415 — lazy to avoid import cycle
    return Path(open_sstv.__file__).parent / "assets" / "templates"


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def _cleanup_stale_tmp_files(tdir: Path) -> None:
    """Remove any ``*.toml.tmp`` files left behind in *tdir* (M-6).

    ``save_template`` writes via a sibling ``.tmp`` + ``os.replace``;
    on the ``OSError`` branch it unlinks its own tmp, but a hard kill
    (SIGKILL, power loss, OOM) between ``tomli_w.dump`` and
    ``os.replace`` leaves the tmp orphaned forever.  Over a long-running
    install they accumulate (especially relevant once template editing
    is a common GUI flow).  Sweep them opportunistically at every
    ``list_templates`` call — the gallery refresh runs at app startup
    and on demand, so stale tmps never live long.  Mirror H-5's
    ``config/store.py`` cleanup pattern; failures are logged at debug
    and never block the listing.
    """
    try:
        for tmp in tdir.glob("*.toml.tmp"):
            try:
                tmp.unlink()
                _log.info("Removed stale template tmp file: %s", tmp)
            except OSError as exc:
                _log.debug("Could not remove stale template tmp %s: %s", tmp, exc)
    except OSError as exc:
        # tdir.glob itself can fail if the directory disappeared mid-call.
        _log.debug("Stale-tmp sweep failed for %s: %s", tdir, exc)


def list_templates(
    templates_dir: Path | None = None,
) -> list[tuple[str, str, Path]]:
    """Return ``[(name, role, path), ...]`` for every loadable template in *templates_dir*.

    Templates that fail to load (corrupt TOML, future schema version) are
    logged and skipped — the list always contains only valid entries.
    Sorted by filename for a stable gallery order.

    Side effect (M-6): opportunistically removes any ``*.toml.tmp``
    files left behind by a SIGKILL-during-save.  Mirrors the H-5 config
    cleanup pattern; failures are logged at debug and never block the
    listing.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    if not tdir.is_dir():
        return []

    _cleanup_stale_tmp_files(tdir)

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
    """Return True if at least one v0.3 starter template is in *templates_dir*.

    v0.3.22 fix: the previous check returned True if *any* ``*.toml``
    existed in the user templates dir.  That misdiagnosed two real
    upgrade paths as "installed":

      1. A user upgrading from v0.2.x (which shipped different
         starter filenames like ``cq.toml``) had those old files
         hanging around.  The check saw them, returned True,
         ``install_starter_pack`` was skipped, and the v0.3 starter
         pack — the eight files this version actually ships — never
         landed.  Caught 2026-05-28 via diagnostics zip from a user
         whose Windows install showed exactly this state.

      2. A hypothetical user who created one custom template before
         their first launch would hit the same misdiagnosis.

    The tightened check looks specifically for any one of the v0.3
    starter filenames (``STARTER_TEMPLATE_FILENAMES``).  Semantics:

      * Fresh install / dir doesn't exist        → False → install.
      * v0.2.x upgrade with old cq.toml only     → False → install.
      * Post-v0.3 user who deleted one template  → True  → respect it.
      * Truly populated v0.3 user                → True  → no-op.

    The "respect a deletion" property matters because
    ``install_starter_pack`` doesn't overwrite existing files
    (``overwrite=False`` is the default), so calling it every launch
    would *not* clobber user edits — but it would noisily re-install
    a template the user deliberately deleted.  Returning True the
    moment any starter is present preserves that intent.
    """
    tdir = templates_dir if templates_dir is not None else default_templates_dir()
    if not tdir.is_dir():
        return False
    return any((tdir / filename).exists() for filename in STARTER_TEMPLATE_FILENAMES)


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
