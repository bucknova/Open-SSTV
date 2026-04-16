# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-line streaming SSTV decoder — covers every mode in the app (default).

Enabled by default via ``AppConfig.incremental_decode = True``
(set to False to fall back to the legacy batch decoder).

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

Concrete backends cover every pixel-RGB and YCbCr mode we ship:

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
* ``Robot36IncrementalDecoder`` — auto-detecting wrapper that buffers
  the first ~450–900 ms of post-VIS audio, measures inter-sync spacing,
  and dispatches to the per-line or line-pair backend as appropriate.
  The backends are a Python port of the slowrx decoder (windytan/slowrx,
  GPL): single-sample-with-small-mean per pixel, nearest-neighbour
  chroma row copy, and slowrx's integer YCbCr→RGB matrix.  This is
  deliberately different from the windowed-median + PIL pipeline that
  every other mode uses — Robot 36's chroma channel is short, sync-
  adjacent, and was producing edge artefacts through that pipeline; the
  slowrx port is known-good against real-world HF transmissions.

Byte-identical guarantee
------------------------
With ``FILTER_MARGIN = 4096`` samples of sosfiltfilt padding before and
after each pixel region, the windowed filtered signal is numerically
identical to what the batch decoder produces on the full signal.  The
order-4 Butterworth impulse response decays to <1e-10 within ~200
samples; 4096 is massive overkill that guarantees byte-identical output
even at the very first line.  See ``test_incremental_decoder.py``.

