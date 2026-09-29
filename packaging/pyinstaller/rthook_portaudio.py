# SPDX-License-Identifier: GPL-3.0-or-later
"""PyInstaller runtime hook: load the bundled PortAudio on Linux.

``sounddevice`` finds PortAudio on Linux through exactly one call,
``ctypes.util.find_library("portaudio")``. On Linux that consults the
system linker cache (``ldconfig -p``) and never looks inside the frozen
app. PyInstaller patches ``find_library`` for Windows only; on macOS it
prepends ``sys._MEIPASS`` to the dyld fallback path instead. On Linux,
nothing points the lookup at the bundle.

So bundling ``libportaudio.so.2`` is not enough by itself. On a machine
without a system PortAudio, ``find_library`` returns ``None`` and
``sounddevice`` raises ``OSError: PortAudio library not found`` at import,
before any window appears. That is how every Linux AppImage and zip
through v0.6.10 shipped (AppImage/appimage.github.io#7563).

This hook runs before the app's own imports, and so before
``sounddevice``'s ``from ctypes.util import find_library``. It wraps
``find_library`` so that ``"portaudio"`` resolves to the bundled library
when one is present. Every other name goes to the original function.
The hook does nothing unless it is running frozen on Linux, and it does
nothing if no bundled copy exists.

It prefers the bundled copy over any system one. That PortAudio was
built for this app (ALSA only; see ``build.yml``), and loading it by full
path means a user's distro upgrade cannot change which PortAudio the app
gets.
"""
from __future__ import annotations

import ctypes.util
import glob
import os
import sys


def _bundled_portaudio(bundle_dir: str) -> str | None:
    """Full path of the bundled ``libportaudio.so*``, or ``None``.

    Globbed rather than hard-coded: PyInstaller keeps the file under
    whatever name the build machine's resolved library had
    (``libportaudio.so.2`` today). A soname bump would otherwise break
    this hook without any error.
    """
    matches = sorted(glob.glob(os.path.join(bundle_dir, "libportaudio.so*")))
    return matches[0] if matches else None


def _install() -> None:
    if not sys.platform.startswith("linux") or not getattr(sys, "frozen", False):
        return
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is None:
        return
    bundled = _bundled_portaudio(bundle_dir)
    if bundled is None:
        return

    original = ctypes.util.find_library

    def find_library(name: str) -> str | None:
        if name == "portaudio":
            return bundled
        return original(name)

    ctypes.util.find_library = find_library


_install()
