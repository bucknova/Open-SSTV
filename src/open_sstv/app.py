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
from pathlib import Path

from open_sstv import __version__


def _set_macos_process_name(name: str) -> None:
    """Set ``-[NSProcessInfo processName]`` via libobjc ctypes (macOS only).

    No-op on non-macOS platforms.  Wrapped in a broad try/except so a
    libobjc lookup failure (PyInstaller bundle without the cdll
    search path, exotic distro) or a future Objective-C ABI change
    never blocks GUI launch — losing the friendly name is cosmetic.

    Called twice from ``main()`` — once before ``QApplication()`` is
    constructed (so the very first Dock-icon render gets the right
    name) and once immediately after (because Qt's NSApplication init
    resets the name back to whatever macOS derives from the embedded
    Python.framework's ``CFBundleName``, which is ``"Python"``).  The
    post-QApplication call is what actually sticks for the Dock; the
    pre-QApplication call is defence against any other code path that
    queries the name during Qt initialization.
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes  # noqa: PLC0415
        import ctypes.util  # noqa: PLC0415

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        # objc_msgSend is variadic; declare per-call arg signatures.
        objc.objc_msgSend.restype = ctypes.c_void_p

        def send(receiver: int, selector: bytes, *args: object) -> int:
            objc.objc_msgSend.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_void_p] * len(args)
            )
            return int(
                objc.objc_msgSend(
                    receiver,
                    objc.sel_registerName(selector),
                    *args,
                )
                or 0
            )

        ns_process_info = objc.objc_getClass(b"NSProcessInfo")
        ns_string = objc.objc_getClass(b"NSString")
        process_info = send(ns_process_info, b"processInfo")
        # +[NSString stringWithUTF8String:] returns an autoreleased NSString.
        name_ns = send(ns_string, b"stringWithUTF8String:", name.encode("utf-8"))
        send(process_info, b"setProcessName:", name_ns)
    except (OSError, AttributeError, ImportError):
        pass


def _verify_bundled_assets() -> list[str]:
    """Return a list of missing critical bundled assets.

    The PyInstaller spec used to bundle only scipy data (open_sstv
    assets silently shipped empty between v0.3.0 and v0.3.19).  The
    v0.3.20 spec adds ``collect_data_files("open_sstv")`` to fix
    this, but the failure mode (template renders crash, taskbar icon
    is generic) is invisible enough that a regression could go
    unnoticed for releases.

    This check runs at app startup, walks the import-resources tree,
    and returns the human-readable names of any expected bundled
    assets that are missing.  Empty list means everything's where it
    should be.  Caller logs and surfaces to the UI; never blocks the
    GUI from starting because partial assets are still better than no
    app.
    """
    missing: list[str] = []
    try:
        import importlib.resources as _res  # noqa: PLC0415

        checks = [
            ("DejaVu Sans Bold font", ("assets", "fonts", "DejaVuSans-Bold.ttf")),
            ("App icon PNG", ("assets", "icons", "Open-SSTV.png")),
            ("TX default image", ("assets", "testimage.jpg")),
        ]
        root = _res.files("open_sstv")
        for label, parts in checks:
            ref = root
            for part in parts:
                ref = ref / part
            try:
                with _res.as_file(ref) as p:
                    if not p.exists():
                        missing.append(label)
            except (FileNotFoundError, OSError):
                missing.append(label)
    except ModuleNotFoundError:
        missing.append("open_sstv package itself")
    return missing


def _user_log_dir() -> Path | None:
    """Return ``platformdirs.user_log_dir("open_sstv")`` as a Path, or None.

    Wrapped in try/except because a misconfigured environment (no HOME,
    locked-down container) could fault on the platformdirs call.  Logging
    setup must never block the GUI from starting; if this returns None
    the FileHandler is skipped silently and only stderr logging applies.
    """
    try:
        import platformdirs  # noqa: PLC0415
        return Path(platformdirs.user_log_dir("open_sstv"))
    except Exception:  # noqa: BLE001
        return None


def _setup_logging(log_level: int) -> Path | None:
    """Configure root logger with stderr AND a rotating file handler.

    v0.3.21: the Windows ``.exe`` is built with ``console=False`` so
    ``sys.stderr`` is attached to a null sink — every log message
    vanishes and the user has no way to share diagnostics.  And even
    when the user *does* run from a terminal (the macOS / Linux
    case), telling them to scroll back through stderr to file an
    issue is a bad UX.

    Always activate a ``RotatingFileHandler`` writing to
    ``platformdirs.user_log_dir("open_sstv")/open-sstv.log`` so the
    new Settings → Diagnostics export button has something to bundle
    regardless of launch method.  The stderr handler is kept as well,
    so terminal users continue to see live output.

    Bounded by ``RotatingFileHandler``'s default ``maxBytes=2 MB``
    and ``backupCount=2`` — total disk usage capped at ~6 MB across
    the rotated set.  Skip silently if the log dir can't be created
    (locked-down container, read-only filesystem) — logging must
    never block the GUI from starting.

    Returns the log file path that was activated, or ``None`` if no
    file handler was wired up.  The caller logs the path so the very
    first line of every session has a "logs are at X" breadcrumb.
    """
    import logging  # noqa: PLC0415
    import logging.handlers as _lh  # noqa: PLC0415

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt)

    log_dir = _user_log_dir()
    if log_dir is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "open-sstv.log"
        handler = _lh.RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,   # 2 MB per file
            backupCount=2,              # keep 2 rotated + 1 current = 3 max
            encoding="utf-8",
        )
        handler.setLevel(log_level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)
        return path
    except OSError:
        # Locked-down or read-only user dir; degrade silently to
        # stderr-only logging.
        return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``open-sstv`` console script and ``python -m open_sstv``."""
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    log_level = logging.DEBUG if os.environ.get("OPEN_SSTV_DEBUG") else logging.INFO
    _log_file_path = _setup_logging(log_level)
    if _log_file_path is not None:
        logging.getLogger("open_sstv").info(
            "Logging to %s (stderr handler unchanged)", _log_file_path
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
    # processInfo] processName]``.  In v0.3.19 the call below was
    # invoked only once, before QApplication construction.  User
    # testing against the v0.3.19 PyInstaller binary showed the Dock
    # still read "Python" — turns out Qt's NSApplication init during
    # QApplication() construction resets the name back to whatever
    # macOS derives from the embedded Python.framework's Info.plist
    # (``CFBundleName = "Python"``).  v0.3.20 calls the helper both
    # BEFORE QApplication (so the very first Dock-icon render is
    # right) AND AFTER (so the post-QApplication name sticks).
    # Belt-and-braces.  See the matching call further down after
    # ``app = QApplication(...)``.
    _set_macos_process_name("Open-SSTV")

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
    # v0.3.20: re-apply NSProcessInfo.setProcessName *after* QApplication
    # is constructed.  Qt's NSApplication init during QApplication()
    # resets the process name back to whatever macOS derives from the
    # embedded ``Python.framework``'s ``CFBundleName`` (which is
    # ``"Python"``), so the v0.3.19 pre-QApplication call was silently
    # overwritten in the PyInstaller bundle.  Calling it again post-
    # QApplication is what actually sticks for the Dock.
    _set_macos_process_name("Open-SSTV")
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

    # v0.3.20: verify the bundled assets actually shipped.  Between
    # v0.3.0 and v0.3.19 the PyInstaller spec silently failed to bundle
    # ``src/open_sstv/assets/`` — fonts, templates, icons, and the
    # default TX photo all went missing from every binary release.
    # The user-visible symptoms were generic icons, garbled template
    # renders, and the H-6 "Fallback font not found" error.  We now
    # bundle them (see open_sstv.spec datas), and this startup check
    # gives a clear log + status-bar warning if a future regression
    # ships an incomplete bundle again.  Non-blocking: a partial
    # install is still better than refusing to launch.
    _missing_assets = _verify_bundled_assets()
    if _missing_assets:
        _msg = (
            "Open-SSTV installation appears incomplete — missing bundled "
            "assets: " + ", ".join(_missing_assets) + ".  Templates may "
            "fail to render and the app icon may be generic.  Re-download "
            "from https://github.com/bucknova/Open-SSTV/releases/latest."
        )
        logging.getLogger("open_sstv").warning(_msg)

    # Start with ManualRig (no-op). The user clicks "Connect Rig" in
    # the radio panel to establish a live rigctld link at runtime.
    window = MainWindow()
    if _missing_assets:
        # Surface the warning to the user via the status bar too —
        # log-only is invisible to GUI users.
        try:
            window.statusBar().showMessage(
                "Installation incomplete — see terminal for details", 0
            )
        except AttributeError:
            pass
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
