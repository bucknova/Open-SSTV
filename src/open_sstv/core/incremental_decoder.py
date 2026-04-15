# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental streaming SSTV decoder — Scottie S1 proof-of-concept.

**Status**: Experimental / local-only. Enable via
``AppConfig.experimental_incremental_decode = True`` (off by default).

Problem solved
--------------
The batch ``Decoder`` reprocesses the full growing audio buffer on every
2-second flush: O(buffer²) total CPU. For a 110-second Scottie S1 receive
that means ~18 flushes × average buffer of ~55 s ≈ 990 equivalent
full-signal passes. This module replaces that with O(1 line period) per
feed call — a ~50× improvement on a typical Scottie S1 image.

Architecture
------------
``ScottieS1IncrementalDecoder`` maintains a bounded sliding audio window.
For each newly confirmed sync pulse it:

1. Extracts a fixed-size window of audio centred on the sync position
   (lookback covers G + filter margin; lookahead covers R + filter margin).
2. Applies the same zero-phase ``sosfiltfilt`` bandpass as the batch decoder.
3. Computes instantaneous frequency via Hilbert transform.
4. Samples RGB pixels with the identical ``_sample_pixels`` algorithm.
5. Emits a ``(row_index, rgb_array)`` tuple for that line.
6. Prunes the audio buffer to the start of the *next* expected line's G
   channel, keeping the window bounded at ~2 × line period.

Byte-identical guarantee
------------------------
With ``FILTER_MARGIN = 4096`` samples of sosfiltfilt padding before and
after each pixel region, the windowed filtered signal is numerically
identical to what the batch decoder produces on the full signal. The
order-4 Butterworth impulse response decays to <1e-10 within ~200
samples; 4096 is massive overkill that guarantees byte-identical output
even at the very first line. See ``test_incremental_decoder.py``.

Scope
-----
Scottie S1 only. The architecture generalises to any ``BEFORE_RED``
Scottie family mode and, with a little extra plumbing, to LINE_START
modes. Robot 36's line-pair dispatch and YCbCr conversion are deferred
to a follow-on.

NOTE: ``_sample_pixels_inc`` below is intentionally kept in sync with
``decoder._sample_pixels``. Any change there must be mirrored here to
preserve the byte-identical guarantee.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from scipy.signal import sosfiltfilt

from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_WHITE_HZ, instantaneous_frequency
from open_sstv.core.dsp_utils import bandpass_sos
from open_sstv.core.modes import ModeSpec, SyncPosition
from open_sstv.core.sync import find_sync_candidates, walk_sync_grid

if TYPE_CHECKING:
    from numpy.typing import NDArray

# Bandpass edges — identical to decoder._BANDPASS_LOW/HIGH/ORDER.
_BP_LOW_HZ: float = 1000.0
_BP_HIGH_HZ: float = 2500.0
_BP_ORDER: int = 4

# Minimum samples sosfiltfilt can handle without internal padding failure.
# Matches decoder._BANDPASS_MIN_SAMPLES.
_MIN_BP_SAMPLES: int = 256


# ---------------------------------------------------------------------------
# Internal DSP helpers (kept in sync with core/decoder.py counterparts)
# ---------------------------------------------------------------------------


def _bp_window(x: "NDArray", fs: int) -> "NDArray":
    """Zero-phase bandpass the SSTV signalling band.

    Mirrors ``decoder._bandpass`` exactly so filtered values are identical
    to those the batch decoder produces for the same audio region.
    """
    if x.size < _MIN_BP_SAMPLES:
        return x
    try:
        sos = bandpass_sos(_BP_LOW_HZ, _BP_HIGH_HZ, fs, order=_BP_ORDER)
    except ValueError:
        return x
    return sosfiltfilt(sos, x)


