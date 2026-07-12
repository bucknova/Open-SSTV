# SPDX-License-Identifier: GPL-3.0-or-later
"""Remote web access (Phase 1 — read-only gallery).

The desktop app embeds a small HTTP server so a browser on the LAN can
*view* decoded images.  This is the first, deliberately narrow slice of
the remote-access design (see ``design/remote/architecture.md``):

- **Read-only.**  No compose, no camera, and above all **no transmit** —
  there is no wire command in this package that can key the rig.
- **Qt-free service layer.**  :class:`~open_sstv.remote.service.GalleryService`
  reuses the Qt-free ``gallery`` package and opens its *own* short-lived
  SQLite connection per query, so it is safe to call from the server's
  worker threads without touching the main-thread Qt objects or the
  main-thread logbook connection.
- **Images are addressed by opaque id, never by client-supplied path** —
  the path-traversal fence for the whole surface.

Phase 2 (the view plane proper) adds authentication/pairing, a live RX
event stream, and a Settings UI; Phase 1 is TOML-config-only and proves
the embedded-server threading model.
"""
from __future__ import annotations

from open_sstv.remote.compose import ComposeService
from open_sstv.remote.control import ControlPlane
from open_sstv.remote.events import EventHub
from open_sstv.remote.server import RemoteServer
from open_sstv.remote.service import GalleryService

__all__ = [
    "ComposeService",
    "ControlPlane",
    "EventHub",
    "GalleryService",
    "RemoteServer",
]
