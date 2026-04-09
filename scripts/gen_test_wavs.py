# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate SSTV test fixtures (WAV files) from reference images.

Used by ``tests/conftest.py`` and as a standalone tool when you want to play
a known fixture through your own radio for a sanity check.

Usage::

    python scripts/gen_test_wavs.py                    # default fixtures
    python scripts/gen_test_wavs.py --mode martin_m1   # one mode
    python scripts/gen_test_wavs.py --noise-snr 10     # add noise

Phase 0 stub. Real implementation lands alongside Phase 2 step 11 of the
v1 plan, when we wire ``core.encoder`` up to PySSTV.
"""
from __future__ import annotations
