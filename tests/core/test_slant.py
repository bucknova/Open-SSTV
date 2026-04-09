# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.core.slant``.

The round-trip "resample at 47900 Hz, decode at 48000 Hz" drift test
lives in ``test_decoder.py`` so it can share the gradient fixture and
luma-error helpers; this file covers the fit primitives in isolation.
"""
from __future__ import annotations

import numpy as np

from sstv_app.core.slant import fit_line_timing, slant_corrected_line_starts
from sstv_app.core.sync import walk_sync_grid


def test_fit_line_timing_recovers_nominal_period() -> None:
    """Exact-period candidates should fit a line whose slope equals
    the nominal period and whose intercept equals the first candidate."""
    period = 7200.0  # Robot 36-ish line period in samples
    candidates = [int(round(i * period)) for i in range(240)]

    fit = fit_line_timing(candidates, period)
    assert fit is not None
    intercept, slope = fit
    assert abs(slope - period) < 1e-6
    assert abs(intercept - 0.0) < 1e-6


def test_fit_line_timing_recovers_drifted_period() -> None:
    """Candidates drifted at a fixed ppm should yield a slope that
    matches the actual period, not the nominal one. This is the core
    "how does slant correction work" check."""
    nominal = 7200.0
    actual = nominal * (47_900.0 / 48_000.0)  # -2083 ppm drift
    candidates = [int(round(i * actual)) for i in range(240)]

    fit = fit_line_timing(candidates, nominal)
    assert fit is not None
    _, slope = fit
    # Well under 1 sample per line — least-squares on integer-rounded
    # positions should recover the real slope to sub-sample precision.
    assert abs(slope - actual) < 0.5


def test_fit_line_timing_skips_spurious_anchor() -> None:
    """A spurious leading candidate (VIS residue, PySSTV's Scottie
    "initial" pre-line sync) should not poison the fit — the
    anchor-selection loop matches ``sync.walk_sync_grid`` so only
    candidates whose outgoing spacing is near the nominal period
    are considered as anchors."""
    period = 7200.0
    # Spurious candidate at t=0, real line 0 starts at 2800 samples
    # (about 285 ms at 48 kHz — the Scottie PySSTV case).
    real_start = 2800
    candidates = [0] + [real_start + int(round(i * period)) for i in range(240)]

    fit = fit_line_timing(candidates, period)
    assert fit is not None
    intercept, slope = fit
    # Intercept must land on the real line-0 position, not on the
    # spurious candidate at 0.
    assert abs(intercept - real_start) < 1.0
    assert abs(slope - period) < 1e-6


def test_fit_line_timing_returns_none_for_too_few_candidates() -> None:
    assert fit_line_timing([], 7200.0) is None
    assert fit_line_timing([0], 7200.0) is None
    assert fit_line_timing([0, 7200], 7200.0) is None  # below _MIN_FIT_POINTS


def test_fit_line_timing_returns_none_for_non_positive_period() -> None:
    assert fit_line_timing([0, 7200, 14400], 0.0) is None
    assert fit_line_timing([0, 7200, 14400], -1.0) is None


def test_fit_line_timing_tolerates_gaussian_jitter() -> None:
    """Detected sync positions always have a few samples of jitter from
    boxcar smoothing + threshold ringing. The fit must still recover
    the true slope within a fraction of a sample."""
    period = 7200.0
    rng = np.random.default_rng(0)
    jitter = rng.normal(0.0, 2.0, 240)  # 2-sample RMS jitter
    candidates = [int(round(i * period + jitter[i])) for i in range(240)]

    fit = fit_line_timing(candidates, period)
    assert fit is not None
    _, slope = fit
    # 240 samples averaged → slope std << per-point jitter
    assert abs(slope - period) < 0.1


def test_slant_corrected_line_starts_projects_across_all_slots() -> None:
    """The convenience wrapper should project the fit across exactly
    ``max_lines`` slots, even if we fed it many more candidates."""
    period = 7200.0
    candidates = [int(round(i * period)) for i in range(300)]  # extra
    starts = slant_corrected_line_starts(candidates, period, max_lines=240)
    assert len(starts) == 240
    # Should match the closed-form prediction since the fit is exact.
    for i, s in enumerate(starts):
        assert abs(s - i * period) <= 1


def test_slant_corrected_line_starts_falls_back_to_naive_walk() -> None:
    """With fewer than _MIN_FIT_POINTS candidates, the wrapper should
    delegate to ``walk_sync_grid`` so callers get the same pre-slant
    behavior instead of an empty list. ``walk_sync_grid`` stops at the
    last real candidate (no extrapolation), so we compare positions
    directly rather than hard-coding a length."""
    period = 7200.0
    candidates = [0, 7200]  # only 2 → can't fit
    starts = slant_corrected_line_starts(candidates, period, max_lines=5)
    expected = walk_sync_grid(candidates, period, max_lines=5)
    assert starts == expected
    # Sanity: walk_sync_grid at least returns the two real candidates.
    assert starts[0] == 0
    assert starts[1] == 7200


def test_slant_corrected_line_starts_empty_candidates_returns_empty() -> None:
    assert slant_corrected_line_starts([], 7200.0, 240) == []


def test_slant_corrected_line_starts_invalid_params_return_empty() -> None:
    assert slant_corrected_line_starts([0, 7200, 14400], 0.0, 240) == []
    assert slant_corrected_line_starts([0, 7200, 14400], 7200.0, 0) == []