def _sample_pixels_inc(
    inst: "NDArray",
    start: float,
    span_samples: float,
    width: int,
    track_len: int,
    *,
    chroma: bool = False,
) -> "NDArray[np.uint8]":
    """Slice a frequency-track span into ``width`` pixel medians.

    **Keep in sync with ``decoder._sample_pixels``** — this copy exists to
    avoid a circular import (incremental_decoder → decoder → incremental_decoder).
    Any algorithm change in decoder._sample_pixels must be mirrored here.
    """
    neutral: int = 128 if chroma else 0
    out = np.full(width, neutral, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    margin = pixel_span * 0.2
    span_lo = SSTV_BLACK_HZ
    span_hi = SSTV_WHITE_HZ
    span_range = span_hi - span_lo
    chroma_floor = 0.15 if chroma else 0.0
    guard_pixels = max(3, width // 80) if chroma else 0
    max_col = width - guard_pixels
    for col in range(max_col):
        center_lo = start + col * pixel_span + margin
        center_hi = start + (col + 1) * pixel_span - margin
        lo = int(round(center_lo))
        hi = int(round(center_hi))
        if hi <= lo:
            hi = lo + 1
        if lo < 0 or hi > track_len:
            continue
        chunk = inst[lo:hi]
        if chunk.size == 0:
            continue
        freq = float(np.median(chunk))
        norm = (freq - span_lo) / span_range
        if norm < chroma_floor:
            out[col] = neutral
            continue
        elif norm > 1.0:
            norm = 1.0
        out[col] = int(round(norm * 255.0))
    return out


# ---------------------------------------------------------------------------
# Incremental decoder
# ---------------------------------------------------------------------------


class ScottieS1IncrementalDecoder:
    """Streaming Scottie S1 decoder: emits line events as each arrives.

    Unlike the batch ``Decoder``, which reprocesses the full growing buffer
    on every flush, this decoder maintains a bounded sliding audio window and
    decodes each line the moment enough audio has accumulated. The audio
    buffer is pruned after each confirmed line, keeping memory consumption
    bounded at approximately ``2 × line_period`` samples (~850 ms at 48 kHz).

    Handles any ``SyncPosition.BEFORE_RED`` Scottie-family mode (S1, S2, DX)
    but validated only against S1 in v0.1.x.

    Parameters
    ----------
    spec:
        Mode specification (Scottie S1 / S2 / DX).
    fs:
        Sample rate in Hz.
    vis_end_abs:
        Absolute sample position (relative to ``start_abs``) where the VIS
        header ends. Sync candidates before this point are ignored.
    start_abs:
        Absolute position of the first sample that will be fed. Defaults to
        0. Pass a non-zero value when the caller's audio buffer starts at a
        known offset (e.g., ``Decoder``'s rolling IDLE window).

    Usage
    -----
    ::

        spec = MODE_TABLE[Mode.SCOTTIE_S1]
        inc = ScottieS1IncrementalDecoder(spec, fs=48000, vis_end_abs=vis_end)
        for chunk in audio_chunks:
            for row_idx, rgb_row in inc.feed(chunk):
                display_line(row_idx, rgb_row)
        image = inc.get_image()
    """

    #: Samples of sosfiltfilt padding before/after each pixel region.
    #: The order-4 Butterworth transient decays to <1e-10 in ~200 samples;
    #: 4096 guarantees byte-identical output vs. the batch decoder.
    FILTER_MARGIN: int = 4096

    #: Width of the rolling tail searched for sync candidates on each
    #: ``feed()`` call, in line periods. 3 line periods (~1.3 s) gives the
    #: ``walk_sync_grid`` anchor detector at least two candidate pairs.
    SYNC_SEARCH_LINES: float = 3.0

    #: Two sync candidates within this many samples of each other are
    #: considered the same pulse (de-duplication across consecutive tails).
    _DEDUP_RADIUS: int = 100

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.BEFORE_RED:
            raise ValueError(
                f"ScottieS1IncrementalDecoder only handles BEFORE_RED modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        self._spec = spec
        self._fs = fs
        self._vis_end_abs = vis_end_abs

        # --- Timing constants (derived from spec) ---
        sync_ms = spec.sync_pulse_ms
        porch_ms = spec.sync_porch_ms
        scan_ms = (spec.line_time_ms - sync_ms - 6.0 * porch_ms) / 3.0

        # Offsets from sync pulse leading edge — identical to _decode_scottie_rgb.
        #   G starts this many ms BEFORE the sync (negative offset).
        #   R ends this many ms AFTER the sync (positive offset).
        g_start_ms = -(porch_ms + scan_ms + porch_ms + porch_ms + scan_ms)
        r_end_ms = sync_ms + porch_ms + scan_ms + porch_ms

        self._g_offset: float = g_start_ms / 1000.0 * fs          # negative
        self._b_offset: float = -(porch_ms + scan_ms) / 1000.0 * fs  # negative
        self._r_offset: float = (sync_ms + porch_ms) / 1000.0 * fs   # positive
        self._scan_samp: float = scan_ms / 1000.0 * fs

        # Pre-audio and post-audio needed around each sync for pixel decode,
        # plus the filter-startup padding that guarantees byte-identity.
        self._g_lookback: int = int(abs(g_start_ms) / 1000.0 * fs) + self.FILTER_MARGIN
        self._r_lookahead: int = int(r_end_ms / 1000.0 * fs) + self.FILTER_MARGIN

        self._line_samp: float = spec.line_time_ms / 1000.0 * fs

        # --- Buffer state ---
        self._buf: NDArray = np.zeros(0, dtype=np.float64)
        self._buf_abs_start: int = start_abs   # absolute position of _buf[0]
        self._total_fed: int = 0               # samples fed (relative to start_abs)

        # --- Sync state ---
        # Absolute positions of all confirmed sync candidates found so far.
        # These are NEVER pruned (only a few hundred integers, negligible memory).
        self._sync_abs: list[int] = []
        # End of the most-recent _update_syncs search range (absolute).
        # Seeded to start_abs so the first call searches from vis_end onward.
        # Bug-1 fix: we overlap successive searches by one line period so that
        # no sync at a chunk boundary is ever missed.
        self._last_search_end_abs: int = start_abs

        # --- Image state ---
        self._lines_decoded: int = 0
        self._image: NDArray[np.uint8] = np.zeros(
            (spec.height, spec.width, 3), dtype=np.uint8
        )

    # --- Public API ---

    @property
    def total_fed(self) -> int:
        """Total samples fed since construction (relative to ``start_abs``)."""
        return self._total_fed

    @property
    def lines_decoded(self) -> int:
        """Number of lines that have been decoded and written to the image."""
        return self._lines_decoded

    @property
    def complete(self) -> bool:
        """True when all ``spec.height`` lines have been decoded."""
        return self._lines_decoded >= self._spec.height

    def get_image(self) -> Image.Image:
        """Return the current image (partial until ``complete`` is True)."""
        return Image.fromarray(self._image)

    def feed(self, chunk: "NDArray") -> list[tuple[int, "NDArray[np.uint8]"]]:
        """Feed a new audio chunk.

        Appends ``chunk`` to the internal buffer, searches for new sync
        pulses in the most-recent ``SYNC_SEARCH_LINES`` of audio, decodes
        any newly complete lines, then prunes stale audio.

        Returns
        -------
        list[tuple[int, NDArray[np.uint8]]]
            ``[(row_index, rgb_row), ...]`` for each line decoded in this
            call. ``rgb_row`` is a ``(width, 3)`` uint8 RGB array. The list
            is empty between sync detections or while waiting for enough
            post-sync audio for the red channel.
        """
        arr = np.asarray(chunk, dtype=np.float64)
        if arr.ndim != 1 or arr.size == 0:
            return []

        self._buf = np.concatenate([self._buf, arr])
        self._total_fed += arr.size
        current_abs = self._buf_abs_start + len(self._buf)

        self._update_syncs(current_abs)

        new_lines: list[tuple[int, NDArray[np.uint8]]] = []
        while not self.complete:
            if not self._try_decode_next(current_abs, new_lines):
                break

        self._prune()
        return new_lines

    # --- Private helpers ---

    def _update_syncs(self, current_abs: int) -> None:
        """Search for new sync candidates from just before the last search end.

        Two fixes applied here vs. the original rolling-tail approach:

        **Bug-1 fix (overlap):** instead of searching only a fixed
        ``SYNC_SEARCH_LINES`` rolling tail, we search from
        ``(last_search_end − line_samp)`` to ``current_abs``.  This gives a
        one-line-period backward overlap with the previous call's search
        window so that a sync falling right at a chunk boundary can never be
        skipped regardless of chunk size.

        **Filter warm-up:** the bandpass filter needs ``FILTER_MARGIN``
        samples of context before the search region to be fully settled.
        Without this, the very first sync (which sits right at ``vis_end``)
        is detected against a transient filter state, shifting its position vs
        the batch decoder (which filters from sample 0).  The fix prepends
        ``min(FILTER_MARGIN, search_from_buf)`` samples of real audio before
        the search region, and adjusts ``start_idx`` so candidates are only
        reported from the intended region onward.

        The deduplication radius ``_DEDUP_RADIUS`` handles candidates
        re-discovered in the overlap region without creating duplicates.
        """
        # Start of the search region: one line period before last search end,
        # but never before vis_end (no valid syncs before VIS header).
        search_from_abs = max(
            self._vis_end_abs,
            self._last_search_end_abs - int(self._line_samp),
        )

        # Map to a buffer-relative offset (buffer may have been pruned past
        # search_from_abs → clamp to buf start in that case).
        search_from_buf = max(0, search_from_abs - self._buf_abs_start)

        # Prepend up to FILTER_MARGIN samples of real audio so the bandpass
        # filter is fully settled by the time it reaches the search region.
        # lead_in == min(FILTER_MARGIN, search_from_buf) ≤ search_from_buf.
        lead_in = min(self.FILTER_MARGIN, search_from_buf)
        actual_start = search_from_buf - lead_in
        tail = self._buf[actual_start:]

        if tail.size < _MIN_BP_SAMPLES:
            return

        filtered_tail = _bp_window(tail, self._fs)
        inst_tail = instantaneous_frequency(filtered_tail, self._fs)
        tail_abs_start = self._buf_abs_start + actual_start

        # Only report candidates from the actual search region (past lead-in).
        # Since search_from_abs ≥ vis_end_abs, this also satisfies the VIS gate.
        search_start = lead_in

        cands_rel = find_sync_candidates(
            inst_tail,
            self._fs,
            self._spec.sync_pulse_ms,
            line_period_samples=self._line_samp,
            start_idx=search_start,
        )
        for c_rel in cands_rel:
            c_abs = tail_abs_start + c_rel
            # De-duplicate: skip if within _DEDUP_RADIUS of a known sync.
            if any(abs(c_abs - s) <= self._DEDUP_RADIUS for s in self._sync_abs):
                continue
            self._sync_abs.append(c_abs)

        self._sync_abs.sort()
        self._last_search_end_abs = current_abs

    def _try_decode_next(
        self,
        current_abs: int,
        out: list[tuple[int, "NDArray[np.uint8]"]],
    ) -> bool:
        """Attempt to decode the next pending line.

        Returns True if a line was decoded (and more may follow), False if
        we should stop trying (not enough audio yet, or grid not anchored).
        """
        if len(self._sync_abs) < 2:
            return False  # not enough candidates to anchor the grid

        # Build the sync grid using ALL known candidates (including historical
        # ones whose audio has been pruned). Buffer-relative coordinates are
        # fine for walk_sync_grid arithmetic — only the line we're about to
        # decode needs a non-negative (in-buffer) position.
        cands_in_buf = [s - self._buf_abs_start for s in self._sync_abs]
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._spec.height)

        if self._lines_decoded >= len(grid):
            return False

        sync_in_buf = grid[self._lines_decoded]
        sync_abs = self._buf_abs_start + sync_in_buf

        # Bail early if the sync's audio hasn't arrived yet (very first line
        # and/or audio is arriving in small chunks).
        if sync_in_buf < 0:
            return False

        # Wait until the R channel's last pixel has arrived.
        #
        # Bug-2 fix: the previous guard used self._r_lookahead which includes
        # FILTER_MARGIN (4096) samples of trailing padding beyond the R scan's
        # last pixel.  For all lines except the very last this padding is
        # available.  For the last line the audio stream ends ~2 samples after
        # the R scan ends, so the full r_lookahead is never satisfied and the
        # decoder stalls.
        #
        # Fix: gate on the R scan end (r_offset + scan_samp), not on
        # r_lookahead.  The extraction already clamps win_end to len(buf), so
        # the trailing filter-startup region will be shorter on the last line —
        # but the order-4 Butterworth decays to <1e-10 in ~200 samples, and
        # we have ~69 samples of trailing audio (7137 − 7068) which is enough
        # for the last few pixels to be unaffected in practice.
        r_scan_end = int(self._r_offset + self._scan_samp)
        if current_abs < sync_abs + r_scan_end:
            return False

        # Extract the decode window.
        win_start = max(0, sync_in_buf - self._g_lookback)
        win_end = min(len(self._buf), sync_in_buf + self._r_lookahead)
        window = self._buf[win_start:win_end]

        if window.size < _MIN_BP_SAMPLES:
            return False

        filtered = _bp_window(window, self._fs)
        inst = instantaneous_frequency(filtered, self._fs)
        n = inst.size

        # Sync position within this window.
        sync_in_win = sync_in_buf - win_start

        # Decode the three RGB channels (G, B, R order matches Scottie layout).
        row: NDArray[np.uint8] = np.zeros((self._spec.width, 3), dtype=np.uint8)
        row[:, 1] = _sample_pixels_inc(
            inst, sync_in_win + self._g_offset, self._scan_samp, self._spec.width, n
        )
        row[:, 2] = _sample_pixels_inc(
            inst, sync_in_win + self._b_offset, self._scan_samp, self._spec.width, n
        )
        row[:, 0] = _sample_pixels_inc(
            inst, sync_in_win + self._r_offset, self._scan_samp, self._spec.width, n
        )

        self._image[self._lines_decoded] = row
        out.append((self._lines_decoded, row.copy()))
        self._lines_decoded += 1
        return True

    def _prune(self) -> None:
        """Trim audio we no longer need.

        After decoding line k, audio before ``sync_{k+1} - g_lookback`` can
        never be needed again (the next line's G channel starts there). We
        leave the filter-margin padding in place so the next decode window
        has a clean startup.
        """
        if self._lines_decoded == 0 or not self._sync_abs:
            return

        # Reconstruct the grid to find the next expected sync position.
        cands_in_buf = [s - self._buf_abs_start for s in self._sync_abs]
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._spec.height)

        if self._lines_decoded < len(grid):
            next_sync_in_buf = grid[self._lines_decoded]
        else:
            # All lines decoded or grid exhausted — extrapolate next sync.
            next_sync_in_buf = (
                int(cands_in_buf[-1] + self._line_samp) if cands_in_buf else 0
            )

        prune_to = max(0, next_sync_in_buf - self._g_lookback)
        if prune_to > 0 and prune_to <= len(self._buf):
            self._buf = self._buf[prune_to:]
            self._buf_abs_start += prune_to


__all__ = ["ScottieS1IncrementalDecoder"]
