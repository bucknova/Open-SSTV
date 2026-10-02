# SPDX-License-Identifier: GPL-3.0-or-later
"""SIGTERM / SIGINT / SIGHUP must shut the app down cleanly, even when idle.

CPython runs a Python-level signal handler only between bytecodes.  While
Qt's event loop sits idle in C++, no bytecode runs.  A handler installed
with plain ``signal.signal`` is therefore queued and never executes, and
because it replaced the default action, the signal is ignored outright.
Open-SSTV did exactly that until the 2026-10 stability audit.  In the
Linux release smoke test it ignored SIGTERM for two hours.  On a real
station that means ``kill``, ``systemctl stop`` and a desktop logout never
reach ``closeEvent``, which is where PTT is dropped.

These tests start a real Qt app in a subprocess, leave it idle, send a real
signal, and require it to exit through the clean path within a few seconds.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signal delivery"
)

#: Child program: an idle Qt app wired with the production helper.  Prints
#: READY once its event loop is running, and markers from both shutdown
#: hooks the real app relies on.
_CHILD = textwrap.dedent(
    """
    import os, sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from open_sstv.app import install_quit_signals

    class W(QWidget):
        def closeEvent(self, event):
            print("CLOSE_EVENT", flush=True)
            event.accept()

    app = QApplication(sys.argv)
    w = W()
    w.show()
    app.aboutToQuit.connect(w.close)
    app.aboutToQuit.connect(lambda: print("ABOUT_TO_QUIT", flush=True))
    install_quit_signals(app)
    QTimer.singleShot(0, lambda: print("READY", flush=True))
    sys.exit(app.exec())
    """
)


def _start_child() -> subprocess.Popen[str]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip() == "READY":
            return proc
        if not line and proc.poll() is not None:
            break
    proc.kill()
    pytest.fail(f"child never became ready: {proc.stdout.read()}")


@pytest.mark.parametrize(
    "sig",
    [signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)],
    ids=lambda s: s.name,
)
def test_signal_quits_an_idle_app_cleanly(sig: signal.Signals) -> None:
    proc = _start_child()
    # Let the event loop go fully idle: the bug only bites when no Python
    # code is running.
    time.sleep(1.0)
    proc.send_signal(sig)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail(
            f"{sig.name} was ignored by an idle app (still running after "
            f"10 s). Output: {out!r}"
        )
    assert proc.returncode == 0, f"exit {proc.returncode}: {out!r}"
    assert "CLOSE_EVENT" in out, f"closeEvent never ran: {out!r}"
    assert "ABOUT_TO_QUIT" in out, f"aboutToQuit never fired: {out!r}"
