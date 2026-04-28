# SPDX-License-Identifier: GPL-3.0-or-later
"""Background update checker.

Runs a single HTTPS GET against the GitHub releases API on a worker
thread so it never blocks the GUI.  If a newer version is found,
``update_available`` is emitted with the version string and the release
page URL.

The check times out after 3 seconds; any network error is silently
swallowed — an update check failing is never surfaced as an error.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from urllib.error import URLError

from PySide6.QtCore import QObject, Signal, Slot

from open_sstv import __version__

_log = logging.getLogger(__name__)

_API_URL = "https://api.github.com/repos/bucknova/Open-SSTV/releases/latest"
_TIMEOUT_S = 3


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v0.2.15' or '0.2.15' → (0, 2, 15). Non-numeric segments become 0."""
    parts = tag.lstrip("v").split(".")
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return tuple(result)


class UpdateCheckerWorker(QObject):
    """Polls the GitHub releases API for a newer version."""

    #: Emitted when a newer release is found: (version_string, release_url).
    update_available = Signal(str, str)
    #: Emitted when the check finishes, whether or not an update was found.
    check_complete = Signal()

    @Slot()
    def check(self) -> None:
        """Fetch the latest release and compare against the running version.

        Blocking — must run on a background QThread.
        """
        try:
            req = urllib.request.Request(
                _API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"open-sstv/{__version__}",
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            tag = data.get("tag_name", "")
            url = data.get("html_url", _API_URL)
            if tag and _parse_version(tag) > _parse_version(__version__):
                self.update_available.emit(tag.lstrip("v"), url)
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            # Network hiccups, DNS failures, malformed JSON, and offline
            # mode are all expected and silent — but keep a debug-level
            # trace so a real bug (TypeError, AttributeError, …) can't
            # hide behind a bare ``except Exception``.
            _log.debug("update check failed: %s", exc)
        finally:
            self.check_complete.emit()


__all__ = ["UpdateCheckerWorker"]
