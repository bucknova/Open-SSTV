# SPDX-License-Identifier: GPL-3.0-or-later
"""``sstv-app-decode`` — decode an SSTV WAV file into an image.

Usage::

    sstv-app-decode in.wav -o out.png
    sstv-app-decode in.wav -o out.png --quiet

A thin argparse wrapper around ``sstv_app.core.decoder.decode_wav``.
The companion to ``sstv-app-encode``: together they let us run an
encode→decode round-trip on the command line without bringing up Qt
or audio hardware. This is the headless smoke test for the entire
RX pipeline.

We read WAVs with stdlib ``wave`` rather than ``scipy.io.wavfile`` so
the CLI works on a stripped-down install (someone running on a Pi
with only the bare minimum). Multi-channel WAVs are mixed down to
mono via ``dsp_utils.to_mono_float32``.

Exit codes:
    0  success — image decoded and written
    1  unrecoverable error (file not found, no VIS detected, decode failed)
    2  argparse-rejected arguments (handled by argparse itself)
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

from sstv_app.core.decoder import decode_wav
from sstv_app.core.dsp_utils import to_mono_float32


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sstv-app-decode",
        description=(
            "Decode an SSTV WAV file into an image. The mode is detected "
            "automatically from the VIS header. Returns a non-zero exit "
            "code if no SSTV header is found in the audio."
        ),
    )
    parser.add_argument(
        "wav",
        type=Path,
        help="Input WAV file. 16-bit mono PCM is the canonical format; "
             "stereo and other widths are accepted and mixed down.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output image file path. Format is inferred from the extension.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the success message on stdout.",
    )
    return parser


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Load a WAV file as a mono float64 buffer plus its sample rate.

    Uses stdlib ``wave`` for reading and decodes the raw bytes via NumPy
    so we can stay scipy-free at the CLI layer.
    """
    with wave.open(str(path), "rb") as wav:
        n_channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        fs = wav.getframerate()
        n_frames = wav.getnframes()
        raw = wav.readframes(n_frames)

    # Decode raw bytes by sample width. WAV is always little-endian PCM.
    if sample_width == 1:
        # 8-bit WAV is unsigned per the spec; convert to signed centered.
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
    elif sample_width == 2:
        samples = np.frombuffer(raw, dtype="<i2")
    elif sample_width == 4:
        samples = np.frombuffer(raw, dtype="<i4")
    else:
        msg = f"Unsupported WAV sample width: {sample_width} bytes"
        raise ValueError(msg)

    if n_channels > 1:
        samples = samples.reshape(-1, n_channels)

    mono = to_mono_float32(samples).astype(np.float64)
    return mono, fs


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.wav.exists():
        print(f"sstv-app-decode: input not found: {args.wav}", file=sys.stderr)
        return 1

    try:
        samples, fs = _read_wav(args.wav)
    except (OSError, ValueError, wave.Error) as exc:
        print(f"sstv-app-decode: failed to read WAV: {exc}", file=sys.stderr)
        return 1

    result = decode_wav(samples, fs)
    if result is None:
        print(
            "sstv-app-decode: no SSTV header found, or unsupported mode",
            file=sys.stderr,
        )
        return 1

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(args.output)
    except OSError as exc:
        print(f"sstv-app-decode: failed to write image: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"sstv-app-decode: failed to write image: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f"sstv-app-decode: wrote {args.output} "
            f"(mode={result.mode.value}, vis=0x{result.vis_code:02x}, "
            f"size={result.image.size[0]}x{result.image.size[1]})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
