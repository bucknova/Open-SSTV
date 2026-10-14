# SPDX-License-Identifier: GPL-3.0-or-later
"""RX save paths, audio/image pairing and the RX gate (v0.6.14 audit Lows).

* RX audio save failures opened a modal per decode, and a soundfile
  ``RuntimeError`` escaped the slot.  They now follow ``_autosave_image``:
  one dialog per failure streak, then the status bar.  Writes are atomic.
* Manual save let PIL's ``ValueError`` (unknown extension) escape and wrote
  straight to the final path.
* ``_pending_rx_audio`` was a single untagged slot, so live audio could be
  paired with an offline-decoded image.
* ``_schedule_rx_resume`` lifted the RX gate (resetting the decoder) even
  when TX failed before ``transmission_started`` had raised it.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    from open_sstv.ui.main_window import MainWindow

    monkeypatch.setattr(
        "open_sstv.ui.workers.encode",
        MagicMock(return_value=np.zeros(100, dtype=np.int16)),
    )
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", MagicMock())
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", MagicMock())
    cfg = AppConfig(first_launch_seen=True, check_for_updates=False)
    cfg.logbook_db_path = str(tmp_path / "logbook.db")
    cfg.images_save_dir = str(tmp_path / "images")
    monkeypatch.setattr("open_sstv.ui.main_window.load_config", lambda: cfg)
    monkeypatch.setattr("open_sstv.ui.main_window.save_config", MagicMock())
    w = MainWindow(rig=ManualRig())
    qtbot.addWidget(w)
    yield w


@pytest.fixture
def dialogs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []
    monkeypatch.setattr(
        "open_sstv.ui.main_window.QMessageBox.warning",
        lambda _parent, title, text, *a, **k: shown.append(f"{title}: {text}"),
    )
    return shown


# --- RX audio save ---------------------------------------------------------


def _audio() -> np.ndarray:
    return np.linspace(-0.5, 0.5, 4800)


def test_audio_save_failures_show_one_dialog_per_streak(
    window, dialogs, tmp_path: Path
) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    bad = blocker / "img.png"  # parent is a file → OSError
    for _ in range(3):
        assert window._save_rx_audio(_audio(), Mode.ROBOT_36, 48_000, "wav", alongside=bad) is None
    assert len(dialogs) == 1
    assert "Audio save failed" in window.statusBar().currentMessage()

    good = tmp_path / "ok.png"
    assert window._save_rx_audio(_audio(), Mode.ROBOT_36, 48_000, "wav", alongside=good)
    # A success ends the streak; the next failure gets a dialog again.
    window._save_rx_audio(_audio(), Mode.ROBOT_36, 48_000, "wav", alongside=bad)
    assert len(dialogs) == 2


def test_soundfile_runtime_error_is_caught(
    window, dialogs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    def _boom(*_a, **_k):
        # soundfile.LibsndfileError subclasses RuntimeError.
        raise RuntimeError("Error opening: format not recognised")

    # A stand-in module, so the test runs without the optional dependency.
    fake_sf = types.ModuleType("soundfile")
    fake_sf.write = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    out = window._save_rx_audio(
        _audio(), Mode.ROBOT_36, 48_000, "flac", alongside=tmp_path / "x.png"
    )
    assert out is None
    assert len(dialogs) == 1
    assert not (tmp_path / "x.flac").exists()


def test_audio_save_is_atomic(
    window, dialogs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write leaves no truncated file behind."""
    import open_sstv.fsutil as fsutil

    def _fail_replace(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", _fail_replace)
    target = tmp_path / "x.png"
    assert window._save_rx_audio(_audio(), Mode.ROBOT_36, 48_000, "wav", alongside=target) is None
    assert list(tmp_path.iterdir()) == []


# --- Manual image save -----------------------------------------------------


def _manual_save(window, monkeypatch, path: Path) -> None:
    window._config.auto_save = False
    monkeypatch.setattr(
        "open_sstv.ui.main_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (str(path), ""),
    )
    window._on_rx_image_saved(Image.new("RGB", (32, 32), "red"), Mode.ROBOT_36)


def test_manual_save_unknown_extension_warns(
    window, dialogs, monkeypatch, tmp_path: Path
) -> None:
    _manual_save(window, monkeypatch, tmp_path / "pic.notanimage")
    assert len(dialogs) == 1
    assert list(tmp_path.glob("pic*")) == []


def test_manual_save_without_extension_defaults_to_png(
    window, dialogs, monkeypatch, tmp_path: Path
) -> None:
    _manual_save(window, monkeypatch, tmp_path / "pic")
    assert dialogs == []
    out = tmp_path / "pic.png"
    assert out.exists()
    with Image.open(out) as im:
        assert im.format == "PNG"


# --- live audio never pairs with an offline image ---------------------------


def test_offline_image_does_not_take_live_audio(window, monkeypatch) -> None:
    saved: list[tuple] = []
    monkeypatch.setattr(
        window, "_save_rx_audio",
        lambda audio, mode, sr, fmt, alongside=None: saved.append((mode, sr)) or None,
    )
    window._config.auto_save = False
    window._config.autosave_rx_audio = True
    window._config.rx_capture_prompt = "never"
    img = Image.new("RGB", (8, 8))

    window._on_rx_audio_ready(_audio(), Mode.ROBOT_36, 8, 48_000)
    window._on_offline_image_complete(img, Mode.MARTIN_M1, 44)
    assert saved == [], "offline image must not get the live recording"

    window._on_rx_image_complete(img, Mode.ROBOT_36, 8)
    assert saved == [(Mode.ROBOT_36, 48_000)], "live image keeps its audio"


def test_live_audio_with_other_mode_is_not_paired(window, monkeypatch) -> None:
    saved: list = []
    monkeypatch.setattr(
        window, "_save_rx_audio",
        lambda *a, **k: saved.append(a) or None,
    )
    window._config.auto_save = False
    window._config.autosave_rx_audio = True
    window._config.rx_capture_prompt = "never"
    window._on_rx_audio_ready(_audio(), Mode.ROBOT_36, 8, 48_000)
    window._on_rx_image_complete(Image.new("RGB", (8, 8)), Mode.MARTIN_M1, 44)
    assert saved == []


# --- RX gate ---------------------------------------------------------------


def test_rx_gate_lifted_only_if_raised(window, qtbot) -> None:
    window.show()
    gate: list[bool] = []
    window._request_rx_gate.connect(gate.append)

    window._schedule_rx_resume()  # TX failed before transmission_started
    qtbot.wait(120)
    assert gate == [], "the gate was never raised; the decode must survive"

    window._on_tx_started()
    window._schedule_rx_resume()
    qtbot.wait(120)
    assert gate == [True, False]
    window._unlock_rig_controls()
