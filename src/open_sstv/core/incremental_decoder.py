# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental streaming SSTV decoder — covers Scottie, Martin, PD, Wraase, Pasokon.

**Status**: Experimental. Enable via
``AppConfig.experimental_incremental_decode = True`` (off by default).

Problem solved
--------------
The batch ``Decoder`` reprocesses the full growing audio buffer on every
flush: O(buffer²) total CPU.  For a 110-second Scottie S1 receive that
means ~18 flushes × average buffer of ~55 s ≈ 990 equivalent full-signal
passes.  For Martin M1 (~114 s) the same pattern meant the batch path was
falling behind real-time partway through the image on laptop-class
hardware.  This module replaces that with O(1 line period) per feed call —
a ~50× improvement on long modes and, for Martin M1, the difference
between "fine" and "slideshow".

Architecture
------------
``IncrementalDecoderBase`` owns the common machinery: a bounded sliding
audio window, sync-candidate harvesting with ``walk_sync_grid``, per-line
window extraction with ``FILTER_MARGIN`` samples of sosfiltfilt padding,
and audio pruning down to the next expected line's leading edge.

Five concrete subclasses cover every pixel-RGB mode we ship except
Robot 36:

* ``ScottieIncrementalDecoder`` — ``BEFORE_RED`` Scottie family
  (S1 / S2 / DX / S3 / S4).  Sync lands mid-line; G and B offsets are
  negative relative to the sync pulse.
* ``MartinIncrementalDecoder`` — ``LINE_START`` Martin family
  (M1 / M2 / M3 / M4).  One sync + four porches + three equal GBR scans.
* ``PDIncrementalDecoder`` — ``LINE_START`` PD family
  (PD-50 / 90 / 120 / 160 / 180 / 240 / 290).  One sync + one porch +
  four equal scans (Y0 / Cr / Cb / Y1); each sync produces **two**
  output image rows.  YCbCr→RGB is done per-pair to match PIL's
  ``Image.frombytes("YCbCr", ...).convert("RGB")`` path exactly.
* ``WraaseIncrementalDecoder`` — ``LINE_START`` Wraase SC2 family
  (SC2-120 / SC2-180).  One sync + one porch + three back-to-back RGB
  scans (no inter-channel gaps).
* ``PasokonIncrementalDecoder`` — ``LINE_START`` Pasokon family
  (P3 / P5 / P7).  One sync + four equal gaps + three RGB scans.

Robot 36's line-pair dispatch and alternating chroma don't fit cleanly
into the per-sync pattern and remain on the batch decoder for now.  It
is a short mode (~36 s) and the batch decoder keeps up with it even on
modest hardware, so this is a pragmatic trade-off rather than a bug.

Byte-identical guarantee
------------------------
With ``FILTER_MARGIN = 4096`` samples of sosfiltfilt padding before and
after each pixel region, the windowed filtered signal is numerically
identical to what the batch decoder produces on the full signal.  The
order-4 Butterworth impulse response decays to <1e-10 within ~200
samples; 4096 is massive overkill that guarantees byte-identical output
even at the very first line.  See ``test_incremental_decoder.py``.

NOTE: ``_sample_pixels_inc`` below is intentionally kept in sync with
``decoder._sample_pixels``.  Any change there must be mirrored here to
preserve the byte-identical guarantee.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from scipy.signal import sosfiltfilt

from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_WHITE_HZ, instantaneous_frequency
from open_sstv.core.dsp_utils import bandpass_sos
from open_sstv.core.modes import Mode, ModeSpec, SyncPosition
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
# Base class
# ---------------------------------------------------------------------------


