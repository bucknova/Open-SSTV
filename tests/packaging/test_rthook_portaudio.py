# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Linux PortAudio runtime hook.

``packaging/pyinstaller/rthook_portaudio.py`` is the piece that makes the
bundled PortAudio actually load.  Until v0.6.10 every Linux AppImage and
zip crashed at launch on machines without a system PortAudio
(AppImage/appimage.github.io#7563).  Bundling the library alone would
not have fixed that: PyInstaller patches ``ctypes.util.find_library`` on
Windows only, so on Linux ``sounddevice``'s lookup never looks inside
the bundle.

These tests run the hook the way PyInstaller does, as a plain script at
startup, against a simulated frozen Linux environment, and check what
``sounddevice`` would see afterwards.
"""
from __future__ import annotations

import ctypes.util
import runpy
import sys
from pathlib import Path

import pytest

HOOK = (
    Path(__file__).resolve().parents[2]
    / "packaging" / "pyinstaller" / "rthook_portaudio.py"
)


@pytest.fixture
def system_lookup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the host's ``find_library``: it finds nothing and
    records every name it was asked for.

    ``monkeypatch`` restores the real function afterwards, which also
    undoes whatever the hook installed over this one.
    """
    asked: list[str] = []

    def host_find_library(name: str) -> str | None:
        asked.append(name)
        return None

    monkeypatch.setattr(ctypes.util, "find_library", host_find_library)
    return asked


def _freeze(monkeypatch: pytest.MonkeyPatch, bundle: Path, platform: str = "linux") -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)


def test_bundled_portaudio_is_found_on_a_host_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_lookup: list[str]
) -> None:
    """The #7563 case: host has no PortAudio, bundle does.

    Also checks the import-order claim the fix depends on.  ``sounddevice``
    binds ``find_library`` with ``from ctypes.util import find_library``
    at its own import time, so an import made *after* the hook has run
    must get the patched function.
    """
    bundled = tmp_path / "libportaudio.so.2"
    bundled.write_bytes(b"")
    _freeze(monkeypatch, tmp_path)

    runpy.run_path(str(HOOK))

    from ctypes.util import find_library  # what sounddevice does

    assert find_library("portaudio") == str(bundled)
    assert system_lookup == [], "bundled copy must win without asking the host"


def test_other_libraries_still_go_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_lookup: list[str]
) -> None:
    """Only ``portaudio`` is redirected.  Anything else ``ctypes`` looks up
    at runtime must reach the original ``find_library`` unchanged."""
    (tmp_path / "libportaudio.so.2").write_bytes(b"")
    _freeze(monkeypatch, tmp_path)

    runpy.run_path(str(HOOK))

    assert ctypes.util.find_library("asound") is None
    assert system_lookup == ["asound"]


def test_soname_is_globbed_not_hardcoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_lookup: list[str]
) -> None:
    """PyInstaller keeps whatever filename the build machine resolved.  A
    future soname bump must not break the hook without any error."""
    bundled = tmp_path / "libportaudio.so.3"
    bundled.write_bytes(b"")
    _freeze(monkeypatch, tmp_path)

    runpy.run_path(str(HOOK))

    assert ctypes.util.find_library("portaudio") == str(bundled)


def test_no_bundled_copy_leaves_lookup_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_lookup: list[str]
) -> None:
    """With nothing bundled, the host's PortAudio (if any) must still be
    reachable.  That's the stopgap the in-app error dialog suggests."""
    _freeze(monkeypatch, tmp_path)
    before = ctypes.util.find_library

    runpy.run_path(str(HOOK))

    assert ctypes.util.find_library is before


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_inert_off_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_lookup: list[str],
    platform: str,
) -> None:
    """macOS and Windows already get PortAudio from the sounddevice
    wheel.  The spec only registers this hook on Linux, but it must stay
    inert elsewhere too."""
    (tmp_path / "libportaudio.so.2").write_bytes(b"")
    _freeze(monkeypatch, tmp_path, platform=platform)
    before = ctypes.util.find_library

    runpy.run_path(str(HOOK))

    assert ctypes.util.find_library is before


def test_inert_when_not_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_lookup: list[str]
) -> None:
    """A source or pip install must keep using the system PortAudio."""
    (tmp_path / "libportaudio.so.2").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    before = ctypes.util.find_library

    runpy.run_path(str(HOOK))

    assert ctypes.util.find_library is before
