# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the ``sstv-app-decode`` CLI.

Same approach as ``test_encode.py``: drive ``main()`` directly with
synthetic argv lists, and round-trip through the encode CLI's WAV
output rather than running the encoder in-memory. The 'CLI to CLI'
round-trip is the cheapest way to validate the WAV reader and the
decoder dispatch in one shot.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sstv_app.cli.decode import main as decode_main
from sstv_app.cli.encode import main as encode_main


@pytest.fixture
def fixture_image(tmp_path: Path) -> Path:
    """A small RGB gradient written to disk for the encoder CLI to read."""
    img = Image.new("RGB", (100, 80))
    pixels = img.load()
    assert pixels is not None
    for y in range(80):
        for x in range(100):
            pixels[x, y] = (x * 2, y * 3, (x + y) * 2)
    path = tmp_path / "in.png"
    img.save(path)
    return path


@pytest.fixture
def robot36_wav(fixture_image: Path, tmp_path: Path) -> Path:
    """Encode the gradient via the encode CLI and return the WAV path.

    Encode→decode through both CLIs at once is the most realistic
    smoke test we can run without spinning up audio hardware.
    """
    wav = tmp_path / "in.wav"
    rc = encode_main(
        [str(fixture_image), "--mode", "robot_36", "-o", str(wav)]
    )
    assert rc == 0
    return wav


def test_cli_decodes_robot36_wav_to_image(
    robot36_wav: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.png"
    rc = decode_main([str(robot36_wav), "-o", str(out)])
    assert rc == 0
    assert out.exists()
    img = Image.open(out)
    assert img.size == (320, 240)
    assert img.mode == "RGB"


def test_cli_quiet_suppresses_stdout(
    robot36_wav: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.png"
    rc = decode_main([str(robot36_wav), "-o", str(out), "--quiet"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_cli_prints_mode_and_size_by_default(
    robot36_wav: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.png"
    rc = decode_main([str(robot36_wav), "-o", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "robot_36" in captured.out
    assert "320x240" in captured.out


def test_cli_creates_missing_output_directory(
    robot36_wav: Path, tmp_path: Path
) -> None:
    out = tmp_path / "nested" / "dir" / "out.png"
    rc = decode_main([str(robot36_wav), "-o", str(out)])
    assert rc == 0
    assert out.exists()


def test_cli_rejects_missing_input(tmp_path: Path) -> None:
    out = tmp_path / "out.png"
    rc = decode_main([str(tmp_path / "missing.wav"), "-o", str(out)])
    assert rc == 1
    assert not out.exists()


def test_cli_rejects_silence_wav(tmp_path: Path) -> None:
    """A 1-second silent WAV has no VIS — the decoder returns None and
    the CLI must surface that as exit 1."""
    silent = tmp_path / "silent.wav"
    samples = np.zeros(48_000, dtype=np.int16)
    with wave.open(str(silent), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(samples.tobytes())

    out = tmp_path / "out.png"
    rc = decode_main([str(silent), "-o", str(out)])
    assert rc == 1
    assert not out.exists()


def test_cli_requires_output(robot36_wav: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        decode_main([str(robot36_wav)])
    assert excinfo.value.code == 2


def test_cli_decodes_stereo_wav(
    fixture_image: Path, tmp_path: Path
) -> None:
    """The CLI must accept multi-channel WAVs and mix them down."""
    # Encode to mono first, then duplicate the samples into a fake stereo
    # WAV. This exercises the channel-mix path in ``_read_wav`` while
    # still containing a real SSTV header.
    mono_wav = tmp_path / "mono.wav"
    encode_main(
        [str(fixture_image), "--mode", "robot_36", "-o", str(mono_wav)]
    )
    with wave.open(str(mono_wav), "rb") as wav:
        fs = wav.getframerate()
        mono = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")

    stereo = np.empty(mono.size * 2, dtype="<i2")
    stereo[0::2] = mono
    stereo[1::2] = mono
    stereo_wav = tmp_path / "stereo.wav"
    with wave.open(str(stereo_wav), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(fs)
        wav.writeframes(stereo.tobytes())

    out = tmp_path / "out.png"
    rc = decode_main([str(stereo_wav), "-o", str(out)])
    assert rc == 0
    assert out.exists()
