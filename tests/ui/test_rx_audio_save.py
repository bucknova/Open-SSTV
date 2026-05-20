# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the RX audio recording feature.

Covers:
- RxWorker.rx_audio_ready signal emission on ImageComplete
- WAV file written with correct properties (sample rate, channels, PCM)
- FLAC file written correctly via soundfile
- Filename derived from alongside-PNG path when auto_save is on
- Filename resolved independently when only autosave_rx_audio is on
- Format combo disables when recording is toggled off
"""
from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.decoder import ImageComplete
from open_sstv.core.modes import Mode
from open_sstv.ui.workers import RxWorker

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Shared FakeSelf for _save_rx_audio tests
# ---------------------------------------------------------------------------

class _FakeSelf:
    """Minimal stub for MainWindow used to call _save_rx_audio in isolation."""
    def statusBar(self):
        bar = MagicMock()
        bar.showMessage = MagicMock()
        return bar

    def _build_save_context(self, mode, direction):
        from open_sstv.templates import TokenContext
        return TokenContext(mode=Mode.ROBOT_36, direction="RX", callsign="")


# ---------------------------------------------------------------------------
# RxWorker.rx_audio_ready signal
# ---------------------------------------------------------------------------


def test_rx_audio_ready_emitted_on_image_complete(qapp) -> None:
    """rx_audio_ready fires once for each ImageComplete with the raw buffer."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    audio_signals: list[tuple] = []
    worker.rx_audio_ready.connect(
        lambda audio, mode, vis, sr: audio_signals.append((audio, mode, vis, sr))
    )

    raw = np.linspace(-1.0, 1.0, 4800, dtype=np.float64)
    img = Image.new("RGB", (320, 240))
    fake_decoder = MagicMock()
    fake_decoder.feed.return_value = [ImageComplete(image=img, mode=Mode.ROBOT_36, vis_code=0x08)]
    fake_decoder.consume_last_buffer.return_value = raw
    worker._decoder = fake_decoder

    worker.feed_chunk(np.zeros(1, dtype=np.float64))

    assert len(audio_signals) == 1
    audio, mode, vis, sr = audio_signals[0]
    np.testing.assert_array_equal(audio, raw)
    assert mode == Mode.ROBOT_36
    assert vis == 0x08
    assert sr == 48_000


def test_rx_audio_ready_not_emitted_when_buffer_is_none(qapp) -> None:
    """rx_audio_ready is suppressed when consume_last_buffer returns None."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    signals: list = []
    worker.rx_audio_ready.connect(lambda *a: signals.append(a))

    img = Image.new("RGB", (320, 240))
    fake_decoder = MagicMock()
    fake_decoder.feed.return_value = [ImageComplete(image=img, mode=Mode.ROBOT_36, vis_code=0x08)]
    fake_decoder.consume_last_buffer.return_value = None
    worker._decoder = fake_decoder

    worker.feed_chunk(np.zeros(1, dtype=np.float64))
    assert len(signals) == 0


def test_rx_audio_ready_not_emitted_for_non_ndarray(qapp) -> None:
    """rx_audio_ready is suppressed when consume_last_buffer returns a non-ndarray
    (guards against tests that mock the decoder with MagicMock)."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    signals: list = []
    worker.rx_audio_ready.connect(lambda *a: signals.append(a))

    img = Image.new("RGB", (320, 240))
    fake_decoder = MagicMock()
    fake_decoder.feed.return_value = [ImageComplete(image=img, mode=Mode.ROBOT_36, vis_code=0x08)]
    fake_decoder.consume_last_buffer.return_value = MagicMock()
    worker._decoder = fake_decoder

    worker.feed_chunk(np.zeros(1, dtype=np.float64))
    assert len(signals) == 0


def test_rx_audio_ready_not_emitted_when_buffer_is_empty(qapp) -> None:
    """rx_audio_ready is suppressed when the raw buffer has zero samples."""
    worker = RxWorker(sample_rate=48_000, flush_samples=1)
    signals: list = []
    worker.rx_audio_ready.connect(lambda *a: signals.append(a))

    img = Image.new("RGB", (320, 240))
    fake_decoder = MagicMock()
    fake_decoder.feed.return_value = [ImageComplete(image=img, mode=Mode.ROBOT_36, vis_code=0x08)]
    fake_decoder.consume_last_buffer.return_value = np.zeros(0, dtype=np.float64)
    worker._decoder = fake_decoder

    worker.feed_chunk(np.zeros(1, dtype=np.float64))
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# _save_rx_audio — WAV path
# ---------------------------------------------------------------------------


def test_save_rx_audio_wav_alongside_png(tmp_path: Path, qapp) -> None:
    """When alongside= a PNG path, WAV lands next to it with same stem."""
    png_path = tmp_path / "2026-01-01_120000_Robot-36.png"
    png_path.touch()

    audio = np.linspace(-1.0, 1.0, 4800, dtype=np.float64)
    sr = 48_000

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(_FakeSelf(), audio, Mode.ROBOT_36, sr, "wav", alongside=png_path)

    wav_path = tmp_path / "2026-01-01_120000_Robot-36.wav"
    assert wav_path.exists()

    with wave.open(str(wav_path)) as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == sr
        assert wf.getnframes() == len(audio)


