# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the offline Encode/Decode Audio workers (in-panel buttons)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.modes import Mode
from open_sstv.ui.offline_workers import OfflineDecodeWorker, OfflineEncodeWorker

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# OfflineDecodeWorker
# ---------------------------------------------------------------------------


def _make_test_wav(tmp_path: Path) -> Path:
    """Encode a small Robot 36 WAV via PySSTV so the decode test has real
    audio to chew on.  Imported lazily because the encoder is slow."""
    from open_sstv.core.encoder import encode

    img = Image.new("RGB", (320, 240), color=(128, 64, 200))
    samples = encode(img, Mode.ROBOT_36, sample_rate=48_000)
    out = tmp_path / "test.wav"
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(samples.tobytes())
    return out


def _collect(worker, *signals: str) -> dict[str, list]:
    """Connect each signal name to a list and return the dict."""
    log: dict[str, list] = {s: [] for s in signals}
    for s in signals:
        getattr(worker, s).connect(lambda *args, _s=s: log[_s].append(args))
    return log


def test_decode_emits_image_complete_on_valid_wav(qapp, tmp_path: Path) -> None:
    """A real encoded WAV decodes into an image_complete emission with
    the correct mode and VIS code."""
    wav_path = _make_test_wav(tmp_path)
    worker = OfflineDecodeWorker()
    log = _collect(worker, "image_complete", "error", "finished")

    worker.decode(str(wav_path))

    assert len(log["error"]) == 0
    assert len(log["image_complete"]) == 1
    image, mode, vis_code = log["image_complete"][0]
    assert mode == Mode.ROBOT_36
    assert vis_code == 0x08  # Robot 36 VIS
    assert image.size == (320, 240)
    assert len(log["finished"]) == 1


def test_decode_missing_file_emits_error(qapp, tmp_path: Path) -> None:
    """File-not-found surfaces via the error signal, not as an exception."""
    worker = OfflineDecodeWorker()
    log = _collect(worker, "image_complete", "error", "finished")

    worker.decode(str(tmp_path / "nonexistent.wav"))

    assert len(log["image_complete"]) == 0
    assert len(log["error"]) == 1
    assert "not found" in log["error"][0][0]
    assert len(log["finished"]) == 1


def test_decode_unsupported_extension_emits_error(
    qapp, tmp_path: Path
) -> None:
    """A .mp3 (or anything not .wav/.flac) surfaces via error."""
    bogus = tmp_path / "audio.mp3"
    bogus.write_bytes(b"fake mp3 data")
    worker = OfflineDecodeWorker()
    log = _collect(worker, "image_complete", "error", "finished")

    worker.decode(str(bogus))

    assert len(log["image_complete"]) == 0
    assert len(log["error"]) == 1
    assert "unsupported" in log["error"][0][0].lower()


def test_decode_no_vis_audio_emits_error(qapp, tmp_path: Path) -> None:
    """A silent WAV (no SSTV signal) emits 'no header found' via error."""
    silence = np.zeros(48_000, dtype=np.int16)  # 1 s of silence
    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(silence.tobytes())

    worker = OfflineDecodeWorker()
    log = _collect(worker, "image_complete", "error", "finished")

    worker.decode(str(wav_path))

    assert len(log["image_complete"]) == 0
    assert len(log["error"]) == 1
    assert "No SSTV header" in log["error"][0][0]


def test_decode_empty_wav_emits_error(qapp, tmp_path: Path) -> None:
    """An empty WAV (zero frames) emits 'empty' via error."""
    wav_path = tmp_path / "empty.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        # no writeframes call → zero frames

    worker = OfflineDecodeWorker()
    log = _collect(worker, "image_complete", "error", "finished")

    worker.decode(str(wav_path))

    assert len(log["image_complete"]) == 0
    assert len(log["error"]) == 1
    assert "empty" in log["error"][0][0].lower()


# ---------------------------------------------------------------------------
# OfflineEncodeWorker
# ---------------------------------------------------------------------------


def test_encode_from_image_writes_correct_wav_format(
    qapp, tmp_path: Path
) -> None:
    """Encoded WAV has the right channels / width / sample rate."""
    img = Image.new("RGB", (320, 240), color=(100, 150, 200))
    out_path = tmp_path / "out.wav"

    worker = OfflineEncodeWorker()
    log = _collect(worker, "encode_complete", "error", "finished")

    worker.encode_from_image(img, Mode.ROBOT_36, 48_000, str(out_path))

    assert len(log["error"]) == 0
    assert len(log["encode_complete"]) == 1
    path, duration_s, mode = log["encode_complete"][0]
    assert path == str(out_path)
    assert mode == Mode.ROBOT_36
    # Robot 36 is ~36 s.
    assert 30 < duration_s < 40

    # Inspect the file: 16-bit PCM mono at 48 kHz.
    with wave.open(str(out_path)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 48_000
        assert w.getnframes() > 0


def test_encode_from_image_creates_parent_dir(qapp, tmp_path: Path) -> None:
    """Output path's parent is created if missing."""
    img = Image.new("RGB", (320, 240))
    out_path = tmp_path / "subdir" / "deeper" / "out.wav"

    worker = OfflineEncodeWorker()
    log = _collect(worker, "encode_complete", "error", "finished")

    worker.encode_from_image(img, Mode.ROBOT_36, 48_000, str(out_path))

    assert out_path.exists()
    assert len(log["error"]) == 0


def test_encode_from_image_unwritable_path_emits_error(
    qapp, tmp_path: Path
) -> None:
    """A path inside a regular file (not a dir) surfaces an OSError via
    the error signal — exercises the file-write error path."""
    img = Image.new("RGB", (320, 240))
    # Create a file, then try to write *inside* it (i.e. treat the file
    # as a parent directory).  ``mkdir(parents=True, exist_ok=True)``
    # raises NotADirectoryError, which falls through to OSError.
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a directory")
    out_path = blocker / "out.wav"

    worker = OfflineEncodeWorker()
    log = _collect(worker, "encode_complete", "error", "finished")

    worker.encode_from_image(img, Mode.ROBOT_36, 48_000, str(out_path))

    assert len(log["encode_complete"]) == 0
    assert len(log["error"]) == 1
    assert "Could not write WAV" in log["error"][0][0]
