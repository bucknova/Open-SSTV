# SPDX-License-Identifier: GPL-3.0-or-later
"""``sstv-app-encode`` — render an image to an SSTV WAV file.

Usage::

    sstv-app-encode in.png --mode martin_m1 -o out.wav
    sstv-app-encode in.jpg --mode robot_36 --sample-rate 44100 -o out.wav

A trivial argparse wrapper around ``sstv_app.core.encoder.encode()``.

Phase 0 stub. Implemented in Phase 1 step 7 of the v1 plan (this is the
first runnable artifact in the project).
"""
from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Phase 0 stub — real implementation lands in Phase 1 step 7."""
    del argv
    print("sstv-app-encode: not implemented yet (Phase 1 step 7).")
    return 1
