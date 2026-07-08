# SPDX-License-Identifier: GPL-3.0-or-later
"""Server-side compose — render photo + tokens + template to a TX image.

Phase 4 (compose plane).  The browser sends a photo, a template choice,
and token values; the **app** renders the exact on-air bytes with the
same v0.3 compositor the desktop uses (``templates.renderer.render_
template``).  No layout logic is reimplemented in the browser — one
renderer, WYSIWYG fidelity (see ``design/remote/architecture.md`` §7).

Qt-free (Pillow + the ``templates`` package only), so it runs on the
server's request threads and is unit-testable headless.  This module only
*produces* a composed image; putting it on the air still goes through the
Phase 3 control plane (lease → confirm → transmit), so composing is safe
and never touches the rig.
"""
from __future__ import annotations

import hashlib
import io
import logging
from typing import TYPE_CHECKING

from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.templates import manager as template_manager
from open_sstv.templates.model import QSOState, TXContext
from open_sstv.templates.renderer import render_template

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import PIL.Image

    from open_sstv.config.schema import AppConfig

_log = logging.getLogger(__name__)

#: Reject oversized uploads.  A phone photo is a few MB; the SSTV frame is
#: tiny, so anything past this is abuse or a mistake.
MAX_PHOTO_BYTES = 12 * 1024 * 1024


def _template_id(path: Path) -> str:
    """Stable opaque id for a template, by path (matches the gallery scheme)."""
    return hashlib.sha1(path.as_posix().encode("utf-8")).hexdigest()[:16]


class ComposeService:
    """Renders a browser-composed TX image via the desktop compositor.

    ``templates_dir`` defaults to the same directory the desktop uses, so
    the remote sees the same templates; tests inject a fixture dir.
    """

    def __init__(
        self,
        config_getter: Callable[[], AppConfig],
        templates_dir: Path | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._templates_dir = templates_dir

    def _dir(self) -> Path:
        return self._templates_dir or template_manager.default_templates_dir()

    def list_templates(self) -> list[dict[str, str]]:
        """The composable templates, as ``[{id, name, role}, …]``."""
        return [
            {"id": _template_id(path), "name": name, "role": role}
            for name, role, path in template_manager.list_templates(self._dir())
        ]

    def _resolve(self, template_id: str) -> Path | None:
        for _name, _role, path in template_manager.list_templates(self._dir()):
            if _template_id(path) == template_id:
                return path
        return None

    def render(
        self,
        photo_bytes: bytes,
        template_id: str,
        tokens: dict[str, str],
        mode_value: str,
    ) -> PIL.Image.Image | None:
        """Render the composed image, or ``None`` on any bad input.

        Every failure path returns ``None`` (never raises) so a malformed
        upload can't 500 the server; the HTTP layer maps that to a 4xx.
        """
        import PIL.Image  # noqa: PLC0415

        if not photo_bytes or len(photo_bytes) > MAX_PHOTO_BYTES:
            return None
        try:
            mode = Mode(mode_value)
        except ValueError:
            return None
        path = self._resolve(template_id)
        if path is None:
            return None
        template = template_manager.load_by_path(path)
        if template is None:
            return None
        try:
            photo = PIL.Image.open(io.BytesIO(photo_bytes))
            photo.load()
        except Exception as exc:  # noqa: BLE001 — any decode failure → 4xx
            _log.debug("compose: undecodable photo: %s", exc)
            return None
        qso = QSOState(
            tocall=(tokens.get("tocall") or "").strip(),
            rst=(tokens.get("rst") or "").strip() or "595",
            tocall_name=(tokens.get("name") or "").strip(),
            note=(tokens.get("note") or "").strip(),
        )
        spec = MODE_TABLE[mode]
        ctx = TXContext(
            mode_display_name=mode.value,
            frame_size=(spec.width, spec.display_height),
            photo_image=photo,
        )
        try:
            return render_template(template, qso, self._config_getter(), ctx)
        except Exception as exc:  # noqa: BLE001 — a bad template must not 500
            _log.warning("compose: render failed for %s: %s", path.name, exc)
            return None


__all__ = ["MAX_PHOTO_BYTES", "ComposeService"]
