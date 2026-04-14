# SPDX-License-Identifier: GPL-3.0-or-later
"""Slant correction for TX/RX clock drift.

SSTV is an open-loop system: the transmitter clocks out its FM signal
at one nominal sample rate, and the receiver digitizes it at its own
nominal sample rate. Real-world sound cards disagree with each other
— and with their nameplate frequencies — by anywhere from a handful of
ppm on precision hardware to a couple of thousand ppm on cheap USB
dongles. Over a 36 s Robot 36 or 110 s Scottie S1 transmission that
drift accumulates into a visible horizontal slant: each scan line is
sampled slightly earlier (or later) than the nominal line period,
and the image tilts.

The classic slowrx-style fix would be to fit a line through the
detected sync positions and resample the audio so the effective rate
matches the transmitter. We do the mathematically-equivalent but much
cheaper thing: fit the line, then use its slope as the **actual**
per-line period for pixel slicing. No audio resampling, no filter
ringing, no extra allocation.

Algorithm
---------

``fit_line_timing`` takes the raw length-filtered sync candidates
produced by ``sync.find_sync_candidates`` and returns an
``(intercept, slope)`` pair such that::

    position_of_line_i ≈ intercept + i * slope

The fit is plain least-squares over candidates that land on an integer
multiple of the nominal period (up to rounding). ``slope`` is the real
per-line period in audio samples and typically sits within a few hundred
ppm of the nominal value; the decoder's pixel slicer uses it in place of
``spec.line_time_ms / 1000 * fs`` and the slant is gone.

``slant_corrected_line_starts`` is the convenience wrapper the decoder
actually calls: fit the line, project it across the first ``max_lines``
slots, fall back to a naive nominal-period walk when the fit is
under-determined.

Limitations
-----------

* Plain least-squares — no outlier rejection. Works fine for drifts
  below about 4000 ppm (0.4 %) because each candidate still rounds
  to its correct integer line index; for heavier drifts we'd need
  iterative reweighting or a robust regressor. Cheap USB dongles can
  hit 2000 ppm so we have comfortable headroom; pathological hardware
  is explicitly a post-v1 problem.
* ``fit_line_timing`` assumes the anchor candidate is at line 0. The
  anchor-selection logic (inherited from ``sync.walk_sync_grid``)
  skips spurious leading candidates, so this is true in practice for
  every encoder format we support.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from open_sstv.core.sync import walk_sync_grid

if TYPE_CHECKING:
    pass


#: Candidates must sit within this fraction of the nominal line period
#: from a whole-integer slot to be included in the fit. 25 % matches
#: ``sync._LINE_SPACING_TOLERANCE`` so the anchor-selection and fit
#: stages agree on what "same slot" means.
_FIT_TOLERANCE: float = 0.25

#: Minimum number of candidates required to attempt a least-squares
#: fit. Two points define a line exactly (no error signal), so three
#: is the smallest sample that can actually be called a "fit". Under
#: this we fall back to the naive grid walk.
_MIN_FIT_POINTS: int = 3


def fit_line_timing(
    candidates: list[int],
    nominal_period: float,
    tolerance: float = _FIT_TOLERANCE,
) -> tuple[float, float] | None:
    """Least-squares fit a line through raw sync candidates.

    Returns ``(intercept, slope)`` such that the predicted position of
    line index ``i`` is ``intercept + i * slope``. ``intercept`` is the
    fitted position of line 0 (the anchor) and ``slope`` is the real
    per-line period in audio samples — which will differ from
    ``nominal_period`` by the TX/RX clock drift.

    Returns ``None`` if there are fewer than ``_MIN_FIT_POINTS`` usable
    candidates (so the caller can fall back to a naive grid walk), if
    ``nominal_period`` is non-positive, or if all surviving candidates
    collapse to the same line index (degenerate fit).

    The algorithm:

    1. Pick an anchor candidate whose outgoing spacing matches
       ``nominal_period`` within ``tolerance``. This matches
       ``sync.walk_sync_grid`` so spurious leading candidates (VIS
       residue, PySSTV's Scottie "initial" pre-line sync) are skipped.
    2. Assign every candidate from the anchor onward to its nearest
       integer line index via ``round((c - anchor) / nominal_period)``.
       De-duplicate on line index (keep first).
    3. Least-squares fit ``position = intercept + slope * line_index``.
    """
    if nominal_period <= 0 or len(candidates) < _MIN_FIT_POINTS:
        return None

    tol_samples = nominal_period * tolerance

    # Anchor selection: same pair-wise logic as ``sync.walk_sync_grid``.
    # Stepping past spurious leading candidates is the difference between
    # "line 0 is at intercept" and "line 0 is at intercept + 285 ms",
    # which turns a clean fit into garbage.
    anchor_idx = 0
    for i in range(len(candidates) - 1):
        if (
            abs(candidates[i + 1] - candidates[i] - nominal_period)
            <= tol_samples
        ):
            anchor_idx = i
            break

    # Estimate the real per-slot period from the median of adjacent
    # diffs (restricted to diffs near the nominal so we skip spurious
    # huge gaps from missing syncs). We use this — not the nominal —
    # for slot assignment below, which matters in two scenarios:
    #
    #   * Heavy clock drift where nominal_period is off by a few %,
    #     e.g. a cheap sound card at 47 900 Hz vs. the claimed 48 kHz.
    #   * The Robot 36 line-pair path, where the dispatcher passes
    #     ``pair_samples = 2 * line_samples = 14400`` as nominal, but
    #     the canonical super-line is actually ~13944 samples at
    #     48 kHz — a ~3.2 % mismatch. Using the nominal for slot
    #     assignment makes candidates collide on a shared integer
    #     slot once cumulative drift passes half a period.
    diffs = np.diff(np.asarray(candidates[anchor_idx:], dtype=np.float64))
    near_nominal = diffs[
        (diffs >= nominal_period - tol_samples)
        & (diffs <= nominal_period + tol_samples)
    ]
    if near_nominal.size == 0:
        return None
    inferred_period = float(np.median(near_nominal))

    anchor_pos = float(candidates[anchor_idx])

    # Round each candidate onto its nearest integer line slot using the
    # inferred period. Any two candidates that land on the same slot
    # are deduplicated (keep first) — this shouldn't happen after the
    # inferred-period correction but guards against degenerate inputs.
    seen_indices: set[int] = set()
    x_list: list[float] = []
    y_list: list[float] = []
    for c in candidates[anchor_idx:]:
        rel = (float(c) - anchor_pos) / inferred_period
        idx = int(round(rel))
        if idx < 0 or idx in seen_indices:
            continue
        seen_indices.add(idx)
        x_list.append(float(idx))
        y_list.append(float(c))

    if len(x_list) < _MIN_FIT_POINTS:
        return None

    x = np.asarray(x_list, dtype=np.float64)
    y = np.asarray(y_list, dtype=np.float64)
    # Degenerate: all candidates landed on the same slot (shouldn't
    # happen after dedup, but guard against np.polyfit raising).
    if x.max() == x.min():
        return None

    slope, intercept = np.polyfit(x, y, 1)
    return float(intercept), float(slope)


def slant_corrected_line_starts(
    candidates: list[int],
    nominal_period: float,
    max_lines: int,
    tolerance: float = _FIT_TOLERANCE,
) -> list[int]:
    """Return slant-corrected line-start positions.

    Fits a least-squares line through ``candidates`` and projects it
    across the first ``max_lines`` line slots. The returned positions
    follow the actual TX/RX clock ratio rather than the nominal one,
    so downstream pixel slicing sees lines at their true offsets even
    when the receiver's sound card is drifting by up to ~4000 ppm.

    Falls back to ``sync.walk_sync_grid`` whenever the fit is
    under-determined (too few candidates, non-positive period, etc.),
    so callers don't have to special-case degenerate inputs. The
    fallback preserves the pre-slant Phase 2 step 13 behavior.
    """
    if not candidates or nominal_period <= 0 or max_lines <= 0:
        return []

    fit = fit_line_timing(candidates, nominal_period, tolerance)
    if fit is None:
        return walk_sync_grid(candidates, nominal_period, max_lines)

    intercept, slope = fit
    return [int(round(intercept + i * slope)) for i in range(max_lines)]


__all__ = [
    "fit_line_timing",
    "slant_corrected_line_starts",
]