class IncrementalDecoderBase:
    """Shared buffer / sync / prune machinery for per-line SSTV decoding.

    Subclasses fill in the geometry (offsets from the sync pulse, how many
    image rows each sync yields) via a small set of hooks.  The base owns:

    * The rolling audio buffer and its absolute-position bookkeeping.
    * Sync-candidate harvesting (with filter-warm-up lead-in and cross-chunk
      de-duplication).
    * Calling ``walk_sync_grid`` to produce stable line positions.
    * Waiting for enough post-sync audio to arrive before attempting decode.
    * Extracting the bandpass-filtered Hilbert envelope for each line
      window.
    * Pruning the buffer once a line is confirmed decoded.

    Subclasses implement:

    * ``_compute_timing(spec, fs)`` — populate any instance-level offsets.
    * ``_g_lookback`` / ``_r_lookahead`` — samples of audio needed before
      and after the sync to cover the window (plus ``FILTER_MARGIN`` each
      side).  The names are historical (Scottie-centric); they mean "how
      far behind" and "how far ahead" of the sync we reach.
    * ``_decode_window(inst, sync_in_win, n, grid_index)`` — perform the
      per-line pixel sampling and return a list of ``(row_idx, rgb_row)``
      tuples.  PD returns two rows per call; Scottie / Martin return one.
    * ``_rows_per_sync`` — 1 for Scottie / Martin, 2 for PD.
    * ``_ready_post_sync`` — minimum post-sync offset (in samples) that
      must be present in the buffer before a line can be decoded.  Usually
      the end of the last channel's scan.
    """

    #: Samples of sosfiltfilt padding before/after each pixel region.
    #: The order-4 Butterworth transient decays to <1e-10 in ~200 samples;
    #: 4096 guarantees byte-identical output vs. the batch decoder.
    FILTER_MARGIN: int = 4096

    #: Width of the rolling tail searched for sync candidates on each
    #: ``feed()`` call, in line periods.
    SYNC_SEARCH_LINES: float = 3.0

    #: Two sync candidates within this many samples of each other are
    #: considered the same pulse (de-duplication across consecutive tails).
    _DEDUP_RADIUS: int = 100

    #: Tolerance (samples) applied when gating the last line's readiness.
    #: Floating-point line-period rounding can leave the encoded audio
    #: a few samples short of the full ``_ready_post_sync`` budget; a 128-
    #: sample slack (≈2.7 ms at 48 kHz, well under 1 pixel of any mode)
    #: absorbs that without risking wrong-data decodes on in-progress
    #: lines (they're gated by the *next* sync arriving, not by this
    #: threshold).
    _READY_SLACK_SAMPLES: int = 128

    #: Number of image rows produced per confirmed sync pulse. Overridden
    #: to 2 by PD (which uses line-pair super-lines).
    _rows_per_sync: int = 1

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        self._spec = spec
        self._fs = fs
        self._vis_end_abs = vis_end_abs

        # Hook for subclass geometry (offsets, channel scan length, etc.).
        self._compute_timing(spec, fs)

        self._line_samp: float = spec.line_time_ms / 1000.0 * fs

        # --- Buffer state ---
        self._buf: NDArray = np.zeros(0, dtype=np.float64)
        self._buf_abs_start: int = start_abs
        self._total_fed: int = 0

        # --- Sync state ---
        self._sync_abs: list[int] = []
        self._last_search_end_abs: int = start_abs

        # --- Image state ---
        # _syncs_consumed counts grid positions turned into image rows;
        # it never exceeds spec.height (the grid length).
        self._syncs_consumed: int = 0
        image_rows = spec.height * self._rows_per_sync
        self._image: NDArray[np.uint8] = np.zeros(
            (image_rows, spec.width, 3), dtype=np.uint8
        )

    # --- Subclass hooks ---

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        """Compute per-channel offsets and window bounds from ``spec``.

        Must set ``self._g_lookback`` and ``self._r_lookahead`` (samples
        of audio needed before / after the sync pulse, *including* the
        ``FILTER_MARGIN`` padding on each side), and ``self._ready_post_sync``
        (the minimum post-sync offset at which a decode attempt makes
        sense — typically the end of the last channel's scan).

        Any additional instance attributes needed by ``_decode_window``
        should be populated here too.
        """
        raise NotImplementedError

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        """Sample pixels from the bandpass-filtered Hilbert track.

        ``grid_index`` is the zero-based index of the sync pulse within the
        grid produced by ``walk_sync_grid``.  The subclass is responsible
        for writing to ``self._image`` and returning the
        ``(row_idx, rgb_row)`` tuples for the UI.
        """
        raise NotImplementedError

    # --- Public API ---

    @property
    def total_fed(self) -> int:
        """Total samples fed since construction (relative to ``start_abs``)."""
        return self._total_fed

    @property
    def lines_decoded(self) -> int:
        """Number of image rows painted so far."""
        return self._syncs_consumed * self._rows_per_sync

    @property
    def image_height(self) -> int:
        """Total rows in the output image (``spec.height × _rows_per_sync``)."""
        return self._spec.height * self._rows_per_sync

    @property
    def complete(self) -> bool:
        """True when all image rows have been decoded."""
        return self._syncs_consumed >= self._spec.height

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
            call.  PD emits two entries per completed sync; Scottie and
            Martin emit one.  The list is empty between sync detections or
            while waiting for enough post-sync audio.
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

    # --- Private helpers (shared across all concrete decoders) ---

    def _update_syncs(self, current_abs: int) -> None:
        """Search for new sync candidates from just before the last search end.

        Two fixes baked in here vs. a naive rolling-tail approach:

        **Overlap:** search from ``(last_search_end − line_samp)`` to
        ``current_abs`` so a sync at a chunk boundary is never skipped.

        **Filter warm-up:** prepend up to ``FILTER_MARGIN`` samples of
        real audio before the search region so the bandpass filter is
        fully settled by the time it reaches the first candidate position.
        Without this, the very first sync (right at ``vis_end``) is
        detected against a transient filter state and its position can
        shift vs. the batch decoder.

        The ``_DEDUP_RADIUS`` check handles candidates re-discovered in
        the overlap region without creating duplicates.
        """
        search_from_abs = max(
            self._vis_end_abs,
            self._last_search_end_abs - int(self._line_samp),
        )

        search_from_buf = max(0, search_from_abs - self._buf_abs_start)

        lead_in = min(self.FILTER_MARGIN, search_from_buf)
        actual_start = search_from_buf - lead_in
        tail = self._buf[actual_start:]

        if tail.size < _MIN_BP_SAMPLES:
            return

        filtered_tail = _bp_window(tail, self._fs)
        inst_tail = instantaneous_frequency(filtered_tail, self._fs)
        tail_abs_start = self._buf_abs_start + actual_start

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
            return False

        cands_in_buf = [s - self._buf_abs_start for s in self._sync_abs]
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._spec.height)

        if self._syncs_consumed >= len(grid):
            return False

        sync_in_buf = grid[self._syncs_consumed]
        sync_abs = self._buf_abs_start + sync_in_buf

        if sync_in_buf < 0:
            return False

        # Wait until the last channel's scan has fully arrived.
        # (See the Scottie docstring for the last-line edge case —
        # FILTER_MARGIN is asymmetric on the last sync but the Butterworth
        # transient is tiny, so the last few pixels remain unaffected.)
        # ``_READY_SLACK_SAMPLES`` absorbs sub-pixel rounding between the
        # encoder's integer sample count and our float line-period math.
        if current_abs < sync_abs + self._ready_post_sync - self._READY_SLACK_SAMPLES:
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

        sync_in_win = sync_in_buf - win_start

        new_rows = self._decode_window(
            inst, sync_in_win, n, self._syncs_consumed
        )
        out.extend(new_rows)
        self._syncs_consumed += 1
        return True

    def _prune(self) -> None:
        """Trim audio we no longer need.

        After decoding a grid position, audio before
        ``next_sync − g_lookback`` can never be needed again.  We leave
        ``FILTER_MARGIN`` padding in place so the next decode window has
        a clean filter startup.
        """
        if self._syncs_consumed == 0 or not self._sync_abs:
            return

        cands_in_buf = [s - self._buf_abs_start for s in self._sync_abs]
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._spec.height)

        if self._syncs_consumed < len(grid):
            next_sync_in_buf = grid[self._syncs_consumed]
        else:
            next_sync_in_buf = (
                int(cands_in_buf[-1] + self._line_samp) if cands_in_buf else 0
            )

        prune_to = max(0, next_sync_in_buf - self._g_lookback)
        if prune_to > 0 and prune_to <= len(self._buf):
            self._buf = self._buf[prune_to:]
            self._buf_abs_start += prune_to


