# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared pytest fixtures for sstv-app.

In Phase 2 this file generates SSTV WAV fixtures on demand at session start
by feeding small reference images from ``tests/fixtures/images/`` through
PySSTV. We deliberately don't commit the WAV blobs to the repo — they're
regenerated locally so the binary surface stays small. Noisy variants are
produced by mixing in white Gaussian noise at 20 / 10 / 5 dB SNR.

Phase 0 stub. Real fixtures land alongside the decoder work in Phase 2.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _qt_shutdown_hygiene():
    """Drain Qt leftovers before interpreter exit (v0.4 test hygiene).

    With 1,500+ tests sharing one QApplication, objects queued via
    ``deleteLater`` and Python receivers with live signal connections
    can survive to interpreter shutdown, where they deallocate in
    arbitrary GC order.  PySide6 then auto-disconnects from a sender
    whose wrapper type is already torn down and segfaults
    (``Sbk_GetPyOverride → dict_getitem``) — observed 2026-06-11 on a
    full-suite run: every test green, process dead with SIGSEGV, which
    a CI matrix would report as a failed job.  Nondeterministic (the
    immediate re-run was clean), so this flushes the raw material
    while the interpreter is still healthy instead of chasing the
    unlucky ordering.

    Also clears the clipboard: the gallery copy-to-clipboard tests
    leave a QPixmap promise that macOS otherwise complains about at
    exit ("Cannot keep promise…").

    Best-effort by design — hygiene must never fail the suite, and
    headless ``-m 'not gui'`` runs may never construct a QApplication.
    """
    yield
    try:
        import gc

        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        clipboard = app.clipboard()
        if clipboard is not None:
            clipboard.clear()
        # Alternate GC and deferred-delete processing: collecting can
        # queue new deleteLater calls and vice versa.
        for _ in range(3):
            gc.collect()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    except Exception:  # noqa: BLE001 — never let teardown hygiene fail the run
        pass
