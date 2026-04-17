# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
#
# PyInstaller spec for Open-SSTV — cross-platform (Windows / macOS / Linux)
#
# Build locally with:
#   pip install pyinstaller pyinstaller-hooks-contrib
#   pyinstaller open_sstv.spec
#
# Output: dist/open-sstv/  (folder containing the launcher + supporting libs)
# Zip the whole dist/open-sstv/ folder and share it — the launcher is:
#   Windows : open-sstv.exe
#   macOS   : open-sstv  (run from terminal; or wrap in a .app manually)
#   Linux   : open-sstv  (or package via appimagetool — see build.yml)

import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# scipy uses lazy/conditional imports internally; collect everything so
# signal.hilbert, butter, sosfiltfilt, and resample_poly all work at runtime.
scipy_hidden = collect_submodules("scipy")

# PySSTV registers mode classes on import; pulling in the whole package is safer.
pyssty_hidden = collect_submodules("PySSTV")

hidden_imports = [
    *scipy_hidden,
    *pyssty_hidden,
    # sounddevice loads PortAudio through ctypes — the shared library is
    # shipped with the sounddevice wheel and PyInstaller copies it
    # automatically, but the module itself still needs to be listed here.
    "sounddevice",
    # tomllib is stdlib on 3.11+; tomli_w is a pure-Python write companion.
    "tomli_w",
]

# platformdirs selects an OS-specific backend at runtime via __import__.
if sys.platform == "win32":
    hidden_imports += ["platformdirs.windows"]
elif sys.platform == "darwin":
    hidden_imports += ["platformdirs.macos"]
else:
    hidden_imports += ["platformdirs.unix"]

# pyserial selects its I/O backend the same way.
if sys.platform == "win32":
    hidden_imports += ["serial.serialwin32", "serial.win32"]
else:
    hidden_imports += ["serial.serialposix"]

# scipy ships .pyd/.dll data alongside its Python modules; include them.
datas = collect_data_files("scipy")

# UPX compresses Mach-O / ELF / PE binaries to shrink bundle size.  On
# macOS (especially Apple Silicon) this is actively harmful: UPX rewrites
# binaries *after* PyInstaller's ad-hoc codesign pass, which invalidates
# every affected ``.dylib`` / ``.so`` signature.  The hardened runtime
# (AMFI) then refuses to load them with::
#
#   code signature in <...> not valid for use in process:
#   library load disallowed by system policy
#
# UPX compression savings are also negligible on modern storage.  Disable
# UPX on Darwin; keep it available on Linux / Windows where it's safe.
UPX_OK = sys.platform != "darwin"

a = Analysis(
    # Entry-point: the same function pyproject.toml's console_script calls.
    ["src/open_sstv/app.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the bundle lean: strip test frameworks and type-stub packages.
    excludes=[
        "pytest",
        "pytest_qt",
        "mypy",
        "ruff",
        "types_Pillow",
        "tkinter",
        "_tkinter",
        "matplotlib",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir mode — DLLs live beside the exe
    name="open-sstv",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=UPX_OK,              # disabled on macOS — UPX breaks ad-hoc codesigns
    console=False,           # no console window (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/icon.ico",  # uncomment and add an .ico when you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=UPX_OK,              # disabled on macOS — UPX breaks ad-hoc codesigns
    upx_exclude=[],
    name="open-sstv",        # dist/open-sstv/  <-- the folder to zip
)
