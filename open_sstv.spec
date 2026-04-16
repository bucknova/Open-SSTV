# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
#
# PyInstaller spec for Open-SSTV (Windows build)
#
# Build locally with:
#   pip install pyinstaller
#   pyinstaller open_sstv.spec
#
# Output: dist/open-sstv/open-sstv.exe  (+ supporting DLLs)
# Zip the whole dist/open-sstv/ folder and share it — the exe is the launcher.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# scipy uses lazy/conditional imports internally; collect everything so
# signal.hilbert, butter, sosfiltfilt, and resample_poly all work at runtime.
scipy_hidden = collect_submodules("scipy")

# PySSTV registers mode classes on import; pulling in the whole package is safer.
pyssty_hidden = collect_submodules("PySSTV")

hidden_imports = [
    *scipy_hidden,
    *pyssty_hidden,
    # sounddevice loads PortAudio through ctypes — the DLL is shipped with the
    # sounddevice wheel and PyInstaller copies it automatically, but the module
    # itself still needs to be on the hidden-import list.
    "sounddevice",
    # platformdirs uses __import__ to pick the OS backend at runtime.
    "platformdirs.windows",
    # pyserial platform back-ends (serial.serialwin32 is the Windows one).
    "serial.serialwin32",
    "serial.win32",
    # tomllib is stdlib on 3.11+; tomli_w is a pure-Python write companion.
    "tomli_w",
]

# scipy ships .pyd/.dll data alongside its Python modules; include them.
datas = collect_data_files("scipy")

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
    upx=True,                # compress binaries with UPX if available
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
    upx=True,
    upx_exclude=[],
    name="open-sstv",        # dist/open-sstv/  <-- the folder to zip
)
