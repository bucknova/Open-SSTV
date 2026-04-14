# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the ``sstv-app-encode`` CLI.

These run the ``main`` function directly with synthetic argv lists rather
than spawning a subprocess, so we get readable failures and no PATH games.
The CLI calls into the same encoder we already test against, so we don't
re-validate audio content here — only the WAV header and the surrounding
plumbing (arg parsing, error handling, file I/O).
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.cli.encode import main
from open_sstv.core.modes import MODE_TABLE, Mode


@pytest.fixture
def fixture_image(tmp_path: Path) -> Path:
    """Tiny gradient image written to disk for the CLI to read."""
    img = Image.new("RGB", (50, 50))
    pixels = img.load()
    assert pixels is not None
    for y in range(50):
        for x in range(50):
            pixels[x, y] = (x * 5, y * 5, (x + y) * 5)
    path = tmp_path / "in.png"
    img.save(path)
    return path


@pytest.mark.parametrize("mode", list(Mode))
def test_cli_round_trip_writes_valid_wav(
    fixture_image: Path, tmp_path: Path, mode: Mode
) -> None:
    out = tmp_path / f"{mode.value}.wav"
    rc = main([str(fixture_image), "--mode", mode.value, "-o", str(out)])

    assert rc == 0
    assert out.exists()

    with wave.open(str(out), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2  # 16-bit
        assert wav.getframerate() == 48_000
        # Body duration plus VIS leader; allow 5% slop on top of body.
        body_s = MODE_TABLE[mode].total_duration_s
        actual_s = wav.getnframes() / wav.getframerate()
        assert body_s <= actual_s <= body_s * 1.05


def test_cli_honors_custom_sample_rate(
    fixture_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "44k.wav"
    rc = main(
        [
            str(fixture_image),
            "--mode",
            "robot_36",
            "--sample-rate",
            "44100",
            "-o",
            str(out),
        ]
    )

    assert rc == 0
    with wave.open(str(out), "rb") as wav:
        assert wav.getframerate() == 44_100


def test_cli_creates_missing_output_directory(
    fixture_image: Path, tmp_path: Path
) -> None:
    out = tmp_path / "nested" / "dir" / "out.wav"
    rc = main([str(fixture_image), "--mode", "robot_36", "-o", str(out)])

    assert rc == 0
    assert out.exists()


def test_cli_rejects_missing_input(tmp_path: Path) -> None:
    out = tmp_path / "out.wav"
    rc = main(
        [str(tmp_path / "does_not_exist.png"), "--mode", "robot_36", "-o", str(out)]
    )
    assert rc == 1
    assert not out.exists()


def test_cli_rejects_unknown_mode(fixture_image: Path, tmp_path: Path) -> None:
    """argparse exits with code 2 for invalid choices — we let it raise
    SystemExit rather than catching it ourselves."""
    out = tmp_path / "out.wav"
    with pytest.raises(SystemExit) as excinfo:
        main([str(fixture_image), "--mode", "bogus", "-o", str(out)])
    assert excinfo.value.code == 2


def test_cli_requires_mode(fixture_image: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.wav"
    with pytest.raises(SystemExit) as excinfo:
        main([str(fixture_image), "-o", str(out)])
    assert excinfo.value.code == 2


def test_cli_requires_output(fixture_image: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([str(fixture_image), "--mode", "robot_36"])
    assert excinfo.value.code == 2
