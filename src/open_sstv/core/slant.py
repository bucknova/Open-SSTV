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

The fit is least-squares over candidates that land on an integer
multiple of the nominal period (up to rounding) **inside the image** —
slots ``0 .. max_lines-1`` — followed by one MAD-based outlier-rejection
refit. ``slope`` is the real
per-line period in audio samples and typically sits within a few hundred
ppm of the nominal value; the decoder's pixel slicer uses it in place of
``spec.line_time_ms / 1000 * fs`` and the slant is gone.

``slant_corrected_line_starts`` is the convenience wrapper the decoder
actually calls: fit the line, project it across the first ``max_lines``
slots, fall back to a naive nominal-period walk when the fit is
under-determined.

Limitations
-----------

* One round of outlier rejection (residual > 3 robust sigmas, with a
  small floor) — not a full robust regressor. Works for drifts below
  about 4000 ppm (0.4 %) because each candidate still rounds to its
  correct integer line index; cheap USB dongles hit ~2000 ppm, so there
  is comfortable headroom.
* v0.6.14 audit D-H3: candidates past the image (CW ID, noise, the next
  QSO) used to enter the unweighted fit and skew every line of a file
  decode by up to ~35/255 MAE. They are now dropped by slot index
  before fitting.
* The anchor (line-0 position) comes from ``sync.select_anchor``, the
  same choice ``walk_sync_grid`` makes. Pass ``first_slot_hint`` (from
  ``sync.line0_sync_hint``) so a missed line-0/1 sync is extrapolated
  back instead of shifting the fit by whole rows (audit D-H1).
"""
from __future__ import annotations

import numpy as np

from open_sstv.core.sync import select_anchor, walk_sync_grid

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

#: Outlier rejection for the refit: drop candidates whose residual from
#: the first fit exceeds this many robust sigmas (1.4826 × MAD) ...
_OUTLIER_SIGMAS: float = 3.0

#: ... or this fraction of the line period, whichever is larger. The
#: floor stops a near-perfect clean fit (MAD ≈ 1 sample) from discarding
#: legitimate candidates that are merely a couple of samples off.
_OUTLIER_FLOOR_FRAC: float = 0.005


def fit_line_timing(
    candidates: list[int],
    nominal_period: float,
    tolerance: float = _FIT_TOLERANCE,
    *,
    max_lines: int | None = None,
    first_slot_hint: float | None = None,
) -> tuple[float, float] | None:
    """Least-squares fit a line through raw sync candidates.

    Returns ``(intercept, slope)`` such that the predicted position of
    line index ``i`` is ``intercept + i * slope``. ``intercept`` is the
    fitted position of line 0 and ``slope`` is the real per-line period
    in audio samples — which will differ from ``nominal_period`` by the
    TX/RX clock drift.

    Returns ``None`` if there are fewer than ``_MIN_FIT_POINTS`` usable
    candidates (so the caller can fall back to a naive grid walk), if
    ``nominal_period`` is non-positive, or if all surviving candidates
    collapse to the same line index (degenerate fit).

    The algorithm:

    1. Pick the anchor and its line slot with ``sync.select_anchor`` —
       the same choice ``walk_sync_grid`` makes, so spurious leading
       candidates are skipped and a missed line-0/1 sync doesn't shift
       the slot numbering (``first_slot_hint``; audit D-H1).
    2. Assign every candidate from the anchor onward to its nearest
       integer line slot. De-duplicate on slot (keep first) and, when
       ``max_lines`` is given, drop slots ≥ ``max_lines`` — trailing
       audio after the image is not image timing (audit D-H3).
    3. Least-squares fit ``position = intercept + slope * slot``, then
       drop candidates whose residual exceeds max(3 robust sigmas,
       0.5 % of a period) and refit once.
    """
    if nominal_period <= 0 or len(candidates) < _MIN_FIT_POINTS:
        return None

    tol_samples = nominal_period * tolerance

    anchor_idx, anchor_slot = select_anchor(
        candidates, nominal_period, tol_samples, first_slot_hint
    )

    # Estimate the real per-slot period from the median of adjacent
    # diffs (restricted to diffs near the nominal so we skip spurious
    # huge gaps from missing syncs). We use this — not the nominal —
    # for slot assignment below: under heavy clock drift (a cheap sound
    # card at 47 900 Hz vs. the claimed 48 kHz) rounding with the nominal
    # period makes candidates collide on a shared slot once cumulative
    # drift passes half a period.
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
        idx = anchor_slot + int(round(rel))
        if idx < 0 or idx in seen_indices:
            continue
        if max_lines is not None and idx >= max_lines:
            # Candidates are sorted, so everything after is past the
            # image too (D-H3).
            break
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

    # One MAD outlier-rejection refit (D-H3): a stray noise candidate that
    # happened to round onto a slot still pulls an unweighted fit.
    resid = y - (intercept + slope * x)
    mad = float(np.median(np.abs(resid - np.median(resid))))
    limit = max(_OUTLIER_SIGMAS * 1.4826 * mad, _OUTLIER_FLOOR_FRAC * nominal_period)
    keep = np.abs(resid) <= limit
    if not keep.all() and int(keep.sum()) >= _MIN_FIT_POINTS:
        xk = x[keep]
        if xk.max() > xk.min():
            slope, intercept = np.polyfit(xk, y[keep], 1)

    return float(intercept), float(slope)


def slant_corrected_line_starts(
    candidates: list[int],
    nominal_period: float,
    max_lines: int,
    tolerance: float = _FIT_TOLERANCE,
    *,
    first_slot_hint: float | None = None,
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

    fit = fit_line_timing(
        candidates, nominal_period, tolerance,
        max_lines=max_lines, first_slot_hint=first_slot_hint,
    )
    if fit is None:
        return walk_sync_grid(
            candidates, nominal_period, max_lines,
            first_slot_hint=first_slot_hint,
        )

    intercept, slope = fit
    return [int(round(intercept + i * slope)) for i in range(max_lines)]


__all__ = [
    "fit_line_timing",
    "slant_corrected_line_starts",
]
