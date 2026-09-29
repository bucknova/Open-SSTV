# SPDX-License-Identifier: GPL-3.0-or-later
"""Startup behaviour when PortAudio can't be loaded.

``sounddevice`` raises ``OSError`` at import when PortAudio is missing.
``app.main`` guarded the ``MainWindow`` import against ``ImportError``
only, so this failure escaped as a bare traceback.  From a double-clicked
AppImage there is no terminal, so the app simply never appeared
(AppImage/appimage.github.io#7563).  It now explains the problem in a
dialog and exits with status 1.
"""
from __future__ import annotations

import sys
import types

import pytest

from open_sstv import app

NOT_FOUND = OSError("PortAudio library not found")
NO_ALSA = OSError(
    "cannot load library '/tmp/_MEI/libportaudio.so.2': "
    "libasound.so.2: cannot open shared object file: No such file or directory"
)


def _set_env(monkeypatch: pytest.MonkeyPatch, platform: str, frozen: bool) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)


# ---------------------------------------------------------------------------
# The advice text: each case must point at the right fix
# ---------------------------------------------------------------------------

def test_linux_source_install_is_told_to_install_portaudio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pip / pipx on Linux: the wheel has no PortAudio, so the distro must
    provide it."""
    _set_env(monkeypatch, "linux", frozen=False)
    text = app._audio_library_error_text(NOT_FOUND)
    assert "sudo apt install libportaudio2" in text
    assert "PortAudio library not found" in text  # the real error is shown


def test_linux_bundle_missing_alsa_is_told_to_install_alsa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AppImage bundles PortAudio but takes ALSA from the host.  If
    ALSA is what's missing, suggesting libportaudio2 would be wrong."""
    _set_env(monkeypatch, "linux", frozen=True)
    text = app._audio_library_error_text(NO_ALSA)
    assert "sudo apt install libasound2" in text
    assert "libportaudio2" not in text


def test_linux_bundle_missing_portaudio_is_reported_as_our_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle is supposed to include PortAudio.  If it doesn't load,
    that's a packaging bug: say so, ask for a report, and offer the
    system library as a stopgap."""
    _set_env(monkeypatch, "linux", frozen=True)
    text = app._audio_library_error_text(NOT_FOUND)
    assert "bug" in text
    assert "github.com/bucknova/Open-SSTV/issues" in text
    assert "sudo apt install libportaudio2" in text
    assert "libasound2" not in text


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_macos_and_windows_are_told_to_reinstall(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Those builds get PortAudio from the sounddevice wheel.  No Linux
    package-manager advice there."""
    _set_env(monkeypatch, platform, frozen=True)
    text = app._audio_library_error_text(NOT_FOUND)
    assert "Reinstalling" in text
    assert "apt" not in text


# ---------------------------------------------------------------------------
# main(): the failure is caught, shown, and exits 1
# ---------------------------------------------------------------------------

class _PortAudioMissing(types.ModuleType):
    """Stands in for ``open_sstv.ui.main_window`` when its
    ``import sounddevice`` has blown up.  ``from … import MainWindow``
    reaches ``__getattr__``, and an ``OSError`` raised there propagates
    unchanged, just like the real import failure."""

    def __getattr__(self, name: str) -> object:
        raise NOT_FOUND


def test_main_shows_dialog_and_exits_1(
    qapp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Keep main() from touching the real log dir, logging config and argv.
    monkeypatch.setattr(app, "_setup_logging", lambda level: None)
    monkeypatch.setattr(app, "_config_log_level", lambda: 20)
    monkeypatch.setattr(app, "_set_macos_process_name", lambda name: None)
    monkeypatch.setattr(sys, "argv", ["open-sstv"])
    monkeypatch.setitem(
        sys.modules, "open_sstv.ui.main_window",
        _PortAudioMissing("open_sstv.ui.main_window"),
    )

    shown: list[tuple[str, str]] = []
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "critical",
        staticmethod(lambda parent, title, text: shown.append((title, text))),
    )

    rc = app.main([])

    assert rc == 1
    assert len(shown) == 1, "the operator must see a dialog, not just stderr"
    title, text = shown[0]
    assert "audio library" in title
    assert "PortAudio library not found" in text
    # Terminal users still get it on stderr.
    assert "PortAudio library not found" in capsys.readouterr().err
