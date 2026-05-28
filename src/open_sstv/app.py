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

    # Cross-OS process-name fix.  When Open-SSTV is launched via the
    # ``open-sstv`` console script from a venv (the canonical source /
    # pipx install path), the actual executable is the venv's Python
    # interpreter — so the OS dock/taskbar tooltip and process-list
    # entry read "python" or "python3" by default.  PyInstaller bundles
    # are exempt (the bootloader binary is named ``open-sstv``).  We
    # apply per-OS overrides *before* constructing QApplication so the
    # platform's window system picks them up the first time it asks.

    # (1) Some platforms (Linux X11 WM_CLASS in particular) sniff
    # ``sys.argv[0]`` to derive the application name.  Overriding it
    # to a friendly string is cheap and harmless on every OS.
    sys.argv[0] = "Open-SSTV"

    # (2) Windows: AppUserModelID controls taskbar icon grouping AND the
    # hover tooltip.  Without an explicit ID, Windows derives one from
    # the .exe path — usually the Python interpreter's, so multiple
    # Python apps stack under one taskbar icon labelled "python.exe".
    # See https://learn.microsoft.com/en-us/windows/win32/shell/appids.
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

    # (3) macOS: the Dock tooltip reads from ``-[NSProcessInfo
    # processInfo] processName]``.  Qt's QCoreApplication.setApplicationName
    # (called below) doesn't propagate to NSProcessInfo, so the source-
    # install Dock entry stays labelled "python" until we set it directly.
    # We use libobjc via ctypes rather than depending on pyobjc — libobjc
    # is always present on macOS, and the call sequence is small enough
    # to inline.  Wrapped in a broad try/except so a future Objective-C
    # ABI change can't break the GUI launch.
    if sys.platform == "darwin":
        try:
            import ctypes  # noqa: PLC0415
            import ctypes.util  # noqa: PLC0415

            _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            _objc.objc_getClass.restype = ctypes.c_void_p
            _objc.objc_getClass.argtypes = [ctypes.c_char_p]
            _objc.sel_registerName.restype = ctypes.c_void_p
            _objc.sel_registerName.argtypes = [ctypes.c_char_p]
            # objc_msgSend is variadic; declare per-call arg signatures.
            _objc.objc_msgSend.restype = ctypes.c_void_p

            def _send(receiver: int, selector: bytes, *args: object) -> int:
                _objc.objc_msgSend.argtypes = (
                    [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_void_p] * len(args)
                )
                return int(
                    _objc.objc_msgSend(
                        receiver,
                        _objc.sel_registerName(selector),
                        *args,
                    )
                    or 0
                )

            _NSProcessInfo = _objc.objc_getClass(b"NSProcessInfo")
            _NSString = _objc.objc_getClass(b"NSString")
            _process_info = _send(_NSProcessInfo, b"processInfo")
            # +[NSString stringWithUTF8String:] returns an autoreleased NSString.
            _name_ns = _send(_NSString, b"stringWithUTF8String:", b"Open-SSTV")
            _send(_process_info, b"setProcessName:", _name_ns)
        except (OSError, AttributeError, ImportError):
            # ctypes / find_library failure, or a future ABI change.
            # Falling through leaves the Dock label unchanged — cosmetic
            # only, never blocks the GUI.
            pass

    # Qt is imported lazily so the encode/decode CLIs (which never
    # construct a QApplication) don't pay the import cost just because
    # they share a package with the GUI.
    try:
        from PySide6.QtCore import QCoreApplication  # noqa: PLC0415
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

    # (4) Qt application metadata — set via the static QCoreApplication
    # methods *before* QApplication() is constructed.  Qt's docs are
    # explicit that calling these after construction "may not propagate
    # properly to the platform's window system" — which is why the
    # dock/taskbar tooltips ignored them in v0.3.18 and earlier.
    QCoreApplication.setApplicationName("Open-SSTV")
    QCoreApplication.setApplicationVersion(__version__)
    QCoreApplication.setOrganizationName("bucknova")
    QCoreApplication.setOrganizationDomain("github.com/bucknova")

    qt_argv = list(argv) if argv is not None else sys.argv
    app = QApplication(qt_argv)
    # ``setApplicationDisplayName`` lives on QGuiApplication so it has
    # to come after construction.  This is the *user-visible* string
    # (window-bar suffix on Linux, fallback for the WM hint, etc.).
    app.setApplicationDisplayName("Open-SSTV")
    # (5) Linux Wayland: ``setDesktopFileName`` tells the compositor
    # which ``.desktop`` entry owns this top-level — the compositor
    # then uses that file's ``Name=`` / ``Icon=`` for the taskbar
    # tooltip and icon.  No-op outside Wayland; on X11 it's harmless.
    # Our AppImage build emits an ``open-sstv.desktop`` file.
    app.setDesktopFileName("open-sstv")

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
