# SPDX-License-Identifier: GPL-3.0-or-later
"""Launching and stopping a local ``rigctld`` daemon — one shared helper.

v0.6.14 audit UI-H1: there were two copies of the rigctld launch code, one
in ``MainWindow`` (auto-launch on Connect) and one in the Settings dialog
(the "Launch rigctld" button).  Only the first drained the child's stderr.
A Settings-launched rigctld was adopted by the main window with
``stderr=PIPE`` that nobody read; after ~64 KB of hamlib chatter (a flaky
CAT cable produces it quickly) rigctld blocked on ``write(2)`` and stopped
servicing CAT and PTT, possibly mid-TX.  That recreated v0.4.0 audit
high #2.  Both paths now come through :func:`launch_rigctld`, which always
drains.

It also resolves the executable to an absolute path from ``PATH`` only
(security info item): on Windows ``Popen(["rigctld"])`` — and even
``shutil.which`` — search the current directory first, so a
``rigctld.exe`` dropped next to a downloaded file could be run instead.

Qt-free, so it is unit-testable without a window.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

from open_sstv.radio.rigctld import is_safe_rigctld_arg

_log = logging.getLogger(__name__)

RIGCTLD_EXECUTABLE = "rigctld"

#: Issue #90: serial-line settings, keyed by ``AppConfig`` attribute, with
#: the values Hamlib accepts for each.  The first entry ("" or 0) means
#: "don't pass it — use the backend's default".
SERIAL_CONF_CHOICES: dict[str, tuple] = {
    "rig_serial_handshake": ("", "None", "XONXOFF", "Hardware"),
    "rig_data_bits": (0, 7, 8),
    "rig_stop_bits": (0, 1, 2),
    "rig_serial_parity": ("", "None", "Odd", "Even", "Mark", "Space"),
    "rig_rts_state": ("", "ON", "OFF"),
    "rig_dtr_state": ("", "ON", "OFF"),
}

#: ``AppConfig`` attribute → Hamlib conf parameter name.
_HAMLIB_CONF_NAMES: dict[str, str] = {
    "rig_serial_handshake": "serial_handshake",
    "rig_data_bits": "data_bits",
    "rig_stop_bits": "stop_bits",
    "rig_serial_parity": "serial_parity",
    "rig_rts_state": "rts_state",
    "rig_dtr_state": "dtr_state",
}


def serial_conf_from(source: object) -> dict[str, str]:
    """Hamlib ``-C`` params for the serial-line settings on *source*.

    *source* is anything with the ``SERIAL_CONF_CHOICES`` attributes (an
    ``AppConfig``, or a namespace built from the Settings dialog).
    Default ("" / 0) and unknown values are left out, so Hamlib keeps its
    own default for them.  RTS is also left out when the handshake is
    Hardware: Hamlib then owns the RTS line and refuses ``rts_state``.
    """
    conf: dict[str, str] = {}
    for attr, choices in SERIAL_CONF_CHOICES.items():
        value = getattr(source, attr, choices[0])
        if value in ("", 0) or value not in choices:
            continue
        conf[_HAMLIB_CONF_NAMES[attr]] = str(value)
    if conf.get("serial_handshake") == "Hardware":
        conf.pop("rts_state", None)
    return conf


def find_rigctld_executable(name: str = RIGCTLD_EXECUTABLE) -> str | None:
    """Absolute path of *name* found on ``PATH``, never the current directory.

    Relative and empty ``PATH`` entries (which mean "current directory")
    are skipped, and unlike ``shutil.which`` the CWD is not implicitly
    searched first on Windows.
    """
    exts = [""]
    if sys.platform == "win32":
        exts = [
            e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e
        ]
        if os.path.splitext(name)[1]:
            exts = [""] + exts
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def build_rigctld_argv(
    executable: str,
    model_id: int,
    tcp_port: int,
    serial_port: str = "",
    baud_rate: int = 0,
    serial_conf: dict[str, str] | None = None,
) -> list[str]:
    """The rigctld command line.  Raises ``ValueError`` for an unsafe port.

    OP-13: a serial-port value starting with ``-`` would be read by rigctld
    as a flag, so it is refused here for both launch paths.  *serial_conf*
    (from :func:`serial_conf_from`) is checked against the known Hamlib
    names and values for the same reason.
    """
    if not is_safe_rigctld_arg(serial_port):
        raise ValueError(
            f"refusing to launch rigctld with serial port {serial_port!r} — "
            "values starting with '-' would be read as rigctld flags"
        )
    cmd = [executable, "-m", str(int(model_id)), "-t", str(int(tcp_port))]
    if serial_port:
        cmd += ["-r", serial_port]
    if baud_rate:
        cmd += ["-s", str(int(baud_rate))]
    if serial_conf:
        allowed = {
            _HAMLIB_CONF_NAMES[attr]: {str(c) for c in choices if c not in ("", 0)}
            for attr, choices in SERIAL_CONF_CHOICES.items()
        }
        for name, value in serial_conf.items():
            if value not in allowed.get(name, ()):
                raise ValueError(
                    f"refusing to launch rigctld with serial setting "
                    f"{name}={value!r}"
                )
        cmd += ["-C", ",".join(f"{k}={v}" for k, v in serial_conf.items())]
    return cmd


def drain_subprocess_stderr(
    proc: subprocess.Popen, name: str, logger: logging.Logger | None = None
) -> None:
    """Pump *proc*'s stderr into the app log on a daemon thread.

    v0.4.0 audit high #2: a child launched with ``stderr=PIPE`` that
    nobody reads wedges once the OS pipe buffer (~64 KB) fills — the
    child blocks on ``write(2)`` and stops doing its real job.  For
    rigctld that means the daemon stops servicing CAT commands, worst
    case with PTT keyed.  The pump thread is a daemon so it can never
    hold the interpreter open; it exits when the child closes stderr.
    """
    if proc.stderr is None:  # not launched with stderr=PIPE — nothing to do
        return
    log = logger if logger is not None else _log

    def _pump() -> None:
        try:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    log.info("%s: %s", name, line)
        except Exception:  # noqa: BLE001 — the drain must never die loudly
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(
        target=_pump, name=f"{name}-stderr-drain", daemon=True
    ).start()


def launch_rigctld(
    model_id: int,
    tcp_port: int,
    serial_port: str = "",
    baud_rate: int = 0,
    serial_conf: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start rigctld with its stderr drained.  The one launch path.

    Raises ``FileNotFoundError`` if rigctld isn't on ``PATH``,
    ``ValueError`` for an unsafe serial port, and ``OSError`` if the
    spawn itself fails.
    """
    exe = find_rigctld_executable()
    if exe is None:
        raise FileNotFoundError("rigctld not found on PATH")
    cmd = build_rigctld_argv(
        exe, model_id, tcp_port, serial_port, baud_rate, serial_conf
    )
    _log.info("launching rigctld: %s", " ".join(cmd))
    # OP2-14: start_new_session=True on POSIX isolates the child into its
    # own process group so a GUI crash/SIGKILL doesn't orphan rigctld
    # holding the serial port.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    drain_subprocess_stderr(proc, "rigctld")
    return proc


def stop_rigctld(proc: subprocess.Popen | None, timeout_s: float = 3.0) -> None:
    """Terminate *proc* and reap it, escalating to kill().  Never raises.

    Always waits after kill() so the child can't linger as a zombie and a
    respawned rigctld can't race the dying one for the serial port and
    TCP listen port (v0.4.0 audit low #14; v0.6.14 audit for the Settings
    copy, which skipped it).
    """
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                pass
    except (ProcessLookupError, OSError):
        # Already gone — nothing to do.
        pass


__all__ = [
    "SERIAL_CONF_CHOICES",
    "build_rigctld_argv",
    "drain_subprocess_stderr",
    "find_rigctld_executable",
    "launch_rigctld",
    "serial_conf_from",
    "stop_rigctld",
]
