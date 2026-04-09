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

The detector treats the frequency track in two passes:

1. A 2 ms (leader) / 1 ms (line) boxcar smooth flattens single-sample
   noise without merging the 10 ms mid-leader break into the 30 ms VIS
   start bit. The line-pass smoother is narrower because line sync pulses
   can be as short as ~5 ms.
2. The smoothed track is thresholded into a "1200 Hz sync band" mask
   (``> 1100 Hz & < 1300 Hz``). Maximal True runs are extracted; runs
   whose length sits within 50–200 % of the expected mode-specific sync
   length are kept as candidate line syncs. Runs much longer than that
   (the 30 ms VIS start/stop bits) and much shorter (transient ringing
   between adjacent VIS data bits) are rejected.

Public API
----------
find_leader(freq_track, fs) -> int | None
    Sample index of the VIS start bit's leading edge, or ``None`` if no
    leader was found.

find_sync_candidates(freq_track, fs, sync_pulse_ms, start_idx=0) -> list[int]
    All plausible per-line sync pulse start indices (length-filtered but
    **not** spacing-filtered). Used by the decoder to detect whether a
    Robot 36 WAV uses PySSTV's 150 ms per-line layout or the canonical
    broadcast 290 ms line-pair layout before committing to a grid walk.

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

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from sstv_app.core.modes import ModeSpec


# === Internal constants ===

#: Boxcar smoothing window for the leader/VIS-start search. 2 ms is wide
#: enough to suppress sample-level jitter and narrow enough to keep the
#: 10 ms mid-leader break distinct from the 30 ms VIS start bit.
_LEADER_SMOOTH_S: float = 0.002

#: Boxcar smoothing window for per-line sync detection. Narrower than the
#: leader smoother because line syncs can be as short as ~5 ms (Martin M1
#: is 4.862 ms) and an over-wide boxcar would smear them.
_LINE_SMOOTH_S: float = 0.001

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
    start_idx: int = 0,
) -> list[int]:
    """Length-filtered sync pulse candidates, with no spacing filter.

    Returns the sample indices (into the original ``freq_track``) of
    every 1200 Hz run whose duration falls within 50–200 % of the given
    ``sync_pulse_ms``. The caller typically feeds this into
    ``walk_sync_grid`` after deciding on the expected line period — or
    uses the raw list to auto-detect the period itself.

    Indices are compensated for the boxcar smoother's leading-edge skew
    (half the smoother window), matching ``find_line_starts``.

    This function is intentionally permissive — it can yield spurious
    candidates such as VIS stop-bit residue (a 1200 Hz run that started
    before ``start_idx`` and leaked past it, then got chopped to
    length-valid by the ``arr[start_idx:]`` slice). ``walk_sync_grid``
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

    smooth_n = max(1, int(round(_LINE_SMOOTH_S * fs)))
    sliced = arr[start_idx:]
    smooth = _boxcar(sliced, smooth_n)

    sync_mask = (smooth > 1100.0) & (smooth < 1300.0)
    runs = _find_runs(sync_mask)

    return [
        start_idx + max(0, run_start - smooth_n // 2)
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
    candidates = find_sync_candidates(
        freq_track, fs, spec.sync_pulse_ms, start_idx=start_idx
    )
    line_samples = spec.line_time_ms / 1000.0 * fs
    return walk_sync_grid(candidates, line_samples, spec.height)


# === Internal helpers ===


def _boxcar(x: NDArray, n: int) -> NDArray:
    """Centered boxcar smooth. ``mode='same'`` so the output indexes 1:1
    with the input — important for keeping detected positions accurate."""
    if n <= 1:
        return np.asarray(x, dtype=np.float64)
    kernel = np.ones(n, dtype=np.float64) / n
    return np.convolve(x, kernel, mode="same")


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
