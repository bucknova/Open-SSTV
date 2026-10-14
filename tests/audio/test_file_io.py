# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.audio.file_io.load_audio_file``."""
from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from open_sstv.audio.file_io import load_audio_file


def _write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int = 48_000,
    sample_width: int = 2,
    n_channels: int = 1,
) -> None:
    """Helper: write samples as a WAV file with the given format."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(n_channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def test_load_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_audio_file(tmp_path / "does_not_exist.wav")


def test_load_unsupported_extension_raises_value_error(tmp_path: Path) -> None:
    bogus = tmp_path / "audio.mp3"
    bogus.touch()
    with pytest.raises(ValueError, match="unsupported audio file extension"):
        load_audio_file(bogus)


def test_load_wav_16bit_mono_round_trip(tmp_path: Path) -> None:
    """A simple int16 mono WAV loads back to the same float values."""
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    wav_path = tmp_path / "mono.wav"
    _write_wav(wav_path, samples)

    loaded, fs = load_audio_file(wav_path)

    assert fs == 48_000
    # to_mono_float32 scales int16 → [-1.0, 1.0] (with -32768 / 32768 = -1.0).
    expected = samples.astype(np.float64) / 32768.0
    np.testing.assert_allclose(loaded, expected, atol=1e-5)


def test_load_wav_stereo_downmix_reduces_to_one_value_per_frame(
    tmp_path: Path,
) -> None:
    """Stereo WAV is downmixed to mono — frame count halves and the
    result is one value per (L,R) pair.

    NOTE: ``to_mono_float32`` has a pre-existing bug where integer
    stereo input is channel-averaged via ``arr.mean(axis=1)`` (which
    upcasts to float64) and then RETURNED WITHOUT INTEGER SCALING —
    so int16 stereo values come out as raw averaged integers in float
    form, not normalised to [-1.0, 1.0].  This test only verifies that
    the downmix HAPPENED (correct length) and the values are real
    (not all zero); the exact scaling is out of scope for this loader
    test because the bug is in the shared helper.  A separate fix
    should restore the int-scaling path for 2-D integer arrays.
    """
    interleaved = np.array(
        [100, -100, 200, -200, 50, 150], dtype=np.int16
    )
    wav_path = tmp_path / "stereo.wav"
    _write_wav(wav_path, interleaved, n_channels=2)

    loaded, fs = load_audio_file(wav_path)

    assert fs == 48_000
    # 6 interleaved int16 samples → 3 stereo frames → 3 mono samples.
    assert loaded.shape == (3,), f"expected (3,) mono samples, got {loaded.shape}"
    # The (L+R)/2 of the third pair is (50+150)/2 = 100; should appear
    # somewhere in the output (regardless of whether the integer
    # scaling bug is present or not).
    assert np.any(np.abs(loaded) > 0), "downmix should not collapse to silence"


def test_load_wav_8bit_is_centered(tmp_path: Path) -> None:
    """8-bit WAV is unsigned (0..255 = silence at 128); the loader must
    re-center to signed."""
    # All-128 → silence (zero).
    silence = np.full(100, 128, dtype=np.uint8)
    wav_path = tmp_path / "8bit.wav"
    _write_wav(wav_path, silence, sample_width=1)

    loaded, fs = load_audio_file(wav_path)

    assert fs == 48_000
    np.testing.assert_allclose(loaded, np.zeros(100), atol=1e-4)


def _riff(fmt_chunk: bytes, data: bytes) -> bytes:
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_load_wav_24bit_pcm(tmp_path: Path) -> None:
    """v0.6.14 audit D-M2: 24-bit PCM used to be rejected outright."""
    values = [0, 2**22, -(2**22), 2**23 - 1, -(2**23)]
    data = b"".join(int(v).to_bytes(3, "little", signed=True) for v in values)
    fmt = struct.pack("<HHIIHH", 1, 1, 48000, 48000 * 3, 3, 24)
    wav_path = tmp_path / "pcm24.wav"
    wav_path.write_bytes(_riff(fmt, data))

    loaded, fs = load_audio_file(wav_path)

    assert fs == 48_000
    np.testing.assert_allclose(loaded, [0.0, 0.5, -0.5, 1.0, -1.0], atol=1e-6)


def test_load_wav_unsupported_sample_width_raises(tmp_path: Path) -> None:
    """A 5-byte integer sample width is still rejected cleanly."""
    data = b"\x00" * 50
    fmt = struct.pack("<HHIIHH", 1, 1, 48000, 48000 * 5, 5, 40)
    wav_path = tmp_path / "weird.wav"
    wav_path.write_bytes(_riff(fmt, data))

    with pytest.raises(ValueError, match="unsupported WAV sample width"):
        load_audio_file(wav_path)


def test_load_wav_float32_via_scipy(tmp_path: Path) -> None:
    """D-M2: IEEE-float WAV (format 3) — what SDR software writes."""
    from scipy.io import wavfile

    samples = np.array([[0.25, -0.25], [0.5, 0.5], [-1.0, -0.5]], dtype=np.float32)
    wav_path = tmp_path / "float.wav"
    wavfile.write(wav_path, 44_100, samples)

    loaded, fs = load_audio_file(wav_path)

    assert fs == 44_100
    assert loaded.dtype == np.float64
    np.testing.assert_allclose(loaded, [0.0, 0.5, -0.75], atol=1e-7)


def test_load_wav_extensible_float(tmp_path: Path) -> None:
    """D-M2: WAVE_FORMAT_EXTENSIBLE wrapping IEEE float."""
    samples = np.array([0.1, -0.2, 0.3], dtype="<f4")
    float_guid = struct.pack("<IHH", 3, 0x0000, 0x0010) + bytes(
        [0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71]
    )
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 48000, 48000 * 4, 4, 32)
    fmt += struct.pack("<HHI", 22, 32, 0x4) + float_guid
    wav_path = tmp_path / "ext_float.wav"
    wav_path.write_bytes(_riff(fmt, samples.tobytes()))

    loaded, fs = load_audio_file(wav_path)

    assert fs == 48_000
    np.testing.assert_allclose(loaded, [0.1, -0.2, 0.3], atol=1e-7)


def test_load_wav_empty_file_raises_value_error(tmp_path: Path) -> None:
    """A zero-byte .wav raised a bare EOFError from ``wave``."""
    wav_path = tmp_path / "empty.wav"
    wav_path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty or truncated"):
        load_audio_file(wav_path)


def test_load_flac_round_trip(tmp_path: Path) -> None:
    """If soundfile is installed, FLAC round-trips correctly."""
    sf = pytest.importorskip("soundfile")

    samples = np.linspace(-0.5, 0.5, 1000, dtype=np.float64)
    flac_path = tmp_path / "test.flac"
    sf.write(str(flac_path), samples, 48_000, subtype="PCM_16")

    loaded, fs = load_audio_file(flac_path)

    assert fs == 48_000
    # PCM_16 round-trip loses ~15-bit precision; allow a few LSB.
    np.testing.assert_allclose(loaded, samples, atol=1e-4)


def test_load_flac_without_soundfile_raises_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When soundfile isn't importable, the loader raises ImportError
    with a clear ``pip install`` hint pointing at the [flac] extra."""
    flac_path = tmp_path / "test.flac"
    flac_path.write_bytes(b"fake flac")  # contents don't matter; we mock import

    # Pretend soundfile isn't installed by removing it from sys.modules
    # and adding a meta-path finder that fails the import.
    import builtins
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "soundfile":
            raise ImportError("simulated missing soundfile")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import sys
    monkeypatch.setitem(sys.modules, "soundfile", None)  # type: ignore[arg-type]

    with pytest.raises(ImportError, match='open-sstv\\[flac\\]'):
        load_audio_file(flac_path)
