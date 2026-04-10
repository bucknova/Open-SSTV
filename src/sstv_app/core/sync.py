# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync pulse detection within a demodulated frequency track.

After FM demodulation we have an instantaneous-frequency time series (one Hz
value per audio sample). SSTV modes mark the start of each scan line with a
1200 Hz sync pulse whose duration varies by mode (4.862 ms for Martin M1,
9 ms for Robot 36 / Scottie S1). This module owns finding those sync pulses
so the per-mode pixel decoder can slice the rest of the audio into rows.

Two related operations live here:

* ``find_leader`` — locates the *first* long sync region (the 30 ms VIS
  start bit, which immediately follows the 600 ms 1900 Hz leader). Useful
  when a higher level only needs to know "is there an SSTV header here?",
  or to share leader-detection logic with ``vis.detect_vis``.

* ``find_line_starts`` — locates the per-line sync pulses inside the image
  body. The caller passes ``start_idx`` (typically ``vis.detect_vis``'s
  ``end_idx``) so we don't accidentally re-detect the VIS start/stop bits.

For modes with ``SyncPosition.LINE_START`` (Martin, Robot, PD …) the
returned sync indices *are* line starts. For Scottie family modes
(``SyncPosition.BEFORE_RED``) the sync sits mid-line, between the blue
and red scans; the per-mode decoder is responsible for offsetting back to
the line start using its own per-channel layout.

The line-sync detector treats the frequency track in three passes:

1. A **median pre-smoother** sized to half the mode's nominal sync
   pulse width flattens high-frequency noise. Median because additive
   noise on a narrow-band FM signal produces occasional large
   phase-slip "clicks" in the IF track, and a boxcar averages those
   clicks into the sync band while a median rejects them outright.
   (The leader pass uses a different, narrower 2 ms boxcar — wide
   enough to suppress sample-level jitter, narrow enough to keep the
   10 ms mid-leader break distinct from the 30 ms VIS start bit.)
2. An **adaptive threshold** is computed by rolling ``minimum_filter1d``
   and ``maximum_filter1d`` over ~2 line periods. At every sample,
   the threshold is ``local_min + 0.1 * (local_max - local_min)``, then
   clamped above at 1450 Hz. This replaces the hard ``1100 < f < 1300``
   band we originally used — a band is too brittle when RX LO drift,
   Doppler, or heavy noise shifts the whole track's sync level by more
   than ~100 Hz, because the old detector simply stopped seeing sync
   at all. The rolling min tracks local sync level; the 0.1 fraction
   keeps the threshold close to that floor (important because PySSTV's
   phase-continuous audio has ~10–15 sample *ramps* at every tone
   transition, and a threshold higher up the ramp produces inconsistent
   start positions). The 1450 Hz hard cap is what actually buys the
   shift-tolerance win: once the whole track moves upward by more than
   ~100 Hz the adaptive midpoint would track with it, but the cap pins
   the ceiling, giving ~250 Hz of upward headroom from the 1200 Hz
   nominal sync level while staying 50 Hz clear of the 1500 Hz porches.
3. The smoothed track is masked as ``smooth < threshold``. Maximal
   True runs are extracted; runs whose length sits within 50–200 % of
   the expected mode-specific sync length are kept. Runs much longer
   than that (the 30 ms VIS start/stop bits, dark image regions) and
   much shorter (the 0.5–1.5 ms 1500 Hz porches, transient ringing
   between adjacent VIS data bits) are rejected.

Public API
----------
find_leader(freq_track, fs) -> int | None
    Sample index of the VIS start bit's leading edge, or ``None`` if no
    leader was found.

find_sync_candidates(freq_track, fs, sync_pulse_ms, line_period_samples, start_idx=0) -> list[int]
    All plausible per-line sync pulse start indices (length-filtered but
    **not** spacing-filtered). ``line_period_samples`` sizes the rolling
    min/max window that drives the adaptive threshold; both callers
    (``find_line_starts`` and the Robot 36 line-pair dispatcher in the
    decoder) already have it on hand. Used by the decoder to detect
    whether a Robot 36 WAV uses PySSTV's 150 ms per-line layout or the
    canonical broadcast 290 ms line-pair layout before committing to a
    grid walk.

