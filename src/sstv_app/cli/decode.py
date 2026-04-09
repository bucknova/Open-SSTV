# SPDX-License-Identifier: GPL-3.0-or-later
"""``sstv-app-decode`` — decode an SSTV WAV file into an image.

Usage::

    sstv-app-decode in.wav -o out.png
    sstv-app-decode in.wav --mode robot_36 -o out.png   # force mode (skip VIS)

A trivial argparse wrapper around ``sstv_app.core.decoder.Decoder``. Used by
``tests/core/test_decoder_*.py`` for end-to-end round-trip checks against
PySSTV-generated fixtures.

Phase 0 stub. Implemented in Phase 2 step 13 of the v1 plan.
"""
from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Phase 0 stub — real implementation lands in Phase 2 step 13."""
    del argv
    print("sstv-app-decode: not implemented yet (Phase 2 step 13).")
    return 1
