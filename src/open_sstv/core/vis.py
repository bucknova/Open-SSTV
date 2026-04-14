# SPDX-License-Identifier: GPL-3.0-or-later
"""VIS (Vertical Interval Signaling) header detection.

Every SSTV transmission begins with an 8-bit VIS code that announces the
mode. The header is a strict, FSK-shaped pattern (verified against PySSTV's
``sstv.py``):

    1. Leader      300 ms @ 1900 Hz
    2. Break       10  ms @ 1200 Hz
    3. Leader      300 ms @ 1900 Hz
    4. Start bit   30  ms @ 1200 Hz
    5. Data bit 0  30  ms @ 1100 Hz (logical 1) or 1300 Hz (logical 0)
       …
       Data bit 6  30  ms
    6. Parity      30  ms — set so the total number of 1s (data + parity)
                            is even
    7. Stop bit    30  ms @ 1200 Hz

Bits are transmitted **LSB first**: the first 30 ms data tone after the
start bit is bit 0 of the VIS byte, the next is bit 1, and so on.

Algorithm
---------

1. Compute the per-sample instantaneous frequency via ``demod.instantaneous_frequency``.
2. Smooth with a short (~2 ms) boxcar to suppress sample-level jitter — narrow
   enough that the 10 ms mid-leader break is still distinguishable from the
   30 ms start bit.
3. Find runs of "sync-band" frequencies (1100–1300 Hz wide enough to include
   real-world drift). For each run long enough to be a 30 ms start bit
   (≥20 ms — the 10 ms mid-leader break is intentionally rejected), try to
   decode VIS bits at fixed 30 ms offsets from the run start.
4. Sample the median frequency in the central 60% of each bit window (to dodge
   filter ringing at bit edges), classify as 1 (1100 Hz) or 0 (1300 Hz),
   verify the parity and stop bits, and reconstruct the VIS code LSB-first.

Returns ``None`` for any failure (no header found, parity mismatch, missing
stop bit, etc.) so callers can fall through to "still searching" without
exception handling.

Public API
----------
detect_vis(samples, fs) -> tuple[vis_code: int, end_sample_index: int] | None
    ``end_sample_index`` is the sample one past the stop bit — the first
    sample of image data (or the optional FSKID payload, which the decoder
    skips).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from open_sstv.core.demod import instantaneous_frequency

if TYPE_CHECKING:
    from numpy.typing import NDArray


# === VIS protocol constants (cross-checked against PySSTV ``sstv.py``) ===

#: Logical "1" data tone in Hz.
VIS_BIT1_HZ: float = 1100.0

#: Logical "0" data tone in Hz.
VIS_BIT0_HZ: float = 1300.0

#: Sync / start / stop tone in Hz.
VIS_SYNC_HZ: float = 1200.0

#: Leader tone in Hz.
VIS_LEADER_HZ: float = 1900.0

#: Each data, parity, and stop bit is 30 ms long.
VIS_BIT_DURATION_S: float = 0.030

#: Each leader half is 300 ms long; the mid-leader break is 10 ms.
VIS_LEADER_DURATION_S: float = 0.300
VIS_BREAK_DURATION_S: float = 0.010

#: Number of data bits in the VIS code (the parity bit is the 8th bit).
VIS_DATA_BITS: int = 7


def detect_vis(
    samples: "NDArray", fs: int, *, weak_signal: bool = False
) -> tuple[int, int] | None:
    """Scan an audio buffer for a VIS header.

    Parameters
    ----------
    samples:
        1-D float audio buffer in any range (the algorithm uses
        instantaneous frequency, which is amplitude-invariant).
    fs:
        Sample rate of ``samples`` in Hz.
    weak_signal:
        When ``True``, relaxes the leader-presence threshold
        (0.40 → 0.25) and the minimum start-bit duration
        (20 ms → 15 ms) to improve detection of signals buried in
        noise or fading conditions.  More permissive thresholds
        increase the false-positive rate; D-1 already handles unknown
        VIS codes gracefully so the cost is one spurious IDLE reset,
        not an error surfaced to the user.

    Returns
    -------
    tuple[int, int] | None
        ``(vis_code, end_index)`` on a successful decode, where
        ``end_index`` is the sample index one past the stop bit (the first
        sample of image data). Returns ``None`` if no valid VIS header
        was found.
    """
    arr = np.asarray(samples)
    if arr.ndim != 1 or arr.size == 0:
        return None

    # Per-sample IF, then a 2 ms boxcar smooth to suppress sample-level
    # jitter. The boxcar is intentionally narrow (5x narrower than the
    # 10 ms mid-leader break) so the break stays distinguishable from
    # the 30 ms start bit.
    inst = instantaneous_frequency(arr, fs)
    smooth_n = max(1, int(round(0.002 * fs)))
    if smooth_n > 1:
        kernel = np.ones(smooth_n, dtype=np.float64) / smooth_n
        smooth = np.convolve(inst, kernel, mode="same")
    else:
        smooth = inst

    bit_samples = int(round(VIS_BIT_DURATION_S * fs))
    # The mid-leader break is 10 ms; require start-bit candidates to be at
    # least 20 ms long so we never mistake the break for the start bit.
    # In weak-signal mode we relax this to 15 ms — a fading signal may not
    # produce a full-duration run, but 15 ms is still safely longer than
    # the 10 ms break.
    min_start_bit_samples = int(round((0.015 if weak_signal else 0.020) * fs))

    sync_mask = (smooth > 1100.0) & (smooth < 1300.0)
    runs = _find_runs(sync_mask)

    # Merge runs separated by gaps shorter than 5 ms. At lower signal
    # levels, brief noise spikes in the FM demod can split a 30 ms
    # start bit into two halves that each fall below the minimum
    # duration threshold. 5 ms is safely below the 10 ms mid-leader
    # break (which is a standalone sync-band *run* at 1200 Hz, not a
    # gap) and far below the 300 ms leader sections that bracket it.
    merge_gap = max(1, int(round(0.005 * fs)))
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    runs = merged

    # Minimum number of samples of 1900 Hz leader we expect before the
    # start bit. We check ~200 ms of the ~300 ms second leader half;
    # being lenient avoids rejecting real signals where the first
    # leader half was clipped by the recording start.
    leader_check_samples = int(round(0.200 * fs))
    # Require at least 40 % of those samples to be in the leader band
    # (1800–2000 Hz). This threshold is low enough to tolerate noisy
    # acoustic audio yet high enough to reject random noise.
    # In weak-signal mode the threshold is dropped to 25 % so faint or
    # intermittently fading leader tones can still pass validation.
    leader_frac_threshold = 0.25 if weak_signal else 0.40

    for run_start, run_end in runs:
        if (run_end - run_start) < min_start_bit_samples:
            continue

        # Leader validation: reject sync-band runs that aren't preceded
        # by the 1900 Hz leader tone. Without this, noise bursts with
        # valid-parity bit patterns (especially 0x00 — all zeros, even
        # parity) create false VIS detections.
        leader_end = max(0, run_start - int(round(0.005 * fs)))  # skip transition
        leader_start = max(0, leader_end - leader_check_samples)
        if leader_end > leader_start:
            leader_region = smooth[leader_start:leader_end]
            leader_in_band = float(np.mean(
                (leader_region > 1800.0) & (leader_region < 2000.0)
            ))
            if leader_in_band < leader_frac_threshold:
                continue

        # The first data bit follows the start bit by exactly bit_samples.
        # Aligning to ``run_start`` rather than ``run_end`` is more robust
        # to filter ringing at the trailing edge of the start bit.
        #
        # Subtract half the smoothing window: the leading 1900→1200
        # transition becomes a linear ramp under the boxcar smoother, and
        # the (>1100 & <1300) threshold crosses partway *into* that ramp,
        # so the detected run_start is ~smooth_n/2 samples *late* relative
        # to the true start of the start bit. Compensating here keeps the
        # subsequent bit-window sampling centered on each data bit.
        first_data_bit = run_start + bit_samples - smooth_n // 2
        if first_data_bit < 0:
            continue
        last_chunk_end = first_data_bit + (VIS_DATA_BITS + 2) * bit_samples
        if last_chunk_end > len(smooth):
            continue

        # Sample each 30 ms window's central 60% to dodge bit-edge ringing.
        # Uses the 30th percentile rather than the median: for clean signals
        # the two are identical (low intra-window variance), but for noisy
        # over-the-air / acoustic coupling the 30th percentile correctly
        # recovers "1" bits (1100 Hz) that the median would miss when
        # brief 1100 Hz dips are surrounded by smeared 1300 Hz energy.
        margin = bit_samples // 5
        chunk_freqs: list[float] = []
        for i in range(VIS_DATA_BITS + 2):
            chunk_start = first_data_bit + i * bit_samples
            chunk_end = chunk_start + bit_samples
            mid = smooth[chunk_start + margin : chunk_end - margin]
            if mid.size == 0:
                mid = smooth[chunk_start:chunk_end]
            chunk_freqs.append(float(np.percentile(mid, 30)))

        data_freqs = chunk_freqs[:VIS_DATA_BITS]
        parity_freq = chunk_freqs[VIS_DATA_BITS]
        stop_freq = chunk_freqs[VIS_DATA_BITS + 1]

        # 1100 Hz = 1, 1300 Hz = 0 (per PySSTV).
        data_bits = [1 if f < VIS_SYNC_HZ else 0 for f in data_freqs]
        parity_bit = 1 if parity_freq < VIS_SYNC_HZ else 0

        # Stop bit must be a sync-band tone (1200 Hz).
        if not (1100.0 < stop_freq < 1300.0):
            continue

        # Even parity: total number of 1s (data + parity) must be even.
        if (sum(data_bits) + parity_bit) % 2 != 0:
            continue

        # Reconstruct the VIS byte LSB-first.
        code = 0
        for i, bit in enumerate(data_bits):
            code |= bit << i

        return (code, last_chunk_end)

    return None


def _find_runs(mask: "NDArray[np.bool_]") -> list[tuple[int, int]]:
    """Return ``[(start, end), …]`` for each maximal True run in ``mask``.

    ``end`` is exclusive (slice-friendly). Empty input returns an empty
    list. Used by ``detect_vis`` to find candidate start-bit regions.
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
    "VIS_BIT0_HZ",
    "VIS_BIT1_HZ",
    "VIS_BIT_DURATION_S",
    "VIS_BREAK_DURATION_S",
    "VIS_DATA_BITS",
    "VIS_LEADER_DURATION_S",
    "VIS_LEADER_HZ",
    "VIS_SYNC_HZ",
    "detect_vis",
]