find_line_starts(freq_track, fs, spec, start_idx=0) -> list[int]
    Sample indices (into the original ``freq_track``, not the slice) of
    each detected per-line sync pulse, anchored on the first valid
    candidate at or after ``start_idx``. Returns up to ``spec.height``
    indices.

walk_sync_grid(candidates, line_period_samples, max_lines) -> list[int]
    Walk a list of candidate sync indices into an evenly-spaced grid of
    ``line_period_samples`` apart, filling missing slots with the
    predicted next index. Shared between ``find_line_starts`` and the
    Robot 36 line-pair path in the decoder.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter, minimum_filter1d

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from sstv_app.core.modes import ModeSpec


# === Internal constants ===

#: Boxcar smoothing window for the leader/VIS-start search. 2 ms is wide
#: enough to suppress sample-level jitter and narrow enough to keep the
#: 10 ms mid-leader break distinct from the 30 ms VIS start bit.
_LEADER_SMOOTH_S: float = 0.002

#: Fraction of the mode's nominal sync pulse width used as the median
#: filter window for per-line sync detection. Half the sync width is
#: the largest window that fully fits inside a legitimate sync pulse
#: (so the pulse still dominates the median and survives), while being
#: more than large enough to reject click-noise outliers that would
#: otherwise corrupt a boxcar-smoothed track.
_LINE_MEDIAN_FRAC: float = 0.5

#: Width of the rolling min/max window used by the adaptive threshold,
#: expressed in multiples of the line period. Two line periods guarantees
#: the window covers at least one sync pulse at every position (modulo a
#: couple of edge samples handled by scipy's ``mode='nearest'`` padding),
#: so the rolling min tracks sync level and the rolling max tracks
#: mid-luma — the midpoint between them is a stable sync/body divider.
_ADAPTIVE_WIN_LINES: float = 2.0

#: Fraction of the ``(local_max - local_min)`` range, added back to
#: ``local_min``, used as the adaptive threshold. A small value puts
#: the typical-case threshold close to the sync floor, which matters
#: because PySSTV-generated audio has ~10–15 sample *ramps* at every
#: sync boundary (phase-continuous tone transitions, not sharp steps),
#: and a threshold too far above the sync floor lands partway up the
#: ramp and produces inconsistent start positions (Martin M1 lines end
#: up with ~6 samples of std-dev jitter vs ~1.3 samples for a near-floor
#: threshold). At 0.1 the typical-case threshold is ~1310 Hz for a
#: 1200 Hz sync / 2300 Hz body track, effectively matching the old hard
#: ``< 1300`` upper bound. The 1450 Hz hard cap still kicks in once the
#: whole track shifts upward — that's where the adaptive detector
#: actually buys us over the old hard threshold.
_ADAPTIVE_THR_FRAC: float = 0.1

#: Hard upper bound on the adaptive threshold, in Hz. Without this cap
#: the threshold would climb above the 1500 Hz porch level in the corner
#: case of a dark, low-contrast image body (because ``local_max`` there
#: sits close to 1500 Hz and the midpoint to a sync-level ``local_min``
#: lands just below the porches). 1450 Hz gives sync 250 Hz of upward
#: headroom from its 1200 Hz nominal while staying 50 Hz clear of
#: 1500 Hz porches that would otherwise get merged into sync runs.
_HARD_SYNC_UPPER_HZ: float = 1450.0

#: Hard lower bound on the adaptive threshold, in Hz. On noisy
#: acoustic-coupled signals the rolling minimum can dip hundreds of Hz
#: below the true 1200 Hz sync level (FM-demod phase-slip noise), pulling
#: the adaptive threshold well below sync. Clamping at 1300 Hz ensures
#: real sync pulses (median-smoothed to ~1200 Hz) always fall below the
#: threshold. 1300 Hz is the same value the adaptive formula naturally
#: produces on clean signals (1200 + 0.1 × 1100 ≈ 1310), so this floor
#: doesn't change clean-signal behavior — it just prevents collapse on
#: noisy input. The 200 Hz gap to the 1500 Hz porch level is comfortable.
_HARD_SYNC_LOWER_HZ: float = 1300.0

#: Lower bound on a "VIS start bit" candidate run, in seconds. The mid-leader
#: break is 10 ms, so 20 ms cleanly rejects it.
_MIN_LEADER_RUN_S: float = 0.020

