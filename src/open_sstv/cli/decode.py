# SPDX-License-Identifier: GPL-3.0-or-later
"""``sstv-app-decode`` — decode an SSTV WAV file into an image.

Usage::

    sstv-app-decode in.wav -o out.png
    sstv-app-decode in.wav -o out.png --quiet

A thin argparse wrapper around ``open_sstv.core.decoder.decode_wav``.
The companion to ``sstv-app-encode``: together they let us run an
encode→decode round-trip on the command line without bringing up Qt
or audio hardware. This is the headless smoke test for the entire
RX pipeline.

Note: Robot 36 images decoded here use the batch median+PIL color
pipeline rather than the slowrx-style integer matrix used by the GUI's
incremental decoder. Output colors may differ slightly between the two
paths.

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

from open_sstv.core.decoder import decode_wav


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sstv-app-decode",
        description=(
            "Decode an SSTV WAV file into an image. The mode is detected "
            "automatically from the VIS header. Returns a non-zero exit "
            "code if no SSTV header is found in the audio. "
            "Note: Robot 36 images are decoded with the batch (median+PIL) "
            "pipeline; the GUI live decoder uses the slowrx-style path and "
            "may produce slightly different colors for Robot 36."
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

    Thin wrapper around ``open_sstv.audio.file_io.load_audio_file`` kept
    for backwards compatibility with any external callers that imported
    this private helper.  New code should use ``load_audio_file``
    directly — it also handles FLAC.
    """
    from open_sstv.audio.file_io import load_audio_file  # noqa: PLC0415
    return load_audio_file(path)


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