# ---------------------------------------------------------------------------
# Scottie (BEFORE_RED) — sync lands mid-line, between B and R scans
# ---------------------------------------------------------------------------


class ScottieIncrementalDecoder(IncrementalDecoderBase):
    """Streaming decoder for ``SyncPosition.BEFORE_RED`` Scottie modes.

    Covers S1, S2, DX, S3, S4.  The sync pulse sits between the blue and
    red scans of each line, so G and B offsets are *negative* relative to
    the sync leading edge and R is positive.
    """

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.BEFORE_RED:
            raise ValueError(
                f"ScottieIncrementalDecoder only handles BEFORE_RED modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = spec.sync_pulse_ms
        porch_ms = spec.sync_porch_ms
        scan_ms = (spec.line_time_ms - sync_ms - 6.0 * porch_ms) / 3.0

        # G starts this many ms BEFORE the sync (negative offset).
        # R ends this many ms AFTER the sync (positive offset).
        g_start_ms = -(porch_ms + scan_ms + porch_ms + porch_ms + scan_ms)
        r_end_ms = sync_ms + porch_ms + scan_ms + porch_ms

        self._g_offset: float = g_start_ms / 1000.0 * fs          # negative
        self._b_offset: float = -(porch_ms + scan_ms) / 1000.0 * fs  # negative
        self._r_offset: float = (sync_ms + porch_ms) / 1000.0 * fs   # positive
        self._scan_samp: float = scan_ms / 1000.0 * fs

        self._g_lookback: int = int(abs(g_start_ms) / 1000.0 * fs) + self.FILTER_MARGIN
        self._r_lookahead: int = int(r_end_ms / 1000.0 * fs) + self.FILTER_MARGIN

        # Gate on the R scan END (not r_lookahead) so the final line is not
        # starved of the trailing FILTER_MARGIN that the audio stream never
        # supplies.  The Butterworth transient decays to <1e-10 in ~200
        # samples, so the last few pixels are unaffected in practice.
        self._ready_post_sync: int = int(self._r_offset + self._scan_samp)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        row: NDArray[np.uint8] = np.zeros((width, 3), dtype=np.uint8)
        row[:, 1] = _sample_pixels_inc(
            inst, sync_in_win + self._g_offset, self._scan_samp, width, n
        )
        row[:, 2] = _sample_pixels_inc(
            inst, sync_in_win + self._b_offset, self._scan_samp, width, n
        )
        row[:, 0] = _sample_pixels_inc(
            inst, sync_in_win + self._r_offset, self._scan_samp, width, n
        )
        self._image[grid_index] = row
        return [(grid_index, row.copy())]


# Back-compat alias: early internal callers and tests used the S1-specific
# name.  The class now covers the whole Scottie family.
ScottieS1IncrementalDecoder = ScottieIncrementalDecoder


# ---------------------------------------------------------------------------
# Martin (LINE_START) — sync at line start, GBR in order
# ---------------------------------------------------------------------------


class MartinIncrementalDecoder(IncrementalDecoderBase):
    """Streaming decoder for Martin-family modes (M1, M2, M3, M4).

    Layout: sync → porch → G_scan → porch → B_scan → porch → R_scan → porch.
    All channel offsets are positive relative to the sync leading edge.
    """

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.LINE_START:
            raise ValueError(
                f"MartinIncrementalDecoder only handles LINE_START modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = spec.sync_pulse_ms
        porch_ms = spec.sync_porch_ms
        # Mirrors _decode_martin_rgb: 4 porches + 3 equal channel scans.
        scan_ms = (spec.line_time_ms - sync_ms - 4 * porch_ms) / 3

        g_offset_ms = sync_ms + porch_ms
        b_offset_ms = g_offset_ms + scan_ms + porch_ms
        r_offset_ms = b_offset_ms + scan_ms + porch_ms
        r_end_ms = r_offset_ms + scan_ms

        self._g_offset: float = g_offset_ms / 1000.0 * fs
        self._b_offset: float = b_offset_ms / 1000.0 * fs
        self._r_offset: float = r_offset_ms / 1000.0 * fs
        self._scan_samp: float = scan_ms / 1000.0 * fs

        # Sync is at the line start, so lookback only needs filter warm-up.
        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(r_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(r_offset_ms / 1000.0 * fs + self._scan_samp)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        row: NDArray[np.uint8] = np.zeros((width, 3), dtype=np.uint8)
        row[:, 1] = _sample_pixels_inc(
            inst, sync_in_win + self._g_offset, self._scan_samp, width, n
        )
        row[:, 2] = _sample_pixels_inc(
            inst, sync_in_win + self._b_offset, self._scan_samp, width, n
        )
        row[:, 0] = _sample_pixels_inc(
            inst, sync_in_win + self._r_offset, self._scan_samp, width, n
        )
        self._image[grid_index] = row
        return [(grid_index, row.copy())]


# ---------------------------------------------------------------------------
# Wraase SC2 (LINE_START, RGB, no inter-channel gaps)
# ---------------------------------------------------------------------------


class WraaseIncrementalDecoder(IncrementalDecoderBase):
    """Streaming decoder for Wraase SC2-family modes (SC2-120, SC2-180).

    Layout: sync → porch → R_scan → G_scan → B_scan.  RGB order (not GBR
    like Martin), and only one porch — the three channel scans run
    back-to-back with no inter-channel gaps.
    """

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.LINE_START:
            raise ValueError(
                f"WraaseIncrementalDecoder only handles LINE_START modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = spec.sync_pulse_ms
        porch_ms = spec.sync_porch_ms
        # Mirrors _decode_wraase_rgb: 1 porch + 3 back-to-back scans.
        scan_ms = (spec.line_time_ms - sync_ms - porch_ms) / 3

        r_offset_ms = sync_ms + porch_ms
        g_offset_ms = r_offset_ms + scan_ms
        b_offset_ms = g_offset_ms + scan_ms
        b_end_ms = b_offset_ms + scan_ms

        self._r_channel_off: float = r_offset_ms / 1000.0 * fs
        self._g_channel_off: float = g_offset_ms / 1000.0 * fs
        self._b_channel_off: float = b_offset_ms / 1000.0 * fs
        self._scan_samp: float = scan_ms / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(b_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(b_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        row: NDArray[np.uint8] = np.zeros((width, 3), dtype=np.uint8)
        row[:, 0] = _sample_pixels_inc(
            inst, sync_in_win + self._r_channel_off, self._scan_samp, width, n
        )
        row[:, 1] = _sample_pixels_inc(
            inst, sync_in_win + self._g_channel_off, self._scan_samp, width, n
        )
        row[:, 2] = _sample_pixels_inc(
            inst, sync_in_win + self._b_channel_off, self._scan_samp, width, n
        )
        self._image[grid_index] = row
        return [(grid_index, row.copy())]


# ---------------------------------------------------------------------------
# Pasokon (LINE_START, RGB, equal inter-channel gaps on both sides of each scan)
# ---------------------------------------------------------------------------


class PasokonIncrementalDecoder(IncrementalDecoderBase):
    """Streaming decoder for Pasokon-family modes (P3, P5, P7).

    Layout: sync → gap → R_scan → gap → G_scan → gap → B_scan → gap.
    Four equal gaps flank the three RGB scans.  ``spec.sync_porch_ms``
    holds the gap duration (per the ModeSpec convention).
    """

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.LINE_START:
            raise ValueError(
                f"PasokonIncrementalDecoder only handles LINE_START modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = spec.sync_pulse_ms
        gap_ms = spec.sync_porch_ms
        # Mirrors _decode_pasokon_rgb: 4 gaps + 3 equal scans.
        scan_ms = (spec.line_time_ms - sync_ms - 4 * gap_ms) / 3

        r_offset_ms = sync_ms + gap_ms
        g_offset_ms = r_offset_ms + scan_ms + gap_ms
        b_offset_ms = g_offset_ms + scan_ms + gap_ms
        b_end_ms = b_offset_ms + scan_ms

        self._r_channel_off: float = r_offset_ms / 1000.0 * fs
        self._g_channel_off: float = g_offset_ms / 1000.0 * fs
        self._b_channel_off: float = b_offset_ms / 1000.0 * fs
        self._scan_samp: float = scan_ms / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(b_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(b_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        row: NDArray[np.uint8] = np.zeros((width, 3), dtype=np.uint8)
        row[:, 0] = _sample_pixels_inc(
            inst, sync_in_win + self._r_channel_off, self._scan_samp, width, n
        )
        row[:, 1] = _sample_pixels_inc(
            inst, sync_in_win + self._g_channel_off, self._scan_samp, width, n
        )
        row[:, 2] = _sample_pixels_inc(
            inst, sync_in_win + self._b_channel_off, self._scan_samp, width, n
        )
        self._image[grid_index] = row
        return [(grid_index, row.copy())]


# ---------------------------------------------------------------------------
# PD (LINE_START, line-pair YCbCr) — sync covers TWO image rows
# ---------------------------------------------------------------------------


class PDIncrementalDecoder(IncrementalDecoderBase):
    """Streaming decoder for PD-family modes (PD-50 / 90 / 120 / 160 /
    180 / 240 / 290).

    Layout: sync → porch → Y0_scan → Cr_scan → Cb_scan → Y1_scan.  Each
    sync pulse covers **two** output image rows: Y0 paints the even row,
    Y1 paints the odd row, and the single chroma pair is shared between
    them (nearest-neighbour chroma upsampling, matching ``_decode_pd``).

    ``spec.height`` is stored as (actual_image_height // 2) — the number
    of sync pulses, not the number of image rows.  The ``_image`` buffer
    is sized accordingly with ``_rows_per_sync = 2``.

    YCbCr → RGB is performed per super-pair using the same PIL machinery
    (``Image.frombytes("YCbCr", ...).convert("RGB")``) as the batch
    decoder, so output pixels are bit-identical.
    """

    _rows_per_sync: int = 2

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.LINE_START:
            raise ValueError(
                f"PDIncrementalDecoder only handles LINE_START modes "
                f"(got {spec.name!r} with sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = spec.sync_pulse_ms
        porch_ms = spec.sync_porch_ms
        # Mirrors _decode_pd: 1 porch + 4 equal channel scans.
        ch_ms = (spec.line_time_ms - sync_ms - porch_ms) / 4

        y0_off_ms = sync_ms + porch_ms
        cr_off_ms = y0_off_ms + ch_ms
        cb_off_ms = cr_off_ms + ch_ms
        y1_off_ms = cb_off_ms + ch_ms
        y1_end_ms = y1_off_ms + ch_ms

        self._y0_off: float = y0_off_ms / 1000.0 * fs
        self._cr_off: float = cr_off_ms / 1000.0 * fs
        self._cb_off: float = cb_off_ms / 1000.0 * fs
        self._y1_off: float = y1_off_ms / 1000.0 * fs
        self._ch_span: float = ch_ms / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(y1_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(y1_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        y0 = _sample_pixels_inc(
            inst, sync_in_win + self._y0_off, self._ch_span, width, n
        )
        cr = _sample_pixels_inc(
            inst, sync_in_win + self._cr_off, self._ch_span, width, n,
            chroma=True,
        )
        cb = _sample_pixels_inc(
            inst, sync_in_win + self._cb_off, self._ch_span, width, n,
            chroma=True,
        )
        y1 = _sample_pixels_inc(
            inst, sync_in_win + self._y1_off, self._ch_span, width, n
        )

        # Build a 2-row YCbCr buffer and let PIL do the BT.601 full-range
        # conversion, matching _decode_pd's final step exactly.
        pair = np.empty((2, width, 3), dtype=np.uint8)
        pair[0, :, 0] = y0
        pair[1, :, 0] = y1
        pair[0, :, 1] = cb  # Cb plane
        pair[1, :, 1] = cb
        pair[0, :, 2] = cr  # Cr plane
        pair[1, :, 2] = cr

        rgb_pair = np.asarray(
            Image.frombytes("YCbCr", (width, 2), pair.tobytes()).convert("RGB")
        )

        row_even = grid_index * 2
        row_odd = row_even + 1
        self._image[row_even] = rgb_pair[0]
        self._image[row_odd] = rgb_pair[1]
        return [
            (row_even, rgb_pair[0].copy()),
            (row_odd, rgb_pair[1].copy()),
        ]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


#: Modes we hand off to the incremental path.  Anything not in this set
#: stays on the batch decoder (currently only Robot 36 — its line-pair
#: + alternating-chroma dispatch doesn't fit the per-sync template).
_MARTIN_MODES: frozenset[Mode] = frozenset({
    Mode.MARTIN_M1, Mode.MARTIN_M2, Mode.MARTIN_M3, Mode.MARTIN_M4,
})
_SCOTTIE_MODES: frozenset[Mode] = frozenset({
    Mode.SCOTTIE_S1, Mode.SCOTTIE_S2, Mode.SCOTTIE_DX,
    Mode.SCOTTIE_S3, Mode.SCOTTIE_S4,
})
_PD_MODES: frozenset[Mode] = frozenset({
    Mode.PD_50, Mode.PD_90, Mode.PD_120, Mode.PD_160,
    Mode.PD_180, Mode.PD_240, Mode.PD_290,
})
_WRAASE_MODES: frozenset[Mode] = frozenset({
    Mode.WRAASE_SC2_120, Mode.WRAASE_SC2_180,
})
_PASOKON_MODES: frozenset[Mode] = frozenset({
    Mode.PASOKON_P3, Mode.PASOKON_P5, Mode.PASOKON_P7,
})


def make_incremental_decoder(
    spec: ModeSpec,
    fs: int,
    vis_end_abs: int,
    start_abs: int = 0,
) -> IncrementalDecoderBase | None:
    """Return an appropriate incremental decoder for ``spec``, or ``None``.

    Returns ``None`` for modes that don't yet have an incremental
    implementation (currently only Robot 36).  The caller should fall
    back to the batch decoder in that case.
    """
    mode = spec.name
    if mode in _SCOTTIE_MODES:
        return ScottieIncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    if mode in _MARTIN_MODES:
        return MartinIncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    if mode in _PD_MODES:
        return PDIncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    if mode in _WRAASE_MODES:
        return WraaseIncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    if mode in _PASOKON_MODES:
        return PasokonIncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    return None


__all__ = [
    "IncrementalDecoderBase",
    "ScottieIncrementalDecoder",
    "ScottieS1IncrementalDecoder",  # back-compat alias
    "MartinIncrementalDecoder",
    "PDIncrementalDecoder",
    "WraaseIncrementalDecoder",
    "PasokonIncrementalDecoder",
    "make_incremental_decoder",
]
