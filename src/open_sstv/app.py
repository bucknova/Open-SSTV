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


def _config_log_level() -> int:
    """Resolve the configured log level (v0.4 Settings → Logging tab).

    Reads ``AppConfig.log_level`` from the saved TOML.  Defensive on
    every failure path — a corrupt or unreadable config must never
    block startup, and the level falls back to INFO exactly as it was
    before the field existed.  ``AppConfig.__post_init__`` has already
    normalised the value to one of DEBUG/INFO/WARNING/ERROR.
    """
    import logging  # noqa: PLC0415

    try:
        from open_sstv.config.store import load_config  # noqa: PLC0415
        name = load_config().log_level
    except Exception:  # noqa: BLE001
        return logging.INFO
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }.get(name, logging.INFO)


def _qt_import_error_text(exc: ImportError) -> str:
    """Advice when PySide6 can't be imported, keeping the real cause."""
    lines = ["Error: could not load PySide6 (Qt).", f"Details: {exc}", ""]
    if ".so" in str(exc) or "DLL" in str(exc):
        lines.append(
            "A system library Qt needs is missing. Install the library "
            "named above with your package manager."
        )
    elif getattr(sys, "frozen", False):
        lines.append(
            "This build should include PySide6. Please report this at "
            "https://github.com/bucknova/Open-SSTV/issues"
        )
    else:
        lines.append("Install it with:  pip install PySide6")
    return "\n".join(lines)


def _audio_library_error_text(exc: OSError) -> str:
    """Advice for an operator whose system couldn't load PortAudio.

    ``sounddevice`` loads PortAudio at import time.  When that fails it
    raises ``OSError``: either "PortAudio library not found" or a
    ``dlopen`` error naming whichever library is missing.  ``OSError`` is
    not ``ImportError``, so it used to slip past the import guard in
    ``main`` and kill the app with a bare traceback.  From a
    double-clicked AppImage, which has no terminal, that meant nothing
    happened at all.
    """
    lines = [
        "Open-SSTV could not load its audio library (PortAudio).",
        "",
        f"Details: {exc}",
        "",
    ]
    report = [
        "Please report this at https://github.com/bucknova/Open-SSTV/issues",
        "and include the details above.",
    ]
    if sys.platform.startswith("linux"):
        frozen = getattr(sys, "frozen", False)
        if frozen and "libasound" in str(exc):
            # The AppImage and zip bundle PortAudio but take ALSA from the
            # host (bundling it breaks PipeWire / PulseAudio).
            lines += [
                "This build includes PortAudio but uses your system's ALSA",
                "library, which is missing. Install it with your package",
                "manager:",
                "",
                "  Debian / Ubuntu:  sudo apt install libasound2",
                "  Fedora:           sudo dnf install alsa-lib",
                "  Arch:             sudo pacman -S alsa-lib",
            ]
        elif frozen:
            # The bundled PortAudio should have loaded.  If it didn't, that
            # is a packaging bug on our side.  A system PortAudio works as
            # a stopgap, because the runtime hook only overrides the lookup
            # when a bundled copy exists.
            lines += [
                "This build should include PortAudio, so this is a bug in",
                "how Open-SSTV was packaged.",
                *report,
                "",
                "Until it's fixed, installing PortAudio yourself may work:",
                "",
                "  Debian / Ubuntu:  sudo apt install libportaudio2",
                "  Fedora:           sudo dnf install portaudio",
                "  Arch:             sudo pacman -S portaudio",
            ]
        else:
            lines += [
                "Install PortAudio with your package manager:",
                "",
                "  Debian / Ubuntu:  sudo apt install libportaudio2",
                "  Fedora:           sudo dnf install portaudio",
                "  Arch:             sudo pacman -S portaudio",
            ]
    else:
        # macOS and Windows builds get PortAudio from the sounddevice
        # wheel, so reaching this point means a damaged install.
        lines += [
            "Reinstalling Open-SSTV should fix this. If it doesn't:",
            *report,
        ]
    return "\n".join(lines)


def _quit_signals() -> list[signal.Signals]:
    """Signals that mean "shut down now, cleanly".

    SIGHUP is POSIX-only.  It's what a terminal sends when its window
    closes, and its default action kills the process without running
    ``closeEvent``, so mid-TX it left PTT keyed.
    """
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        sigs.append(signal.SIGHUP)
    return sigs