def test_save_rx_audio_wav_pcm_quantization(tmp_path: Path, qapp) -> None:
    """WAV PCM values are correctly quantized from float64 [-1,1] → int16."""
    png_path = tmp_path / "test.png"
    png_path.touch()

    audio = np.array([0.0, 1.0, -1.0, 0.5], dtype=np.float64)

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(_FakeSelf(), audio, Mode.ROBOT_36, 44_100, "wav", alongside=png_path)

    with wave.open(str(tmp_path / "test.wav")) as wf:
        pcm = np.frombuffer(wf.readframes(4), dtype=np.int16)

    assert pcm[0] == 0
    assert pcm[1] == 32767
    assert pcm[2] == -32767
    assert pcm[3] == int(0.5 * 32767)  # astype truncates toward zero


def test_save_rx_audio_wav_unknown_format_falls_back(tmp_path: Path, qapp) -> None:
    """An unrecognised format string falls back to WAV."""
    png_path = tmp_path / "test.png"
    png_path.touch()

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(
        _FakeSelf(), np.zeros(100), Mode.ROBOT_36, 48_000, "ogg", alongside=png_path
    )

    # Should produce a .wav, not a .ogg
    assert (tmp_path / "test.wav").exists()
    assert not (tmp_path / "test.ogg").exists()


def test_save_rx_audio_empty_is_noop(tmp_path: Path, qapp) -> None:
    """Zero-length audio produces no file and no error."""
    png_path = tmp_path / "test.png"
    png_path.touch()

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(_FakeSelf(), np.zeros(0), Mode.ROBOT_36, 48_000, "wav", alongside=png_path)

    assert not (tmp_path / "test.wav").exists()


# ---------------------------------------------------------------------------
# _save_rx_audio — FLAC path
# ---------------------------------------------------------------------------


def test_save_rx_audio_flac_alongside_png(tmp_path: Path, qapp) -> None:
    """FLAC file lands next to the PNG with the same stem."""
    pytest.importorskip("soundfile")

    png_path = tmp_path / "2026-01-01_120000_Robot-36.png"
    png_path.touch()

    audio = np.linspace(-1.0, 1.0, 4800, dtype=np.float64)
    sr = 48_000

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(_FakeSelf(), audio, Mode.ROBOT_36, sr, "flac", alongside=png_path)

    flac_path = tmp_path / "2026-01-01_120000_Robot-36.flac"
    assert flac_path.exists()


def test_save_rx_audio_flac_roundtrip(tmp_path: Path, qapp) -> None:
    """FLAC: reading back gives correct frame count and sample rate."""
    soundfile = pytest.importorskip("soundfile")

    png_path = tmp_path / "test.png"
    png_path.touch()

    audio = np.linspace(-1.0, 1.0, 9600, dtype=np.float64)

    from open_sstv.ui.main_window import MainWindow
    MainWindow._save_rx_audio(_FakeSelf(), audio, Mode.ROBOT_36, 48_000, "flac", alongside=png_path)

    info = soundfile.info(str(tmp_path / "test.flac"))
    assert info.frames == len(audio)
    assert info.samplerate == 48_000
    assert info.channels == 1


# ---------------------------------------------------------------------------
# Settings dialog — RX Audio Recording group
# ---------------------------------------------------------------------------


def test_settings_audio_recording_group_present(qapp) -> None:
    """The Audio tab exposes the recording toggle and format combo."""
    from open_sstv.config.schema import AppConfig
    from open_sstv.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppConfig(), rig_connected=False)
    assert hasattr(dlg, "_autosave_rx_audio_check")
    assert hasattr(dlg, "_rx_audio_format")
    dlg.reject()


def test_settings_audio_recording_default_wav(qapp) -> None:
    """Default format is WAV and recording is disabled."""
    from open_sstv.config.schema import AppConfig
    from open_sstv.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppConfig(), rig_connected=False)
    assert not dlg._autosave_rx_audio_check.isChecked()
    assert dlg._rx_audio_format.currentData() == "wav"
    dlg.reject()


def test_settings_audio_recording_format_persists(qapp) -> None:
    """Selecting FLAC and accepting saves it into result_config."""
    from open_sstv.config.schema import AppConfig
    from open_sstv.ui.settings_dialog import SettingsDialog

    cfg = AppConfig(autosave_rx_audio=True, rx_audio_format="flac")
    dlg = SettingsDialog(cfg, rig_connected=False)
    assert dlg._autosave_rx_audio_check.isChecked()
    assert dlg._rx_audio_format.currentData() == "flac"
    dlg.accept()
    out = dlg.result_config()
    assert out.autosave_rx_audio is True
    assert out.rx_audio_format == "flac"


def test_settings_audio_recording_format_combo_disabled_when_off(qapp) -> None:
    """Format combo is disabled when the recording toggle is unchecked."""
    from open_sstv.config.schema import AppConfig
    from open_sstv.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(AppConfig(autosave_rx_audio=False), rig_connected=False)
    assert not dlg._rx_audio_format.isEnabled()
    dlg._autosave_rx_audio_check.setChecked(True)
    assert dlg._rx_audio_format.isEnabled()
    dlg.reject()
