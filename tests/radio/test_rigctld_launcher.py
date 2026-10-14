# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.6.14 audit UI-H1 / security info: the shared rigctld launcher."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from open_sstv.radio import rigctld_launcher as rl

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script fake")


def _fake_rigctld(directory: Path, body: str) -> Path:
    exe = directory / "rigctld"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


@posix_only
def test_executable_found_on_absolute_path_entry(tmp_path: Path, monkeypatch) -> None:
    exe = _fake_rigctld(tmp_path, "exit 0")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert rl.find_rigctld_executable() == str(exe)


@posix_only
def test_current_directory_is_never_searched(tmp_path: Path, monkeypatch) -> None:
    """A rigctld planted in the CWD must not be picked up via an empty or
    relative PATH entry (Windows CWD-hijack class)."""
    _fake_rigctld(tmp_path, "exit 0")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin"]))
    assert rl.find_rigctld_executable() is None


def test_launch_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(rl, "find_rigctld_executable", lambda: None)
    with pytest.raises(FileNotFoundError):
        rl.launch_rigctld(1, 4532)


def test_argv_rejects_flag_smuggling() -> None:
    with pytest.raises(ValueError):
        rl.build_rigctld_argv("/x/rigctld", 1, 4532, "--help")
    assert rl.build_rigctld_argv("/x/rigctld", 1035, 4532, "/dev/ttyUSB0", 19200) == [
        "/x/rigctld", "-m", "1035", "-t", "4532", "-r", "/dev/ttyUSB0", "-s", "19200",
    ]


@posix_only
def test_launched_rigctld_stderr_is_drained(tmp_path: Path, monkeypatch) -> None:
    """UI-H1: ~200 KB of stderr would wedge an undrained child forever."""
    _fake_rigctld(
        tmp_path,
        "i=0\n"
        "while [ $i -lt 2000 ]; do\n"
        "  echo 'hamlib chatter xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' >&2\n"
        "  i=$((i+1))\n"
        "done\n",
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    proc = rl.launch_rigctld(1, 4532)
    try:
        assert proc.wait(timeout=10) == 0, "rigctld wedged on a full stderr pipe"
    finally:
        rl.stop_rigctld(proc)


@posix_only
def test_stop_reaps_a_sigterm_ignoring_child() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    )
    time.sleep(0.2)
    rl.stop_rigctld(proc, timeout_s=0.3)
    assert proc.returncode is not None, "child must be reaped, not zombied"


def test_stop_none_is_noop() -> None:
    rl.stop_rigctld(None)