#: Sync runs whose length is between this fraction and the corresponding
#: ``_MAX_LINE_SYNC_RATIO`` of the mode's nominal sync length are accepted
#: as line syncs.
_MIN_LINE_SYNC_RATIO: float = 0.5
_MAX_LINE_SYNC_RATIO: float = 2.0

#: Allowed slack on the inter-line spacing when validating consecutive
#: candidates. 25 % is generous enough to absorb any plausible TX/RX clock
#: drift while still rejecting spurious mid-line sync-band crossings.
_LINE_SPACING_TOLERANCE: float = 0.25


def find_leader(
    freq_track: NDArray, fs: int
) -> int | None:
    """Locate the end of the SSTV leader (= start of the VIS start bit).

    Returns the sample index of the first long-enough 1200 Hz run in
    ``freq_track``, or ``None`` if no leader-shaped pattern was found.
    Does **not** decode the VIS byte — call ``vis.detect_vis`` for that.

    The returned index is roughly aligned to the *leading* edge of the
    start bit (the 1900 → 1200 Hz transition under the leader smoother),
    so callers can use it as the anchor for VIS bit slicing or as a
    "we have an SSTV signal here" indicator.
    """
    arr = np.asarray(freq_track)
    if arr.ndim != 1 or arr.size == 0:
        return None

    smooth_n = max(1, int(round(_LEADER_SMOOTH_S * fs)))
    smooth = _boxcar(arr, smooth_n)

    sync_mask = (smooth > 1100.0) & (smooth < 1300.0)
    runs = _find_runs(sync_mask)
    min_run = int(round(_MIN_LEADER_RUN_S * fs))

    for run_start, run_end in runs:
        if (run_end - run_start) >= min_run:
            # Compensate for the leading-edge offset introduced by the
            # boxcar smoother (see vis.detect_vis for the same correction).
            return max(0, run_start - smooth_n // 2)

    return None


def find_sync_candidates(
    freq_track: NDArray,
    fs: int,
    sync_pulse_ms: float,
    line_period_samples: float,
    start_idx: int = 0,
) -> list[int]:
    """Length-filtered sync pulse candidates, with no spacing filter.

    Returns the sample indices (into the original ``freq_track``) of
    every sync-band run whose duration falls within 50–200 % of the
    given ``sync_pulse_ms``. ``line_period_samples`` sizes the adaptive
    threshold's rolling window (2 × line period); the caller typically
    feeds the return value into ``walk_sync_grid`` after deciding on
    the expected line period — or uses the raw list to auto-detect
    the period itself.

    The frequency track is pre-smoothed with a **median filter** sized
    to half the sync pulse width. Median-instead-of-boxcar is a
    Phase 2.5 robustness fix: additive white noise on a narrow-band
    FM signal produces occasional large phase-slip "click" spikes in
    the demodulated IF track, and a boxcar averages those clicks
    straight into the sync band where they manufacture spurious runs.
    The median filter rejects them outright as long as each click is
    shorter than half the window. Unlike a boxcar, median is
    edge-preserving on a clean step — the sync-band crossing lands at
    the true transition sample rather than somewhere inside a linear
    ramp — so no leading-edge correction is applied to the reported
    indices.

    The smoothed track is compared against a **rolling adaptive
    threshold**: at every sample, ``threshold = local_min + 0.1 *
    (local_max - local_min)``, where ``local_min`` and ``local_max``
    are taken over a 2-line-period window, then capped above at
    1450 Hz. The adaptive floor-tracking replaces the old hard
    ``1100 < f < 1300`` band, which collapsed the moment noise or LO
    drift shifted the whole track's sync level by more than ~100 Hz.
    The 0.1 fraction (see ``_ADAPTIVE_THR_FRAC``) keeps the typical
    threshold near the sync floor — about 1310 Hz on a 1200/2300 Hz
    track — which matters because PySSTV's tone transitions are
    linear ramps, not sharp steps, and a higher threshold lands
    partway up the ramp and jitters the detected start positions.
    The 1450 Hz hard cap is what gives the shift-tolerance win: it
    pins the ceiling independently of the adaptive arithmetic, so the
    detector still sees sync even when LO drift/Doppler shifts the
    whole track by up to ~250 Hz. The cap is still below the 1500 Hz
    porch level, so Martin/Scottie porches never merge into sync runs.

    This function is intentionally permissive — it can yield spurious
    candidates such as VIS stop-bit residue (a 1200 Hz run that
    started before ``start_idx`` and leaked past it, then got chopped
    to length-valid by the ``arr[start_idx:]`` slice). ``walk_sync_grid``
    filters those out by anchor selection rather than forcing every
    caller to re-implement residue detection.
    """
    arr = np.asarray(freq_track)
    if arr.ndim != 1 or arr.size == 0:
        return []
    if start_idx < 0 or start_idx >= arr.size:
        return []

    sync_samples = sync_pulse_ms / 1000.0 * fs
    min_sync_run = max(1, int(round(_MIN_LINE_SYNC_RATIO * sync_samples)))
    max_sync_run = max(
        min_sync_run + 1, int(round(_MAX_LINE_SYNC_RATIO * sync_samples))
    )

    # Size the median window to half the sync pulse width, rounded to
    # an odd integer (scipy's median_filter accepts even windows but
    # the symmetry is cleaner with odd). At minimum 3 samples so
    # degenerate high-fs / short-sync inputs still smooth *something*.
    median_n = max(3, int(round(_LINE_MEDIAN_FRAC * sync_samples)))
    if median_n % 2 == 0:
        median_n += 1

    sliced = arr[start_idx:]
    smooth = _median_smooth(sliced, median_n)

    # Adaptive threshold: rolling min/max over ~2 line periods, then
    # their midpoint (with a hard 1450 Hz cap for the dark-body corner
    # case). Window clamped to the slice size so scipy's filter still
    # behaves on short synthetic test tracks; ``mode='nearest'`` pads
    # edges with replicated values, which matches the boxcar/median
    # helpers' ``mode='same'`` contract.
    adapt_win = max(3, int(round(_ADAPTIVE_WIN_LINES * line_period_samples)))
    if adapt_win % 2 == 0:
        adapt_win += 1
    adapt_win = min(adapt_win, max(3, smooth.size))
    local_min = minimum_filter1d(smooth, size=adapt_win, mode="nearest")
    local_max = maximum_filter1d(smooth, size=adapt_win, mode="nearest")
    adaptive_thr = local_min + _ADAPTIVE_THR_FRAC * (local_max - local_min)
    threshold = np.clip(adaptive_thr, _HARD_SYNC_LOWER_HZ, _HARD_SYNC_UPPER_HZ)

    sync_mask = smooth < threshold
    runs = _find_runs(sync_mask)

    return [
        start_idx + run_start
        for run_start, run_end in runs
        if min_sync_run <= run_end - run_start <= max_sync_run
    ]


def walk_sync_grid(
    candidates: list[int],
    line_period_samples: float,
    max_lines: int,
) -> list[int]:
    """Walk raw sync candidates into an evenly-spaced grid.

    Finds the first candidate whose outgoing spacing matches the expected
    line period within ±25 %, anchors the grid there, and walks forward.
    Candidates before the anchor are discarded as spurious — this is how
    we skip things like VIS stop-bit residue that ``find_sync_candidates``
    intentionally lets through, or PySSTV's Scottie "initial" pre-line
    sync that sits one partial line before the real line-0 sync. Falls
    back to ``candidates[0]`` if no pair matches, so well-formed inputs
    with only one candidate still produce a best-effort grid.

    Gaps (missing or out-of-tolerance candidates) are filled with the
    predicted index so downstream decoders can still slice a plausible
    scan line there.

    Returns up to ``max_lines`` grid indices, or an empty list if
    ``candidates`` is empty.
    """
    if not candidates or line_period_samples <= 0 or max_lines <= 0:
        return []

    tolerance = line_period_samples * _LINE_SPACING_TOLERANCE

    # Pick an anchor candidate whose distance to the next candidate
    # matches the expected line period. This skips spurious leading
    # candidates — the two we see in practice are (a) VIS stop-bit
    # residue that leaked past ``start_idx`` and got chopped to a
    # length-valid run, and (b) PySSTV's Scottie "initial" sync pulse,
    # which precedes the line-0 mid-line sync by ~285 ms rather than a
    # full 428 ms line period. Both confuse a naive ``[candidates[0]]``
    # anchor, and both are trivially rejected by this pair-wise check.
    anchor_idx = 0
    for i in range(len(candidates) - 1):
        if (
            abs(candidates[i + 1] - candidates[i] - line_period_samples)
            <= tolerance
        ):
            anchor_idx = i
            break

    line_starts: list[int] = [candidates[anchor_idx]]
    next_idx = anchor_idx + 1
    while len(line_starts) < max_lines and next_idx < len(candidates):
        expected = line_starts[-1] + line_period_samples
        # Skip candidates that fall well before the expected slot (these
        # are spurious mid-line sync-band crossings).
        while (
            next_idx < len(candidates)
            and candidates[next_idx] < expected - tolerance
        ):
            next_idx += 1
        if next_idx >= len(candidates):
            break
        c = candidates[next_idx]
        if abs(c - expected) <= tolerance:
            line_starts.append(c)
            next_idx += 1
        else:
            # Gap in the sync run (lost line) — advance the predicted slot
            # by one full line and try again rather than abandoning.
            line_starts.append(int(round(expected)))
    return line_starts


def find_line_starts(
    freq_track: NDArray,
    fs: int,
    spec: ModeSpec,
    start_idx: int = 0,
) -> list[int]:
    """Find per-line sync pulse start indices.

    Parameters
    ----------
    freq_track:
        Per-sample instantaneous frequency in Hz, as produced by
        ``demod.instantaneous_frequency``.
    fs:
        Sample rate the frequency track was computed at.
    spec:
        Mode specification — used for the expected sync pulse duration
        (filtering out the 30 ms VIS start/stop bits as too long, and
        sub-millisecond noise spikes as too short) and the expected line
        spacing (rejecting candidates that fall outside ±25 % of the
        anchored grid).
    start_idx:
        Index in ``freq_track`` to start searching from. Pass the
        ``end_idx`` returned by ``vis.detect_vis`` so we don't re-detect
        the VIS start/stop bits as candidate line syncs.

    Returns
    -------
    list[int]
        Up to ``spec.height`` sample indices into the original
        ``freq_track`` (not into the post-``start_idx`` slice). Returns
        an empty list if no plausible line syncs were found.
    """
    line_samples = spec.line_time_ms / 1000.0 * fs
    candidates = find_sync_candidates(
        freq_track,
        fs,
        spec.sync_pulse_ms,
        line_period_samples=line_samples,
        start_idx=start_idx,
    )
    return walk_sync_grid(candidates, line_samples, spec.height)


# === Internal helpers ===


def _boxcar(x: NDArray, n: int) -> NDArray:
    """Centered boxcar smooth. ``mode='same'`` so the output indexes 1:1
    with the input — important for keeping detected positions accurate."""
    if n <= 1:
        return np.asarray(x, dtype=np.float64)
    kernel = np.ones(n, dtype=np.float64) / n
    return np.convolve(x, kernel, mode="same")


def _median_smooth(x: NDArray, n: int) -> NDArray:
    """Centered 1-D median filter. Edge-replicated so the output indexes
    1:1 with the input (matching ``_boxcar``'s ``mode='same'`` contract).

    Delegates to ``scipy.ndimage.median_filter`` which uses a histogram
    / rank-based algorithm — ~45 ms on a 1.7 M-sample Robot 36 track
    with a 217-sample window on a modern laptop, which is well under
    our per-flush budget in ``RxWorker``.
    """
    if n <= 1:
        return np.asarray(x, dtype=np.float64)
    return median_filter(
        np.asarray(x, dtype=np.float64), size=n, mode="nearest"
    )


def _find_runs(mask: NDArray[np.bool_]) -> list[tuple[int, int]]:
    """Return ``[(start, end), …]`` for each maximal True run in ``mask``.

    ``end`` is exclusive (slice-friendly). Empty input returns an empty list.
    """
    if mask.size == 0:
        return []
    diff = np.diff(mask.astype(np.int8))
    starts = (np.where(diff == 1)[0] + 1).tolist()
    ends = (np.where(diff == -1)[0] + 1).tolist()
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(int(mask.size))
    return list(zip(starts, ends, strict=True))


__all__ = [
    "find_leader",
    "find_line_starts",
    "find_sync_candidates",
    "walk_sync_grid",
]
