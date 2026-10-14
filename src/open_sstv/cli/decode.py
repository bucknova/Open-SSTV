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

Robot 36 images decoded here run through the same slowrx-style sampler
and colour matrix (``core.robot36_dsp``) as the GUI's live decoder.

Audio loading is ``audio.file_io.load_audio_file``: WAV (8/16/24/32-bit
PCM via stdlib ``wave``, float / EXTENSIBLE via ``scipy.io.wavfile``) and
FLAC (optional ``soundfile``). Multi-channel audio is mixed to mono.

Exit codes:
    0  success — image decoded and written
    1  unrecoverable error (file not found, no VIS detected, decode failed)
    2  argparse-rejected arguments (handled by argparse itself)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import wave
from pathlib import Path

import numpy as np
import platformdirs

from open_sstv.core.decoder import decode_wav


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
        help="Input WAV (or FLAC) file. 16-bit mono PCM is the canonical "
             "format; 8/24/32-bit PCM, float WAVs and stereo are accepted "
             "and mixed down.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Output image file path. Format is inferred from the extension. "
            "If omitted, writes to "
            "<platform pictures dir>/Open-SSTV/sstv_<mode>_<UTC timestamp>.png "
            "(mirrors the GUI auto-save behaviour and the .gitignore "
            "convention for the project's own sstv_*.png pattern; "
            "M-9 audit follow-up)."
        ),
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
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        # v0.6.14 audit (CLI low): EOFError (empty .wav from stdlib wave)
        # used to escape as a traceback.
        print(f"sstv-app-decode: failed to read WAV: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        # FLAC input without the optional ``soundfile`` package.
        print(f"sstv-app-decode: {exc}", file=sys.stderr)
        return 1

    result = decode_wav(samples, fs)
    if result is None:
        print(
            "sstv-app-decode: no SSTV header found, or unsupported mode",
            file=sys.stderr,
        )
        return 1

    # M-9 (audit 4.7/v0.2.9): if the user didn't pass ``-o``, build a
    # platform-appropriate default path now that we know the mode.  This
    # mirrors the GUI auto-save behaviour and stops the CLI's old
    # required-``-o`` requirement from being a footgun on the obvious
    # "open-sstv-decode foo.wav" invocation.  No collision protection
    # because the timestamp is per-second; on the rare second-bucket
    # collision the user can pass ``-o`` explicitly.
    if args.output is None:
        out_dir = Path(platformdirs.user_pictures_dir()) / "Open-SSTV"
        timestamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%d_%H%M%S")
        args.output = out_dir / f"sstv_{result.mode.value}_{timestamp}.png"

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
