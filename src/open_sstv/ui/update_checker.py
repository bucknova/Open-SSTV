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

import http.client
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
    """``"v0.2.15"`` or ``"0.2.15"`` → ``(0, 2, 15)``.

    Pre-release suffixes (``"0rc1"`` / ``"3a2"`` / ``"5.dev1"``) are
    stripped down to the leading digits — ``"v1.0.0rc1"`` → ``(1, 0, 0)``,
    which sorts equal to the matching stable release (``1.0.0``).  GitHub
    doesn't normally tag pre-releases as ``latest`` so this is a safety
    rail for forks / typos rather than the common path.

    Non-numeric segments with no leading digits (e.g. an entirely-letter
    tag like ``"main"``) become ``0`` so the comparison remains a tuple
    of integers.

    L6: previously each segment ran straight through ``int()`` and a
    non-numeric segment like ``"0rc1"`` raised ValueError → returned
    ``0``, meaning ``"v0.2.0rc1"`` parsed to ``(0, 2, 0)``.  That's the
    same as ``"v0.2.0"``, which is *correct* for the "is this a newer
    release" comparison.  But ``"v0.2.0a1"`` and ``"v0.2.0b1"`` and
    ``"v0.2.0rc1"`` all compare equal — fine if all three are tagged
    consecutively, fragile if they aren't.  Tighten to "take the
    leading digit run; if none, treat as 0".
    """
    parts = tag.lstrip("v").split(".")
    result: list[int] = []
    for p in parts:
        # Take the leading run of digits.  "12rc1" → 12; "rc1" → 0;
        # "12" → 12; "" → 0.
        leading = ""
        for ch in p:
            if ch.isdigit():
                leading += ch
            else:
                break
        result.append(int(leading) if leading else 0)
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
            # M1: a rate-limit / abuse-detection JSON from GitHub is a
            # *dict* with different keys, but a hypothetical malformed
            # response (or a future GitHub API change returning a list)
            # would make ``data.get`` raise ``AttributeError`` that
            # bypassed every except below.  Guard explicitly.
            if not isinstance(data, dict):
                _log.debug("update check: unexpected response type %s", type(data).__name__)
                return
            tag = data.get("tag_name", "")
            url = data.get("html_url", _API_URL)
            if tag and _parse_version(tag) > _parse_version(__version__):
                self.update_available.emit(tag.lstrip("v"), url)
        except (
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            # M1: ``http.client.HTTPException`` (IncompleteRead, BadStatusLine,
            # etc.) is NOT an OSError subclass — a malformed response chunk
            # would otherwise crash the worker thread and the ``finally``
            # block would leave the worker in an indeterminate state.
            http.client.HTTPException,
        ) as exc:
            # Network hiccups, DNS failures, malformed JSON, and offline
            # mode are all expected and silent — but keep a debug-level
            # trace so a real bug (TypeError, AttributeError, …) can't
            # hide behind a bare ``except Exception``.
            _log.debug("update check failed: %s", exc)
        finally:
            self.check_complete.emit()


__all__ = ["UpdateCheckerWorker"]
