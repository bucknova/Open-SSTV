# SPDX-License-Identifier: GPL-3.0-or-later
"""QApplication bootstrap and dependency-injection wiring.

Phase 1 launches a TX-only main window: load an image, pick a mode,
click Transmit, and the audio plays out the system default output device
(with optional rigctld PTT keying around it). Phase 2 will add the RX
side and a settings dialog.

Backends are constructed here, not inside the window, so future tests
and headless launches can swap them out without monkey-patching the UI.
"""
from __future__ import annotations

import signal
import sys

from sstv_app import __version__


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``sstv-app`` console script and ``python -m sstv_app``."""
    # Qt is imported lazily so the encode/decode CLIs (which never
    # construct a QApplication) don't pay the import cost just because
    # they share a package with the GUI.
    try:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        print(
            "Error: PySide6 is not installed.\n"
            "Install it with:  pip install 'sstv-app[dev]'  or  pip install PySide6",
            file=sys.stderr,
        )
        return 1

    try:
        from sstv_app.ui.main_window import MainWindow  # noqa: PLC0415
    except ImportError as exc:
        missing = str(exc).replace("No module named ", "").strip("'\"")
        print(
            f"Error: required dependency '{missing}' is not installed.\n"
            f"Install all dependencies with:  pip install sstv-app",
            file=sys.stderr,
        )
        return 1

    qt_argv = list(argv) if argv is not None else sys.argv
    app = QApplication(qt_argv)
    app.setApplicationName("Open-SSTV")
    app.setApplicationDisplayName("Open-SSTV")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("bucknova")
    app.setOrganizationDomain("github.com/bucknova")

    # Start with ManualRig (no-op). The user clicks "Connect Rig" in
    # the radio panel to establish a live rigctld link at runtime.
    window = MainWindow()
    window.show()

    # Belt-and-braces cleanup: even if the event loop quits via something
    # other than the user clicking X (Ctrl-C, signal, etc.), make sure
    # the window's closeEvent fires so the TX worker thread shuts down
    # cleanly instead of being destroyed mid-run.
    app.aboutToQuit.connect(window.close)

    # Route SIGTERM (systemd stop, kill PID, container shutdown) through
    # Qt's event loop so closeEvent fires and PTT is unkeyed cleanly.
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    # SIGINT (Ctrl-C in terminal) follows the same path.
    signal.signal(signal.SIGINT, lambda *_: app.quit())

    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