def install_quit_signals(app: object) -> None:
    """Route SIGINT / SIGTERM (and SIGHUP on POSIX) to a clean ``app.quit()``.

    A plain ``signal.signal(SIGTERM, ...)`` doesn't work under Qt.  CPython
    runs a Python signal handler only between bytecodes, and while Qt's
    event loop sits idle in C++ no bytecode runs.  The handler is queued
    and never executes, and since it replaced the default action, the
    signal is ignored.  Until the 2026-10 stability audit that is exactly
    what happened: ``kill``, ``systemctl stop``, a logout or Ctrl-C reached
    an idle Open-SSTV and did nothing.  The release smoke test saw it ignore
    SIGTERM for two hours.  When the OS then escalated to SIGKILL,
    ``closeEvent`` never ran, so a keyed rig stayed keyed and a spawned
    rigctld was orphaned.

    The fix is CPython's ``set_wakeup_fd``.  The C-level handler writes the
    signal number to a socket, a ``QSocketNotifier`` on the other end wakes
    Qt's event loop, the drain slot runs Python code, and the queued Python
    handler gets its turn.  ``app.quit()`` then closes the window, so
    ``closeEvent`` drops PTT and tears down normally.

    A second signal while shutting down is logged and ignored rather than
    forcing an exit, because the first one is already running the unkey
    path.  SIGKILL remains the way to force it.
    """
    import logging  # noqa: PLC0415
    import socket  # noqa: PLC0415

    from PySide6.QtCore import QSocketNotifier  # noqa: PLC0415

    log = logging.getLogger("open_sstv")
    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)
    try:
        signal.set_wakeup_fd(wsock.fileno(), warn_on_full_buffer=False)
    except (ValueError, OSError) as exc:
        # Only possible off the main thread or on an exotic platform.  The
        # handlers below still work whenever Python code happens to run.
        log.warning("signal wakeup unavailable (%s); quit signals may be slow", exc)

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

    def _drain() -> None:
        # Running this slot is what matters: it gives CPython a bytecode
        # boundary at which to run the pending Python handler.
        try:
            while rsock.recv(4096):
                pass
        except OSError:  # BlockingIOError once empty
            pass

    notifier.activated.connect(_drain)

    shutting_down = False

    def _on_quit_signal(signum: int, _frame: object) -> None:
        nonlocal shutting_down
        name = signal.Signals(signum).name
        if shutting_down:
            log.info("%s received while already shutting down; ignoring", name)
            return
        shutting_down = True
        log.info("%s received; shutting down cleanly", name)
        app.quit()  # type: ignore[attr-defined]

    for sig in _quit_signals():
        signal.signal(sig, _on_quit_signal)

    # Keep the sockets and notifier alive for the app's lifetime.  If they
    # were garbage-collected, the wakeup fd would dangle.
    app._open_sstv_quit_signal_wakeup = (rsock, wsock, notifier)  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``open-sstv`` console script and ``python -m open_sstv``."""
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    # OPEN_SSTV_DEBUG wins over the configured level so "set this env
    # var and re-run" stays a one-step support instruction even when
    # the user has Settings → Logging set to WARNING/ERROR.
    log_level = (
        logging.DEBUG if os.environ.get("OPEN_SSTV_DEBUG") else _config_log_level()
    )
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
    # ...and into the *log file* too.  The print above goes to stderr, which
    # on a packaged Windows GUI build is discarded — so every bug report
    # arrived with a log that never said which version produced it.
    logging.getLogger("open_sstv").info(
        "Open-SSTV v%s starting — module loaded from %s", __version__, _pkg.__file__
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
    except ImportError as exc:
        # Not only "PySide6 absent": a PySide6 that is present but can't
        # load a Qt system library (libEGL, libxkbcommon, ...) raises the
        # same ImportError.  This handler used to say "not installed" in
        # both cases and drop the real error, which was wrong for every
        # bundled build.
        print(_qt_import_error_text(exc), file=sys.stderr)
        return 1

    try:
        from open_sstv.ui.main_window import MainWindow  # noqa: PLC0415
    except ImportError as exc:
        missing = str(exc).replace("No module named ", "").strip("'\"")
        print(
            f"Error: required dependency '{missing}' is not installed.\n"
            f"Install all dependencies with:  pip install open-sstv",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        # PortAudio failed to load (see _audio_library_error_text).  The
        # message goes to stderr for terminal users and to a dialog for
        # everyone else.  A launcher-started app has no terminal, and
        # without the dialog this failure is completely silent.
        text = _audio_library_error_text(exc)
        print(text, file=sys.stderr)
        from PySide6.QtWidgets import QMessageBox  # noqa: PLC0415

        _dialog_app = QApplication.instance() or QApplication(
            list(argv) if argv is not None else sys.argv
        )
        QMessageBox.critical(None, "Open-SSTV — audio library missing", text)
        del _dialog_app
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

    # Route SIGTERM (systemd stop, kill PID, logout), SIGINT (Ctrl-C) and
    # SIGHUP (terminal closed) through Qt's event loop, so closeEvent fires
    # and PTT is unkeyed cleanly.  See install_quit_signals for why a plain
    # signal.signal() call doesn't work while Qt is idle.
    install_quit_signals(app)

    rc = app.exec()
    _exit_now_if_threads_were_detached(rc)
    return rc


def _exit_now_if_threads_were_detached(rc: int) -> None:
    """Leave with ``os._exit`` if shutdown had to detach a running thread.

    ``closeEvent`` detaches a worker thread that won't stop in time rather
    than kill it (see ``main_window._DETACHED_AT_SHUTDOWN``).  If this
    function then returned normally, interpreter finalization would destroy
    that still-running ``QThread`` and Qt would abort the process, measured
    as exit 134 every time.  Everything worth saving is already saved by
    this point: closeEvent wrote config and closed the logbook.  So flush
    the logs and leave directly.  It does nothing on a normal shutdown.
    """
    from open_sstv.ui.main_window import _DETACHED_AT_SHUTDOWN  # noqa: PLC0415

    if not _DETACHED_AT_SHUTDOWN:
        return
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415

    logging.getLogger("open_sstv").warning(
        "%d worker thread(s) were still running at shutdown; exiting "
        "immediately so Qt doesn't abort on them", len(_DETACHED_AT_SHUTDOWN),
    )
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001, S110 — exiting regardless
            pass
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
