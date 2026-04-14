# SPDX-License-Identifier: GPL-3.0-or-later
"""``open-sstv-encode`` — render an image to an SSTV WAV file.

Usage::

    open-sstv-encode in.png --mode martin_m1 -o out.wav
    open-sstv-encode in.jpg --mode robot_36 --sample-rate 44100 -o out.wav

A thin argparse wrapper around ``open_sstv.core.encoder.encode``. We write
the WAV with stdlib ``wave`` rather than ``scipy.io.wavfile`` so that this
CLI also works on a stripped-down install (someone running on a Pi with
``pip install sstv-app --no-deps`` and only the bare minimum). Output is
mono int16 PCM, which is what every SSTV decoder on the planet expects.

This is the first runnable artifact in the project: encode an image to
WAV without spinning up Qt or touching audio hardware. Useful for headless
smoke tests and for sanity-checking the TX path against a third-party
decoder.

Exit codes:
    0  success
    1  unrecoverable error (file not found, unsupported mode, ...)
    2  argparse-rejected arguments (handled by argparse itself)
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

from open_sstv.core.encoder import DEFAULT_SAMPLE_RATE, encode
from open_sstv.core.modes import Mode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-sstv-encode",
        description=(
            "Render an image as an SSTV WAV file. The output is 16-bit mono "
            "PCM at the chosen sample rate; play it through your radio's "
            "audio input (or feed it to another SSTV decoder for testing)."
        ),
    )
    parser.add_argument(
        "image",
        type=Path,
        help="Input image. Any Pillow-readable format (PNG, JPEG, BMP, ...).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output WAV file path.",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        required=True,
        help="SSTV mode to encode in.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help=f"Output sample rate in Hz (default: {DEFAULT_SAMPLE_RATE}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.image.exists():
        print(f"sstv-app-encode: input not found: {args.image}", file=sys.stderr)
        return 1

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        samples = encode(args.image, Mode(args.mode), sample_rate=args.sample_rate)
    except OSError as exc:
        print(f"open-sstv-encode: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"open-sstv-encode: {exc}", file=sys.stderr)
        return 1

    # ``wave`` expects bytes; int16 little-endian is the SSTV-standard format
    # and matches what PySSTV's gen_samples quantizes to.
    try:
        with wave.open(str(args.output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(args.sample_rate)
            wav.writeframes(samples.tobytes())
    except OSError as exc:
        print(f"open-sstv-encode: {exc}", file=sys.stderr)
        return 1

    duration_s = samples.size / args.sample_rate
    print(
        f"open-sstv-encode: wrote {args.output} "
        f"({samples.size} samples, {duration_s:.2f}s, {args.mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
