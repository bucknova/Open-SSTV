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
* ``Robot36IncrementalDecoder`` — one sync per line, Y + alternating
  Cr (even) / Cb (odd).  A Python port of the slowrx decoder
  (windytan/slowrx, GPL): single-sample-with-small-mean per pixel,
  nearest-neighbour chroma row copy, and slowrx's integer YCbCr→RGB
  matrix, shared with the batch decoder via ``core.robot36_dsp``.  This is
  deliberately different from the windowed-median + PIL pipeline that
  every other mode uses — Robot 36's chroma channel is short, sync-
  adjacent, and was producing edge artefacts through that pipeline; the
  slowrx port is known-good against real-world HF transmissions.

Agreement with the batch decoder
--------------------------------
Each line window carries ``FILTER_MARGIN = 4096`` samples of sosfiltfilt
padding on both sides, so the windowed bandpass output matches what the
batch decoder computes on the full signal (the order-4 Butterworth
impulse response decays to <1e-10 within ~200 samples).  The decoded
pixels are *close to* but not byte-identical with ``decode_wav``: the
live path smooths the IF track with a 3-tap boxcar before sampling (see
``_try_decode_next``), walks the sync grid instead of slant-fitting it,
and detects sync candidates on a sliding tail.  Pixel sampling itself is
the one shared implementation, ``demod.sample_pixels`` (Robot 36:
``robot36_dsp.sample_scan``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
from PIL import Image

from open_sstv.core import robot36_dsp
from open_sstv.core.demod import instantaneous_frequency
from open_sstv.core.demod import sample_pixels as _sample_pixels_inc
from open_sstv.core.dsp_utils import SSTV_BANDPASS_MIN_SAMPLES as _MIN_BP_SAMPLES
from open_sstv.core.dsp_utils import sstv_bandpass as _bp_window
from open_sstv.core.modes import Mode, ModeSpec, SyncPosition
from open_sstv.core.robot36_dsp import (
    sample_pixel as _sample_pixel_slowrx,  # noqa: F401 — re-exported for tests
)
from open_sstv.core.robot36_dsp import (
    sample_scan as _sample_scan_slowrx,
)
from open_sstv.core.robot36_dsp import (
    ycbcr_to_rgb as _ycbcr_to_rgb_slowrx,
)
from open_sstv.core.sync import (
    find_sync_candidates,
    line0_sync_hint,
    walk_sync_grid,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray


# ---------------------------------------------------------------------------
# Public Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class IncrementalDecoder(Protocol):
    """Structural type shared by all per-line streaming decoder backends.

    Every ``IncrementalDecoderBase`` subclass (Scottie, Martin, PD,
    Wraase, Pasokon, Robot 36) satisfies this Protocol.

    Use this as the annotation for variables that may hold any concrete
    incremental decoder — e.g. ``_incremental_dec: IncrementalDecoder | None``.
    """

    @property
    def complete(self) -> bool: ...

    @property
    def image_height(self) -> int: ...

    @property
    def lines_decoded(self) -> int: ...

    @property
    def total_fed(self) -> int: ...

    def feed(self, chunk: NDArray) -> list[tuple[int, NDArray[np.uint8]]]: ...

    def get_image(self) -> Image.Image: ...


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
    #: 4096 makes the windowed filter output match the batch decoder's.
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
        # L (v0.3 audit): a zero/negative rate or line time would
        # surface as a divide-by-zero in ``_line_period_samples`` (or
        # NaN geometry) several calls later — fail at construction with
        # a message that points at the bad input instead.
        if fs <= 0:
            raise ValueError(f"Sample rate must be positive (got {fs})")
        if spec.line_time_ms <= 0:
            raise ValueError(
                f"Mode line time must be positive (got {spec.line_time_ms})"
            )
        self._spec = spec
        self._fs = fs
        self._vis_end_abs = vis_end_abs
        #: Absolute position line 0's sync should have (audit D-H1): lets
        #: the grid walk extrapolate back over a missed line-0/1 sync.
        self._line0_abs: float = line0_sync_hint(spec, fs, vis_end_abs)

        # Hook for subclass geometry (offsets, channel scan length, etc.).
        self._compute_timing(spec, fs)

        # Line period hook (subclasses may override).
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

        Defaults to ``spec.height``.
        """
        return self._spec.height

    def _line_period_samples(self) -> float:
        """Samples between consecutive sync pulses.

        Defaults to ``spec.line_time_ms``.
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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

    def feed(self, chunk: NDArray) -> list[tuple[int, NDArray[np.uint8]]]:
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
        self._enforce_stall_cap()
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
            dup = next(
                (i for i, s in enumerate(self._sync_abs)
                 if abs(c_abs - s) <= self._DEDUP_RADIUS),
                None,
            )
            if dup is None:
                self._sync_abs.append(c_abs)
            else:
                # Re-found in the overlap: prefer the newer estimate. The
                # first sighting was near the end of its search tail, where
                # the adaptive threshold's rolling window is truncated; the
                # re-search has a full window on both sides (this is what
                # made live line positions jitter by up to ~0.3 ms against
                # the batch decoder's).
                self._sync_abs[dup] = c_abs

        self._sync_abs.sort()
        self._last_search_end_abs = current_abs

    def _try_decode_next(
        self,
        current_abs: int,
        out: list[tuple[int, NDArray[np.uint8]]],
    ) -> bool:
        """Attempt to decode the next pending line.

        Returns True if a line was decoded (and more may follow), False if
        we should stop trying (not enough audio yet, or grid not anchored).
        """
        if len(self._sync_abs) < 2:
            return False

        grid = self._grid()

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

        # Short uniform smooth to reduce per-sample IF noise before pixel
        # extraction.  3 samples is ≤25 % of the narrowest pixel span
        # (Robot 36 luma at 44.1 kHz ≈ 12 samples/px) so horizontal detail
        # is unaffected on strong signals, while weak-signal colour noise is
        # reduced by ~√3 ≈ 1.7×.  The convolution output matches the input
        # length (mode="same") so ``n`` and all window offsets stay valid.
        if n > 3:
            _k = np.ones(3, dtype=np.float64) / 3.0
            inst = np.convolve(inst, _k, mode="same")

        sync_in_win = sync_in_buf - win_start

        new_rows = self._decode_window(
            inst, sync_in_win, n, self._syncs_consumed
        )
        out.extend(new_rows)
        self._syncs_consumed += 1
        return True

    def _grid(self) -> list[int]:
        """Buffer-relative line grid from all sync candidates so far,
        anchored on the VIS-derived line-0 slot (audit D-H1)."""
        return walk_sync_grid(
            [s - self._buf_abs_start for s in self._sync_abs],
            self._line_samp,
            self._grid_len,
            first_slot_hint=self._line0_abs - self._buf_abs_start,
        )

    def _enforce_stall_cap(self) -> None:
        """Bound ``_buf`` when a decode stalls (audit #10).

        ``_prune`` trims only behind the next expected grid position, so
        a stalled decode grows without limit in two ways: before the
        first line ever decodes (``_prune`` early-returns entirely) and
        after a mid-image signal loss (the prune anchor freezes while
        dead-carrier audio piles up behind it).  Either way, once we've
        buffered more than 1.5 image-durations past the anchor the
        decode is not going to complete from that grid — drop the head
        and discard sync candidates that fell off the front.

        Consequences are deliberately conservative: rows already
        decoded stay in the image; if the trim leaves fewer grid
        positions than ``_syncs_consumed``, ``_try_decode_next``'s
        bounds check simply stops producing new rows (bounded memory,
        no corruption), and the caller's watchdog/reset handles the
        stalled image exactly as it did before — just without the
        multi-GB memory bill for long headless sessions.
        """
        cap = (
            int(self._grid_len * self._line_samp * 1.5)
            + self._g_lookback
            + self.FILTER_MARGIN
        )
        if len(self._buf) <= cap:
            return
        drop = len(self._buf) - cap
        self._buf = self._buf[drop:]
        self._buf_abs_start += drop
        self._sync_abs = [s for s in self._sync_abs if s >= self._buf_abs_start]

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
        grid = self._grid()

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
        # Spec layout (v0.6.14 audit D-L3), mirrors _decode_scottie_rgb:
        # SEP | G | SEP | B | SYNC | PORCH | R
        scan_ms = (spec.line_time_ms - sync_ms - 3.0 * porch_ms) / 3.0

        # G starts this many ms BEFORE the sync (negative offset).
        # R ends this many ms AFTER the sync (positive offset).
        g_start_ms = -(scan_ms + porch_ms + scan_ms)
        r_end_ms = sync_ms + porch_ms + scan_ms

        self._g_offset: float = g_start_ms / 1000.0 * fs          # negative
        self._b_offset: float = -scan_ms / 1000.0 * fs             # negative
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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
# The batch Robot 36 decoder in ``decoder.py`` (``decode_wav`` / CLI /
# "Decode audio file") runs the same ``robot36_dsp`` helpers, so the
# two paths share their sampling and colour pipeline.


class Robot36IncrementalDecoder(IncrementalDecoderBase):
    """Streaming Robot 36 decoder (one sync per line).

    v0.6.14 audit D-H2: this used to sit behind an auto-detecting wrapper
    that chose between this backend and a 290.5 ms "line-pair" backend
    from the spacing of the first three syncs; one missed sync picked
    the line-pair backend and decoded garbage. No spec or reference
    decoder has a line-pair Robot 36 layout, so the wrapper and that
    backend are gone and every Robot 36 signal comes straight here.

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
                f"Robot36IncrementalDecoder only handles LINE_START "
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
        # Same layout constants as the batch decoder (``robot36_dsp``).
        c_end_ms = robot36_dsp.C_END_MS

        self._y_off: float = robot36_dsp.Y_OFFSET_MS / 1000.0 * fs
        self._c_off: float = robot36_dsp.C_OFFSET_MS / 1000.0 * fs
        self._y_span: float = robot36_dsp.Y_SCAN_MS / 1000.0 * fs
        self._c_span: float = robot36_dsp.C_SCAN_MS / 1000.0 * fs

        self._g_lookback: int = self.FILTER_MARGIN
        self._r_lookahead: int = int(c_end_ms / 1000.0 * fs) + self.FILTER_MARGIN
        self._ready_post_sync: int = int(c_end_ms / 1000.0 * fs)

    def _decode_window(
        self,
        inst: NDArray,
        sync_in_win: int,
        n: int,
        grid_index: int,
    ) -> list[tuple[int, NDArray[np.uint8]]]:
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

    def _emit_row(self, row: int) -> NDArray[np.uint8]:
        rgb = _ycbcr_to_rgb_slowrx(
            self._y_plane[row : row + 1],
            self._cb_plane[row : row + 1],
            self._cr_plane[row : row + 1],
        )
        self._image[row] = rgb[0]
        return rgb[0].copy()


#: Back-compat alias for the pre-v0.6.15 name of the per-line backend.
_Robot36PerLineIncrementalDecoder = Robot36IncrementalDecoder


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
) -> IncrementalDecoderBase | None:
    """Return an appropriate incremental decoder for ``spec``, or ``None``.

    Returns ``None`` only if ``spec.name`` is a mode we don't recognise.
    All modes currently in ``Mode`` are covered: Scottie, Martin, PD,
    Wraase SC2, Pasokon, and Robot 36.
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
    "IncrementalDecoder",
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