NOTE: ``_sample_pixels_inc`` below is intentionally kept (mostly) in
sync with ``decoder._sample_pixels``.  It diverges on the chroma floor
and right-edge guard (see the function's docstring); Robot 36 does not
use this helper at all — it has its own slowrx-style sampler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from scipy.signal import sosfiltfilt

from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_WHITE_HZ, instantaneous_frequency
from open_sstv.core.dsp_utils import bandpass_sos
from open_sstv.core.modes import Mode, ModeSpec, SyncPosition
from open_sstv.core.robot36_dsp import (
    sample_pixel as _sample_pixel_slowrx,
    sample_scan as _sample_scan_slowrx,
    ycbcr_to_rgb as _ycbcr_to_rgb_slowrx,
)
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

# Frequencies below this are assumed to be sync-band leakage (sync is
# 1200 Hz, black/chroma-zero is 1500 Hz — so 1400 Hz leaves a 100 Hz
# guard on each side).  When chroma sampling lands on the sync pulse
# that follows a Robot 36 chroma scan, the instantaneous frequency
# crashes below this threshold; clamping to neutral 128 prevents the
# "byte 0 = strong green" artefact that shows up as a right-edge stripe.
# For luma, the existing [0, 255] clipping is already correct (below-
# black noise is just very dark), so this threshold is chroma-only.
_SYNC_REJECT_HZ: float = 1400.0


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

    **Diverges from ``decoder._sample_pixels``** on two points:

    1. The batch helper clamps any chroma frequency below 15 % of the
       signalling band (~byte 38) to neutral 128.  That corrupts every
       saturated yellow / green / cyan pixel because those have a
       genuine Cb or Cr value in [0, 38] under full-range BT.601.  This
       copy replaces the 15 % floor with a narrow sync-band reject at
       ``_SYNC_REJECT_HZ`` — legitimate low-chroma values decode as
       themselves, but sync-pulse leakage (~1200 Hz) still clamps to
       neutral and doesn't produce a green right-edge stripe.
    2. The right-edge ``guard_pixels`` skip stays at ``max(2, W//80)``
       (≈ 4 pixels on a 320-wide row).  This matches the batch decoder
       and is the minimum that covers Robot 36's chroma-to-sync
       transition: the bandpass filter's ringing smears the upcoming
       1200 Hz sync back 10-15 samples into the Cb scan, producing
       readings that slip below ``_SYNC_REJECT_HZ`` and would otherwise
       decode as byte-0 chroma → strong green bias on the last image
       column (most visible on dark-blue pixels).  The ~1.2 % right-
       edge fringe is the price for robust chroma at the edge.
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
    guard_pixels = max(2, width // 80) if chroma else 0
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
        # Sync-band reject: frequencies deep in sync territory are not
        # valid pixel data.  Chroma clamps to neutral (preserves neighbour
        # interpolation); luma falls through to the [0, 255] clip below
        # (sub-black noise just reads as very dark).
        if chroma and freq < _SYNC_REJECT_HZ:
            out[col] = neutral
            continue
        norm = (freq - span_lo) / span_range
        if norm < 0.0:
            norm = 0.0
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

        # Line period can be overridden by subclasses whose wire format
        # packs multiple image rows into a single sync pulse that is
        # ``N × line_time_ms`` apart (Robot 36 line-pair → N = 2).
        self._line_samp: float = self._line_period_samples()
        #: Number of sync pulses that compose a full image (grid height).
        #: Cached here so property calls don't re-enter the subclass hook
        #: on every ``feed()``.
        self._grid_len: int = self._grid_length()

        # --- Buffer state ---
        self._buf: NDArray = np.zeros(0, dtype=np.float64)
        self._buf_abs_start: int = start_abs
        self._total_fed: int = 0

        # --- Sync state ---
        self._sync_abs: list[int] = []
        self._last_search_end_abs: int = start_abs

        # --- Image state ---
        # _syncs_consumed counts grid positions turned into image rows;
        # it never exceeds _grid_len.
        self._syncs_consumed: int = 0
        image_rows = self._grid_len * self._rows_per_sync
        self._image: NDArray[np.uint8] = np.zeros(
            (image_rows, spec.width, 3), dtype=np.uint8
        )

    # --- Subclass hooks ---

    def _grid_length(self) -> int:
        """Number of sync pulses that make up the full image.

        Defaults to ``spec.height``.  Robot 36's line-pair wire format
        overrides this to ``spec.height // 2`` because one sync covers
        two image rows without that being reflected in the mode spec.
        """
        return self._spec.height

    def _line_period_samples(self) -> float:
        """Samples between consecutive sync pulses.

        Defaults to ``spec.line_time_ms``.  Robot 36's line-pair wire
        format overrides this to ``2 × line_time_ms`` so ``walk_sync_grid``
        treats super-lines as the unit of interest.
        """
        return self._spec.line_time_ms / 1000.0 * self._fs

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
        """Total rows in the output image (``grid_len × _rows_per_sync``)."""
        return self._grid_len * self._rows_per_sync

    @property
    def complete(self) -> bool:
        """True when all image rows have been decoded."""
        return self._syncs_consumed >= self._grid_len

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
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._grid_len)

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
        grid = walk_sync_grid(cands_in_buf, self._line_samp, self._grid_len)

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
# Robot 36 — ported from slowrx (windytan/slowrx, GPL)
# ---------------------------------------------------------------------------
#
# Implementation follows the slowrx reference decoder's approach instead
# of the shared ``_sample_pixels_inc`` / PIL machinery used by other
# modes.  The previous attempt at Robot 36 inherited the batch decoder's
# windowed-median sampler and a PIL ``YCbCr → RGB`` convert, plus
# several rounds of chroma-floor / sync-reject patches to paper over
# edge artefacts.  Each fix created a new one.  slowrx's model is
# fundamentally different, simpler, and known-good against real-world
# HF transmissions, so we port it wholesale.
#
# The three differences from our other decoders:
#
#   1. **Single-sample reads with a small local mean** (not a
#      windowed-central-60 % median).  slowrx reads one FFT-peak
#      frequency per pixel at the pixel centre; we approximate with a
#      5-sample mean of the Hilbert-derived instantaneous frequency
#      centred on each pixel.  That absorbs per-sample Hilbert noise
#      without pulling in neighbouring pixels or the chroma→sync
#      transition at the right edge.
#   2. **Nearest-neighbour chroma upsampling** (row-copy from the
#      adjacent native-chroma row).  slowrx does not interpolate; the
#      softer linear interpolation we tried first added complexity and
#      deferred-emission bookkeeping without meaningful image quality
#      gain.
#   3. **Direct integer YCbCr → RGB matrix**, bypassing PIL.  The
#      coefficients are slowrx's (rounded BT.601 full-range), expressed
#      as ``(100·Y + 140·Cr − 17850) / 100`` etc.  Numerically close to
#      PIL's convert-YCbCr but gives explicit, reproducible output
#      matching the reference bit-for-bit.
#
# The batch Robot 36 decoder in ``decoder.py`` is still the old
# median / PIL / nearest-neighbour path — intentional, so the
# experimental flag lets users A/B between batch and slowrx-style from
# a single build.
#
# slowrx itself does not implement the PySSTV per-line wire format,
# but the sampling model transfers cleanly; we support both wire
# formats with the same helpers and the ``Robot36IncrementalDecoder``
# wrapper auto-detects at feed time.


class _Robot36PerLineIncrementalDecoder(IncrementalDecoderBase):
    """Streaming Robot 36 decoder for the PySSTV-style per-line wire format.

    Layout::

        SYNC (9 ms) | SYNC_PORCH (3 ms) | Y (88 ms) | GAP (4.5 ms) |
        PORCH (1.5 ms) | C (44 ms)

    One sync per image row.  Even rows carry Cr, odd rows carry Cb; the
    complementary chroma is filled by nearest-neighbour row copy from
    the immediately-adjacent opposite-parity row (slowrx's model).

    Emission: each sync produces one image row with the current row's
    Y plus best-available chroma.  Row N is emitted as soon as its sync
    is processed; when row N+1's sync later arrives, row N's missing-
    chroma plane is back-filled from row N+1 and the row is re-emitted.
    That re-emission produces a momentarily monotonicity break in
    ``lines_decoded`` (N+1 then N), but ``get_image()`` always returns
    a self-consistent snapshot so the UI just redraws row N.
    """

    _rows_per_sync: int = 1

    def __init__(
        self,
        spec: ModeSpec,
        fs: int,
        vis_end_abs: int,
        start_abs: int = 0,
    ) -> None:
        if spec.sync_position != SyncPosition.LINE_START:
            raise ValueError(
                f"_Robot36PerLineIncrementalDecoder only handles LINE_START "
                f"modes (got {spec.name!r} with "
                f"sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)
        h = self._grid_len
        w = spec.width
        self._y_plane: NDArray[np.uint8] = np.zeros((h, w), dtype=np.uint8)
        self._cb_plane: NDArray[np.uint8] = np.full((h, w), 128, dtype=np.uint8)
        self._cr_plane: NDArray[np.uint8] = np.full((h, w), 128, dtype=np.uint8)

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        # Same timing constants as the batch decoder.
        sync_ms = 9.0
        sync_porch_ms = 3.0
        y_scan_ms = 88.0
        inter_ch_gap_ms = 4.5
        porch_ms = 1.5
        c_scan_ms = 44.0

        y_offset_ms = sync_ms + sync_porch_ms
        c_offset_ms = y_offset_ms + y_scan_ms + inter_ch_gap_ms + porch_ms
        c_end_ms = c_offset_ms + c_scan_ms

        self._y_off: float = y_offset_ms / 1000.0 * fs
        self._c_off: float = c_offset_ms / 1000.0 * fs
        self._y_span: float = y_scan_ms / 1000.0 * fs
        self._c_span: float = c_scan_ms / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(c_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(c_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        y_row = _sample_scan_slowrx(
            inst, sync_in_win + self._y_off, self._y_span, width, n
        )
        c_row = _sample_scan_slowrx(
            inst, sync_in_win + self._c_off, self._c_span, width, n
        )
        self._y_plane[grid_index] = y_row
        if grid_index % 2 == 0:
            self._cr_plane[grid_index] = c_row  # even rows carry Cr
        else:
            self._cb_plane[grid_index] = c_row  # odd rows carry Cb

        out: list[tuple[int, NDArray[np.uint8]]] = []
        # Emit the current row immediately with best-available chroma
        # (its native component + a backward-copy of the complementary
        # component from the previous row, if any).
        self._fill_missing_chroma(grid_index)
        out.append((grid_index, self._emit_row(grid_index)))
        # Back-fill and re-emit the previous row now that its forward
        # neighbour's native chroma is known (slowrx's row-copy model).
        if grid_index >= 1:
            self._fill_missing_chroma(grid_index - 1)
            out.append((grid_index - 1, self._emit_row(grid_index - 1)))
        return out

    def _fill_missing_chroma(self, row: int) -> None:
        """Row-copy the complementary-chroma plane from the nearer
        neighbour, matching slowrx's nearest-neighbour upsample."""
        h = self._grid_len
        if row % 2 == 0:
            # Even row: Cr native, need Cb.  Prefer forward neighbour
            # (row+1 carries native Cb); fall back to backward.
            plane = self._cb_plane
            src = row + 1 if row + 1 < h else row - 1
        else:
            plane = self._cr_plane
            src = row - 1 if row - 1 >= 0 else row + 1
        if 0 <= src < h:
            plane[row] = plane[src]

    def _emit_row(self, row: int) -> "NDArray[np.uint8]":
        rgb = _ycbcr_to_rgb_slowrx(
            self._y_plane[row : row + 1],
            self._cb_plane[row : row + 1],
            self._cr_plane[row : row + 1],
        )
        self._image[row] = rgb[0]
        return rgb[0].copy()


class _Robot36LinePairIncrementalDecoder(IncrementalDecoderBase):
    """Streaming Robot 36 decoder for the canonical broadcast line-pair format.

    Layout: one sync pulse per super-line, each super-line covering two
    image rows with shared Cr and Cb samples::

        SYNC | SYNC_PORCH | Y0 | GAP | PORCH | Cr | SYNC_PORCH
                          | Y1 | GAP | PORCH | Cb

    slowrx (``video.c`` lines 410-445) assigns the pair's Cr/Cb to both
    rows via a plain row-copy — no interpolation between adjacent
    pairs.  We match that verbatim: the two image rows produced per
    sync share identical Cb/Cr planes, and the final YCbCr→RGB matrix
    runs per-row through ``_ycbcr_to_rgb_slowrx``.
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
                f"_Robot36LinePairIncrementalDecoder only handles LINE_START "
                f"modes (got {spec.name!r} with "
                f"sync_position={spec.sync_position!r})"
            )
        super().__init__(spec, fs, vis_end_abs, start_abs)
        h = self._grid_len * 2
        w = spec.width
        self._y_plane: NDArray[np.uint8] = np.zeros((h, w), dtype=np.uint8)
        self._cb_plane: NDArray[np.uint8] = np.full((h, w), 128, dtype=np.uint8)
        self._cr_plane: NDArray[np.uint8] = np.full((h, w), 128, dtype=np.uint8)

    def _grid_length(self) -> int:
        # One sync per pair; spec.height is the full image height.
        return self._spec.height // 2

    def _line_period_samples(self) -> float:
        # Pair-to-pair spacing is twice the canonical single-line period.
        return 2.0 * self._spec.line_time_ms / 1000.0 * self._fs

    def _compute_timing(self, spec: ModeSpec, fs: int) -> None:
        sync_ms = 9.0
        sync_porch_ms = 3.0
        y_scan_ms = 88.0
        inter_ch_gap_ms = 4.5
        porch_ms = 1.5
        c_scan_ms = 44.0

        y0_off_ms = sync_ms + sync_porch_ms
        cr_off_ms = y0_off_ms + y_scan_ms + inter_ch_gap_ms + porch_ms
        y1_off_ms = cr_off_ms + c_scan_ms + sync_porch_ms
        cb_off_ms = y1_off_ms + y_scan_ms + inter_ch_gap_ms + porch_ms
        cb_end_ms = cb_off_ms + c_scan_ms

        self._y0_off: float = y0_off_ms / 1000.0 * fs
        self._cr_off: float = cr_off_ms / 1000.0 * fs
        self._y1_off: float = y1_off_ms / 1000.0 * fs
        self._cb_off: float = cb_off_ms / 1000.0 * fs
        self._y_span: float = y_scan_ms / 1000.0 * fs
        self._c_span: float = c_scan_ms / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(cb_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(cb_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: "NDArray",
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        width = self._spec.width
        y0 = _sample_scan_slowrx(
            inst, sync_in_win + self._y0_off, self._y_span, width, n
        )
        y1 = _sample_scan_slowrx(
            inst, sync_in_win + self._y1_off, self._y_span, width, n
        )
        cr = _sample_scan_slowrx(
            inst, sync_in_win + self._cr_off, self._c_span, width, n
        )
        cb = _sample_scan_slowrx(
            inst, sync_in_win + self._cb_off, self._c_span, width, n
        )

        row_even = grid_index * 2
        row_odd = row_even + 1
        self._y_plane[row_even] = y0
        self._y_plane[row_odd] = y1
        # slowrx: nearest-neighbour upsample → both rows share this
        # pair's chroma verbatim.
        self._cr_plane[row_even] = cr
        self._cr_plane[row_odd] = cr
        self._cb_plane[row_even] = cb
        self._cb_plane[row_odd] = cb

        rgb_pair = _ycbcr_to_rgb_slowrx(
            self._y_plane[row_even : row_odd + 1],
            self._cb_plane[row_even : row_odd + 1],
            self._cr_plane[row_even : row_odd + 1],
        )
        self._image[row_even] = rgb_pair[0]
        self._image[row_odd] = rgb_pair[1]
        return [
            (row_even, rgb_pair[0].copy()),
            (row_odd, rgb_pair[1].copy()),
        ]


class Robot36IncrementalDecoder:
    """Auto-detecting streaming decoder for Robot 36.

    Robot 36 is transmitted in two mutually incompatible wire formats:

    * **Per-line** (PySSTV, slowrx): one sync pulse per image row,
      chroma alternating Cr / Cb line by line.  Inter-sync spacing is
      150 ms.
    * **Line-pair** (SimpleSSTV iOS, MMSSTV, most HF broadcasts): one
      sync pulse per two image rows, carrying shared Cr and Cb within
      the super-line.  Inter-sync spacing is 300 ms.

    The wire format can't be detected from the VIS code — both encode as
    0x08.  This wrapper buffers incoming audio until it has enough post-
    VIS sync candidates to measure the median inter-sync spacing, then
    constructs the appropriate backend and replays the buffered audio
    through it.

    Detection threshold is ``_DETECT_SYNC_COUNT`` candidates.  At 48 kHz
    this resolves within ~450 ms of audio for per-line (three 150 ms
    periods) or ~900 ms for line-pair (three 300 ms periods) — well
    under a single UI flush cycle, so the user sees no perceptible
    delay before rows start arriving.

    The class doesn't subclass ``IncrementalDecoderBase`` because the
    choice of line period and grid length is format-dependent; it
    duck-types the public API (``feed``, ``get_image``, ``complete``,
    ``image_height``, ``lines_decoded``, ``total_fed``) so
    ``Decoder._feed_decoding_incremental`` can treat it interchangeably.
    """

    #: Minimum sync candidates needed to decide between per-line and
    #: line-pair.  Three gives us two inter-sync diffs; the median of
    #: two is robust to a single missed / spurious candidate.
    _DETECT_SYNC_COUNT: int = 3

    #: ±25 % tolerance on the median inter-sync diff, matching
    #: ``walk_sync_grid`` and the batch ``_decode_robot36_dispatch``.
    _DETECT_TOLERANCE: float = 0.25

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
        self._start_abs = start_abs
        self._total_fed: int = 0
        self._backend: IncrementalDecoderBase | None = None
        self._pending: list[NDArray] = []
        self._placeholder: NDArray[np.uint8] = np.zeros(
            (spec.height, spec.width, 3), dtype=np.uint8
        )

    # --- Public API (duck-typed to match IncrementalDecoderBase) ---

    @property
    def total_fed(self) -> int:
        return self._total_fed

    @property
    def lines_decoded(self) -> int:
        return self._backend.lines_decoded if self._backend is not None else 0

    @property
    def image_height(self) -> int:
        # Both backends produce spec.height rows in the end.  Returning
        # spec.height before backend selection keeps the UI progress
        # denominator stable across the detection boundary.
        if self._backend is not None:
            return self._backend.image_height
        return self._spec.height

    @property
    def complete(self) -> bool:
        return self._backend is not None and self._backend.complete

    def get_image(self) -> Image.Image:
        if self._backend is not None:
            return self._backend.get_image()
        return Image.fromarray(self._placeholder)

    def feed(
        self, chunk: "NDArray"
    ) -> list[tuple[int, "NDArray[np.uint8]"]]:
        arr = np.asarray(chunk, dtype=np.float64)
        if arr.ndim != 1 or arr.size == 0:
            return []
        self._total_fed += arr.size

        if self._backend is not None:
            return self._backend.feed(arr)

        self._pending.append(arr)
        backend_cls = self._try_detect()
        if backend_cls is None:
            return []

        # Construct the chosen backend and replay all buffered audio
        # so it sees the stream from the configured start_abs onwards.
        self._backend = backend_cls(
            self._spec, self._fs, self._vis_end_abs, self._start_abs,
        )
        replay = (
            np.concatenate(self._pending)
            if len(self._pending) > 1
            else self._pending[0]
        )
        self._pending = []
        return self._backend.feed(replay)

    # --- Internal ---

    def _try_detect(self) -> type[IncrementalDecoderBase] | None:
        """Estimate median sync spacing over the buffered post-VIS tail."""
        if not self._pending:
            return None
        buf = (
            np.concatenate(self._pending)
            if len(self._pending) > 1
            else self._pending[0]
        )
        vis_end_buf = max(0, self._vis_end_abs - self._start_abs)
        if buf.size <= vis_end_buf:
            return None
        tail = buf[vis_end_buf:]
        if tail.size < _MIN_BP_SAMPLES:
            return None
        filtered = _bp_window(tail, self._fs)
        inst = instantaneous_frequency(filtered, self._fs)
        line_samples = self._spec.line_time_ms / 1000.0 * self._fs
        cands = find_sync_candidates(
            inst,
            self._fs,
            self._spec.sync_pulse_ms,
            line_period_samples=line_samples,
            start_idx=0,
        )
        if len(cands) < self._DETECT_SYNC_COUNT:
            return None
        diffs = np.diff(np.asarray(cands, dtype=np.float64))
        if diffs.size == 0:
            return None
        median_diff = float(np.median(diffs))
        pair_samples = 2.0 * line_samples
        tol = self._DETECT_TOLERANCE
        if abs(median_diff - line_samples) <= line_samples * tol:
            return _Robot36PerLineIncrementalDecoder
        if abs(median_diff - pair_samples) <= pair_samples * tol:
            return _Robot36LinePairIncrementalDecoder
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


#: Modes we hand off to the incremental path.  All modes in the app are
#: now covered by an incremental backend; ``make_incremental_decoder``
#: returns ``None`` only for a completely unknown mode.
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
) -> IncrementalDecoderBase | Robot36IncrementalDecoder | None:
    """Return an appropriate incremental decoder for ``spec``, or ``None``.

    Returns ``None`` only if ``spec.name`` is a mode we don't recognise.
    All modes currently in ``Mode`` are covered: Scottie, Martin, PD,
    Wraase SC2, Pasokon, and Robot 36 (both wire formats via
    ``Robot36IncrementalDecoder``).  The caller should fall back to the
    batch decoder when ``None`` is returned.
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
    if mode == Mode.ROBOT_36:
        return Robot36IncrementalDecoder(spec, fs, vis_end_abs, start_abs)
    return None


__all__ = [
    "IncrementalDecoderBase",
    "ScottieIncrementalDecoder",
    "ScottieS1IncrementalDecoder",  # back-compat alias
    "MartinIncrementalDecoder",
    "PDIncrementalDecoder",
    "WraaseIncrementalDecoder",
    "PasokonIncrementalDecoder",
    "Robot36IncrementalDecoder",
    "make_incremental_decoder",
]
