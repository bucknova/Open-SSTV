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

from open_sstv import __version__


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``open-sstv`` console script and ``python -m open_sstv``."""
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    log_level = logging.DEBUG if os.environ.get("OPEN_SSTV_DEBUG") else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # v0.1.34: log the runtime version and the module path immediately
    # so a stale install vs current source mismatch is obvious.  If the
    # terminal shows a different version than the About dialog, the
    # open-sstv script on PATH is pointing at a different Python
    # environment than the one pip install -e . ran against — usually
    # a pre-existing site-packages install from before the editable
    # install was set up.  ``open_sstv.__file__`` makes the source
    # path unambiguous.
    import open_sstv as _pkg
    print(
        f"Open-SSTV v{__version__} starting — module loaded from "
        f"{_pkg.__file__}",
        file=sys.stderr,
        flush=True,
    )

    # Windows taskbar grouping: by default a Python-launched binary
    # inherits whatever AppUserModelID Windows derives from the .exe
    # path, which is usually the Python interpreter's — so multiple
    # Python apps stack under one icon and the taskbar tooltip says
    # "python.exe".  Setting an explicit AppUserModelID *before*
    # constructing QApplication makes Windows treat Open-SSTV as its
    # own first-class taskbar entry with the icon we ship.  No-op on
    # other platforms.  See:
    #   https://learn.microsoft.com/en-us/windows/win32/shell/appids
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415
            # Reverse-DNS form is conventional; matches our org / repo.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "github.bucknova.OpenSSTV"
            )
        except (AttributeError, OSError, ImportError):
            # SetCurrentProcessExplicitAppUserModelID is shell32 ≥ Win7;
            # any failure here just means the taskbar grouping falls
            # back to the Python default.  Cosmetic only — never block
            # the GUI launch.
            pass

    # Qt is imported lazily so the encode/decode CLIs (which never
    # construct a QApplication) don't pay the import cost just because
    # they share a package with the GUI.
    try:
        from PySide6.QtGui import QIcon  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        print(
            "Error: PySide6 is not installed.\n"
            "Install it with:  pip install 'open-sstv[dev]'  or  pip install PySide6",
            file=sys.stderr,
        )
        return 1

    try:
        from open_sstv.ui.main_window import MainWindow  # noqa: PLC0415
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

    # App icon — picked up by every window's title bar, the Linux
    # window-manager hint, and the Windows taskbar (in addition to the
    # .ico embedded in the .exe by PyInstaller).  The PNG ships inside
    # the wheel under ``open_sstv/assets/icons/`` so this works for
    # pipx installs, source checkouts, and the PyInstaller bundle
    # alike — ``importlib.resources`` is the right abstraction for
    # all three.  Failure is non-fatal: a missing icon shouldn't
    # block the GUI from starting.
    try:
        import importlib.resources as _res  # noqa: PLC0415
        _icon_ref = _res.files("open_sstv") / "assets" / "icons" / "Open-SSTV.png"
        with _res.as_file(_icon_ref) as _icon_path:
            app.setWindowIcon(QIcon(str(_icon_path)))
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        pass

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
