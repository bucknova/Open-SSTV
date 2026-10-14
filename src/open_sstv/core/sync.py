# SPDX-License-Identifier: GPL-3.0-or-later
"""Sync pulse detection within a demodulated frequency track.

After FM demodulation we have an instantaneous-frequency time series (one Hz
value per audio sample). SSTV modes mark the start of each scan line with a
1200 Hz sync pulse whose duration varies by mode (4.862 ms for Martin M1,
9 ms for Robot 36 / Scottie S1). This module owns finding those sync pulses
so the per-mode pixel decoder can slice the rest of the audio into rows.

The entry point is ``find_sync_candidates`` (length-filtered 1200 Hz runs
after the VIS), followed by ``walk_sync_grid`` (snap the candidates onto an
evenly spaced line grid, filling missed lines) or, for one-shot decodes,
``slant.slant_corrected_line_starts`` (least-squares fit through the
same candidates).

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
   (VIS detection in ``core/vis.py`` uses its own narrower 2 ms boxcar.)
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
find_sync_candidates(freq_track, fs, sync_pulse_ms, line_period_samples, start_idx=0) -> list[int]
    All plausible per-line sync pulse start indices (length-filtered but
    **not** spacing-filtered). ``line_period_samples`` sizes the rolling
    min/max window that drives the adaptive threshold.

line0_sync_hint(spec, fs, vis_end) -> float
    Where line 0's sync pulse should sit, given the VIS end. Passed to
    the grid walk / slant fit so a missed sync on line 0 or 1 can be
    extrapolated back to the right slot (v0.6.14 audit D-H1).

select_anchor(candidates, line_period_samples, tolerance, first_slot_hint=None) -> (int, int)
    Pick the candidate the grid is anchored on and the line slot it sits
    in. Shared by ``walk_sync_grid`` and ``slant.fit_line_timing`` so the
    two agree on which candidate is line 0.

walk_sync_grid(candidates, line_period_samples, max_lines, *, first_slot_hint=None) -> list[int]
    Walk a list of candidate sync indices into an evenly-spaced grid of
    ``line_period_samples`` apart, filling missing slots with the
    predicted next index.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter, minimum_filter1d

from open_sstv.core.dsp_utils import find_runs
from open_sstv.core.modes import SyncPosition

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from open_sstv.core.modes import ModeSpec


# === Internal constants ===

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

#: Sync runs whose length is between this fraction and the corresponding
#: ``_MAX_LINE_SYNC_RATIO`` of the mode's nominal sync length are accepted
#: as line syncs.
_MIN_LINE_SYNC_RATIO: float = 0.5
_MAX_LINE_SYNC_RATIO: float = 2.0

#: Systematic delay of the trailing-edge (sync → 1500 Hz porch) threshold
#: crossing on the bandpassed IF track, added back when a sync start is
#: inferred as ``run_end − nominal_length`` (see ``find_sync_candidates``).
#: Measured on encoder audio across all mode families: −2 … −4.6 samples
#: at 48 kHz before this correction.
_TRAILING_EDGE_LAG_S: float = 0.00008

#: Furthest the trailing-edge estimate may move a sync start ahead of its
#: leading-edge threshold crossing. The leading edge is ≤ ~0.25 ms late on
#: clean audio; anything beyond this means the pulse is not nominal length.
_MAX_EDGE_BACKDATE_S: float = 0.0003

#: Allowed slack on the inter-line spacing when validating consecutive
#: candidates. 25 % is generous enough to absorb any plausible TX/RX clock
#: drift while still rejecting spurious mid-line sync-band crossings.
_LINE_SPACING_TOLERANCE: float = 0.25

#: Largest outgoing gap, in line periods, that still qualifies a candidate
#: as the grid anchor. 1 would mean "the next line's sync must be there";
#: v0.6.14 audit D-H1 showed that one missed sync on line 1 then pushed
#: the anchor down a row (and swapped Robot 36's Cr/Cb parity). A few
#: periods is plenty to bridge a fade without letting random spacing
#: between noise candidates qualify very often.
_MAX_ANCHOR_GAP_LINES: int = 4


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
    # edges with replicated values, which matches the median helper's
    # 1:1 indexing contract.
    adapt_win = max(3, int(round(_ADAPTIVE_WIN_LINES * line_period_samples)))
    if adapt_win % 2 == 0:
        adapt_win += 1
    adapt_win = min(adapt_win, max(3, smooth.size))
    local_min = minimum_filter1d(smooth, size=adapt_win, mode="nearest")
    local_max = maximum_filter1d(smooth, size=adapt_win, mode="nearest")
    adaptive_thr = local_min + _ADAPTIVE_THR_FRAC * (local_max - local_min)
    threshold = np.clip(adaptive_thr, _HARD_SYNC_LOWER_HZ, _HARD_SYNC_UPPER_HZ)

    sync_mask = smooth < threshold
    runs = [
        (run_start, run_end)
        for run_start, run_end in find_runs(sync_mask)
        if min_sync_run <= run_end - run_start <= max_sync_run
    ]
    if not runs:
        return []

    # Where each sync *starts*. The threshold crossing of the leading
    # edge is a biased estimate: the threshold sits near the sync floor
    # (~1310 Hz) and the bandpassed IF falls slowly (~1 ms ramp), so it is
    # crossed late — ~0.2 ms late when the edge comes down from image
    # content (Scottie / Wraase B, PD Y1, Robot 36 chroma), ~0.03 ms when
    # it comes down from a 1500 Hz porch (Martin). Every mode has a
    # 1500 Hz porch *after* its sync, so the trailing edge (1200 → 1500)
    # is the same shape everywhere: ``end − nominal_length`` lands within
    # ~0.1 ms of the true start regardless of image content (measured SD
    # 0.4–1.6 samples vs. 1–4 for the leading edge), and a fixed small
    # correction removes the rest.
    #
    # The trailing estimate may only move a start *earlier* than the
    # leading-edge crossing (which is never early on a clean edge), and by
    # at most ``_MAX_EDGE_BACKDATE_S`` — so a transmitter whose sync is
    # shorter or longer than nominal degrades to the old estimate instead
    # of shifting the whole image.
    #
    # This matters for Scottie's spec layout (v0.6.14 audit D-L3), where
    # B runs straight into the sync with no porch: a 0.2 ms-late grid
    # read the sync into B's last pixel. It also fixes the v0.6.14 audit
    # "live line-0 offset" (~0.3 ms, ~1 px on Martin M4): a run already in
    # progress at ``start_idx`` — line 0 of a LINE_START mode abuts the
    # 1200 Hz VIS stop bit — has no leading edge at all, so it now takes
    # its start from the trailing edge like every other line.
    lag = _TRAILING_EDGE_LAG_S * fs
    back = _MAX_EDGE_BACKDATE_S * fs
    starts: list[int] = []
    for run_start, run_end in runs:
        if run_end >= sliced.size:
            # No trailing edge yet (run cut off by the buffer end).
            starts.append(run_start)
            continue
        est = run_end - sync_samples + lag
        if run_start == 0 and sync_mask[0]:
            # Leading edge clipped by the slice start: trailing edge only.
            starts.append(int(round(est)))
            continue
        est = max(run_start - back, est)
        starts.append(int(round(min(float(run_start), est))))
    return [start_idx + st for st in starts]


def first_sync_offset_samples(spec: ModeSpec, fs: int) -> float:
    """Distance from the start of a scan line to its sync pulse, in samples.

    Zero for ``LINE_START`` modes. For Scottie (``BEFORE_RED``) the sync
    follows separator + G + separator + B, so line 0's sync sits that far
    after the VIS. Uses the spec layout (``scan = (line − sync − 3·sep)/3``);
    PySSTV's old split put the same channel *starts* in the same places,
    so this is right for audio from either encoder.
    """
    if spec.sync_position == SyncPosition.BEFORE_RED:
        scan_ms = (
            spec.line_time_ms - spec.sync_pulse_ms - 3.0 * spec.sync_porch_ms
        ) / 3.0
        return 2.0 * (spec.sync_porch_ms + scan_ms) / 1000.0 * fs
    return 0.0


def line0_sync_hint(spec: ModeSpec, fs: int, vis_end: float) -> float:
    """Expected sample index of line 0's sync pulse, given the VIS end.

    Feed this to ``walk_sync_grid`` / ``slant_corrected_line_starts`` as
    ``first_slot_hint`` so a missed line-0/1 sync is extrapolated back
    instead of shifting the whole image up (v0.6.14 audit D-H1).
    """
    return float(vis_end) + first_sync_offset_samples(spec, fs)


def select_anchor(
    candidates: list[int],
    line_period_samples: float,
    tolerance: float,
    first_slot_hint: float | None = None,
) -> tuple[int, int]:
    """Choose the grid anchor: ``(candidate_index, line_slot)``.

    The anchor is the first candidate whose outgoing gap is a whole
    number ``k`` (1 ≤ k ≤ ``_MAX_ANCHOR_GAP_LINES``) of line periods, ±
    ``tolerance`` samples. Accepting k > 1 means a single missed sync on
    line 1 no longer drags the anchor down to line 2 (v0.6.14 audit D-H1).

    ``line_slot`` is the anchor's line number. Without a hint it is 0 —
    the old assumption that the anchor *is* line 0. With
    ``first_slot_hint`` (from ``line0_sync_hint``) the anchor must sit on
    the hinted lattice — ``hint + k·P`` for some k ≥ 0 — and its slot is
    that k, so a missed line-0 sync is extrapolated back to the right row
    (and Robot 36 keeps its even=Cr / odd=Cb parity). Candidates off the
    lattice (VIS stop-bit residue, MMSSTV's Scottie "starting sync") are
    skipped exactly as an outgoing-gap mismatch would skip them.

    Falls back to the first on-lattice candidate; then, if nothing is on
    the lattice at all, to the no-hint choice (first well-spaced pair at
    slot 0) — the hint is built on the *nominal* period, so after a long
    run of missed syncs under heavy TX/RX clock drift every candidate can
    sit off it, and the old pair-anchored grid beats calling an arbitrary
    candidate line 0.  Finally candidate 0, so a lone candidate still
    yields a grid.
    """
    p = line_period_samples

    def gap_ok(i: int) -> bool:
        d = candidates[i + 1] - candidates[i]
        k = int(round(d / p))
        return 1 <= k <= _MAX_ANCHOR_GAP_LINES and abs(d - k * p) <= tolerance

    def slot_of(c: int) -> int | None:
        if first_slot_hint is None:
            return 0
        k = int(round((c - first_slot_hint) / p))
        if k < 0 or abs(c - first_slot_hint - k * p) > tolerance:
            return None
        return k

    for i in range(len(candidates) - 1):
        if gap_ok(i):
            slot = slot_of(candidates[i])
            if slot is not None:
                return i, slot
    if first_slot_hint is not None:
        for i, c in enumerate(candidates):
            slot = slot_of(c)
            if slot is not None:
                return i, slot
        for i in range(len(candidates) - 1):
            if gap_ok(i):
                return i, 0
    return 0, 0


def walk_sync_grid(
    candidates: list[int],
    line_period_samples: float,
    max_lines: int,
    *,
    first_slot_hint: float | None = None,
) -> list[int]:
    """Walk raw sync candidates into an evenly-spaced grid.

    Anchors the grid via ``select_anchor`` (first candidate whose outgoing
    spacing is a whole number of line periods within ±25 %, on the
    ``first_slot_hint`` lattice when one is given) and walks forward.
    Candidates before the anchor are discarded as spurious — this is how
    we skip things like VIS stop-bit residue that ``find_sync_candidates``
    intentionally lets through.

    If the anchor sits on line slot k > 0 (its line-0 / line-1 syncs were
    missed — v0.6.14 audit D-H1) the first k slots are extrapolated back
    from it one period at a time, so the image starts on the right row.

    Gaps (missing or out-of-tolerance candidates) are filled with the
    predicted index so downstream decoders can still slice a plausible
    scan line there.

    Returns up to ``max_lines`` grid indices, or an empty list if
    ``candidates`` is empty.
    """
    if not candidates or line_period_samples <= 0 or max_lines <= 0:
        return []

    tolerance = line_period_samples * _LINE_SPACING_TOLERANCE
    anchor_idx, anchor_slot = select_anchor(
        candidates, line_period_samples, tolerance, first_slot_hint
    )
    anchor = candidates[anchor_idx]

    line_starts: list[int] = [
        int(round(anchor - (anchor_slot - j) * line_period_samples))
        for j in range(min(anchor_slot, max_lines))
    ]
    if len(line_starts) >= max_lines:
        return line_starts
    line_starts.append(anchor)
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


# === Internal helpers ===


def _median_smooth(x: NDArray, n: int) -> NDArray:
    """Centered 1-D median filter. Edge-replicated so the output indexes
    1:1 with the input.

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


__all__ = [
    "find_sync_candidates",
    "first_sync_offset_samples",
    "line0_sync_hint",
    "select_anchor",
    "walk_sync_grid",
]
