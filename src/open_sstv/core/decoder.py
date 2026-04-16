# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level SSTV decoder.

This module is the buffered front-end of the receive pipeline. It owns:

* a small audio ring buffer (so chunked feeds can be re-windowed without
  callers having to track absolute sample positions),
* the high-level state machine (idle → searching for VIS → decoding image),
* the dispatch into per-mode pixel decoders.

Phase 2 step 13 ships the **Robot 36** decoder. Step 14 adds Martin M1
and Scottie S1 per-mode functions plus a dual-layout Robot 36 path (see
below) — the ``Decoder.feed`` plumbing is mode-agnostic so each step
only touches the dispatch table.

Robot 36 has two wire formats in the wild:

* PySSTV's per-line layout — 240 sync pulses at 150 ms intervals,
  each followed by one Y and one chroma scan (Cr on even, Cb on odd).
* The canonical broadcast "line-pair" layout — 120 sync pulses at
  290 ms intervals, each followed by **two** Y scans and **two**
  chroma scans (Cr then Cb) with no sync between them. This is what
  SimpleSSTV iOS, MMSSTV, and most over-the-air encoders emit.

``decode_wav`` auto-detects the layout by measuring the median spacing
of raw sync candidates before walking the grid, then dispatches to
``_decode_robot36`` (single-line) or ``_decode_robot36_line_pair``.

Public API
----------

``decode_wav(samples, fs) -> DecodedImage | None``
    One-shot decode of an entire audio buffer. Returns the first complete
    image found, or ``None`` if no VIS or supported mode was detected.
    This is what the CLI and the round-trip tests call.

``Decoder``
    Streaming, pull-model API used by ``ui/workers.py``. Callers push
    audio chunks via ``feed(samples)`` and pop ``DecoderEvent`` objects
    describing what happened (``ImageStarted``, ``LineDecoded``,
    ``ImageComplete``). Internally it just buffers audio until enough is
    available to call ``decode_wav`` end-to-end — Phase 2 keeps the
    streaming wrapper deliberately dumb so we can iterate on the per-mode
    decoders without rewriting state-machine code.

Algorithm sketch (Robot 36)
---------------------------

PySSTV's encoder (verified by reading ``pysstv/color.py``) emits each
scan line as::

    1200 Hz sync ─ 9 ms
    1500 Hz porch ─ 3 ms
    Y scan ─ 88 ms (320 pixels @ 0.275 ms each)
    inter-channel gap ─ 4.5 ms
        2300 Hz on odd lines (sending Cb)
        1500 Hz on even lines (sending Cr)
    1900 Hz porch ─ 1.5 ms
    chroma scan ─ 44 ms (320 samples @ 0.1375 ms each)

Y is sent on every line; chroma alternates Cb (odd lines) / Cr (even
lines), so each pair of adjacent lines carries one full set of YCbCr.
The decoder pairs line N with line N+1, fills in the missing chroma
from its neighbor, and converts YCbCr→RGB via Pillow.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

import numpy as np
from PIL import Image
from scipy.signal import sosfiltfilt

from open_sstv.core.demod import (
    SSTV_BLACK_HZ,
    SSTV_WHITE_HZ,
    instantaneous_frequency,
)
from open_sstv.core.dsp_utils import bandpass_sos
from open_sstv.core.modes import MODE_TABLE, Mode, ModeSpec, mode_from_vis
from open_sstv.core.robot36_dsp import (
    sample_scan as _robot36_sample_scan,
    ycbcr_to_rgb as _robot36_ycbcr_to_rgb,
)
from open_sstv.core.slant import slant_corrected_line_starts
from open_sstv.core.sync import find_sync_candidates, walk_sync_grid
from open_sstv.core.vis import detect_vis

# Bandpass edges for the pre-demod filter. The SSTV signalling frequencies
# span 1100 Hz (VIS '1') through 2300 Hz (white luma); we push the edges
# out to 1000–2500 Hz to leave ~100 Hz of margin for filter rolloff and
# plausible TX/RX clock drift. Butterworth order 4 gives ~24 dB/octave
# stopband attenuation, which — combined with ``sosfiltfilt``'s zero-phase
# response — drops out-of-band noise variance enough to recover ~12 dB of
# sync-detector margin vs. running the Hilbert transform on raw audio.
# Measured in tests/core/test_decoder.py::test_decode_wav_robot36_low_snr.
_BANDPASS_LOW_HZ: float = 1000.0
_BANDPASS_HIGH_HZ: float = 2500.0
_BANDPASS_ORDER: int = 4

# ``sosfiltfilt`` forward/back filters with a default padlen ≈ 3 * filter
# length; very short inputs (well below any plausible SSTV image) trip
# that guard. Skip filtering for buffers under this threshold — the
# downstream decode will return ``None`` for them anyway.
_BANDPASS_MIN_SAMPLES: int = 256

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from open_sstv.core.incremental_decoder import ScottieS1IncrementalDecoder


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """A successfully decoded SSTV image plus the metadata callers need."""

    image: Image.Image
    mode: Mode
    vis_code: int


def decode_wav(
    samples: NDArray, fs: int
) -> DecodedImage | None:
    """Decode an audio buffer into a single SSTV image.

    Looks for the first VIS header, dispatches into the right per-mode
    decoder, and returns the resulting image. Returns ``None`` if no
    VIS was found or the detected mode isn't one of the v1 modes
    (Robot 36 / Martin M1 / Scottie S1).

    Parameters
    ----------
    samples:
        1-D float audio buffer at sample rate ``fs``. Amplitude-invariant.
    fs:
        Sample rate of ``samples`` in Hz. Anything reasonable for SSTV
        works (44.1 kHz and 48 kHz are the common cases).
    """
    arr = np.asarray(samples, dtype=np.float64)
    if arr.ndim != 1 or arr.size == 0:
        return None

    # Bandpass-filter to the SSTV signalling band before demodulation.
    # Without this the Hilbert transform sees noise from DC to Nyquist,
    # which inflates FM-demod variance and makes the sync detector
    # collapse around 12 dB SNR. Zero-phase (``sosfiltfilt``) is
    # mandatory — any group delay would shift sync positions and corrupt
    # the per-line pixel slicing downstream. See the module-level
    # constants for the edge choices and the empirical measurements.
    filtered = _bandpass(arr, fs)

    vis_result = detect_vis(filtered, fs)
    if vis_result is None:
        return None
    vis_code, vis_end = vis_result

    mode = mode_from_vis(vis_code)
    if mode is None:
        return None

    spec = MODE_TABLE[mode]
    inst = instantaneous_frequency(filtered, fs)

    # Robot 36 has two incompatible wire formats (see module docstring).
    # Detect which one this WAV uses before fitting a sync grid.
    if mode == Mode.ROBOT_36:
        image = _decode_robot36_dispatch(inst, fs, spec, vis_end)
    else:
        line_samples = spec.line_time_ms / 1000.0 * fs
        candidates = find_sync_candidates(
            inst,
            fs,
            spec.sync_pulse_ms,
            line_period_samples=line_samples,
            start_idx=vis_end,
        )
        line_starts = slant_corrected_line_starts(
            candidates, line_samples, spec.height
        )
        if len(line_starts) < spec.height:
            return None
        # Reject truncated buffers: slant projection happily extrapolates
        # past the end of ``inst`` and ``_sample_pixels`` silently fills
        # out-of-range pixels with zeros, which would hand callers a
        # half-black image instead of a clean decode failure. The check
        # is on the last projected line *start*, not end, because for
        # Scottie (``sync_position=BEFORE_RED``) the anchor lands mid-line
        # and the ``line_samples`` after it extends past the encoded end.
        if line_starts[-1] >= inst.size:
            return None
        decoder = _PIXEL_DECODERS.get(mode)
        if decoder is None:
            return None
        image = decoder(inst, fs, spec, line_starts)

    if image is None:
        return None
    return DecodedImage(image=image, mode=mode, vis_code=vis_code)


def _decode_robot36_dispatch(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    vis_end: int,
) -> Image.Image | None:
    """Auto-detect Robot 36 wire format and dispatch to the right decoder.

    Uses the raw 1200 Hz sync candidates (length-filtered, no grid walk)
    to estimate the actual inter-sync spacing. If it matches the
    canonical per-line period within 25 %, the file is PySSTV-style and
    we walk a ``spec.height`` grid. If it matches 2× that period, the
    file is the canonical broadcast line-pair format and we walk a
    ``spec.height // 2`` grid of super-lines. Anything else is rejected.
    """
    line_samples = spec.line_time_ms / 1000.0 * fs
    candidates = find_sync_candidates(
        inst,
        fs,
        spec.sync_pulse_ms,
        line_period_samples=line_samples,
        start_idx=vis_end,
    )
    if len(candidates) < 2:
        return None

    pair_samples = 2.0 * line_samples
    tolerance = 0.25  # ±25 %, matching walk_sync_grid's slack

    # Median of consecutive diffs — resilient to an occasional missed
    # or spurious candidate, unlike mean.
    diffs = np.diff(np.asarray(candidates, dtype=np.float64))
    if diffs.size == 0:
        return None
    median_diff = float(np.median(diffs))

    if abs(median_diff - line_samples) <= line_samples * tolerance:
        line_starts = slant_corrected_line_starts(
            candidates, line_samples, spec.height
        )
        if len(line_starts) < spec.height:
            return None
        # Truncation guard: see the matching comment in ``decode_wav``.
        if line_starts[-1] >= inst.size:
            return None
        return _decode_robot36(inst, fs, spec, line_starts)

    if abs(median_diff - pair_samples) <= pair_samples * tolerance:
        super_starts = slant_corrected_line_starts(
            candidates, pair_samples, spec.height // 2
        )
        if len(super_starts) < spec.height // 2:
            return None
        if super_starts[-1] >= inst.size:
            return None
        return _decode_robot36_line_pair(inst, fs, spec, super_starts)

    return None


# === per-mode pixel decoders ===


def _decode_robot36(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Robot 36 YCbCr image (per-line format).

    Delegates per-pixel sampling and YCbCr→RGB to the slowrx port in
    ``core.robot36_dsp``: single-sample-with-5-sample-mean reads (no
    chroma floor, no right-edge guard), then slowrx's integer matrix
    for the colour conversion.  See that module's docstring for why
    this is not the shared ``_sample_pixels`` path that every other
    mode uses.

    ``line_starts`` is the list of sample indices where each scan line
    begins (one per image row, ``spec.height`` long).
    """
    width = spec.width
    height = spec.height

    # PySSTV-derived per-segment durations (ms). Cross-checked above.
    sync_ms = 9.0
    sync_porch_ms = 3.0
    y_scan_ms = 88.0
    inter_ch_gap_ms = 4.5
    porch_ms = 1.5
    c_scan_ms = 44.0

    y_offset_samples = (sync_ms + sync_porch_ms) / 1000.0 * fs
    c_offset_samples = (
        (sync_ms + sync_porch_ms + y_scan_ms + inter_ch_gap_ms + porch_ms)
        / 1000.0
        * fs
    )
    y_scan_samples = y_scan_ms / 1000.0 * fs
    c_scan_samples = c_scan_ms / 1000.0 * fs

    y_plane = np.zeros((height, width), dtype=np.uint8)
    # Cb/Cr neutral midpoint is 128 — initializing to 0 would produce a
    # green tint on any unsampled pixels after YCbCr→RGB conversion.
    cb_plane = np.full((height, width), 128, dtype=np.uint8)
    cr_plane = np.full((height, width), 128, dtype=np.uint8)

    n = inst.size
    for row, line_start in enumerate(line_starts):
        if cancel is not None and cancel.is_set():
            return None
        y_start = line_start + y_offset_samples
        c_start = line_start + c_offset_samples
        y_plane[row] = _robot36_sample_scan(inst, y_start, y_scan_samples, width, n)
        c_row = _robot36_sample_scan(inst, c_start, c_scan_samples, width, n)
        # PySSTV: even lines (row%2 == 0) carry Cr, odd lines carry Cb.
        if row % 2 == 0:
            cr_plane[row] = c_row
        else:
            cb_plane[row] = c_row

    # Nearest-neighbour chroma upsample: each line carries only one of
    # Cr / Cb, so copy the missing component from the adjacent line.
    # slowrx's model — we experimented with linear interpolation between
    # rows in the incremental decoder and found it added complexity
    # without meaningful image-quality gain.
    for row in range(height):
        if row % 2 == 0:
            src = row + 1 if row + 1 < height else row - 1
            cb_plane[row] = cb_plane[src]
        else:
            src = row - 1 if row - 1 >= 0 else row + 1
            cr_plane[row] = cr_plane[src]

    rgb = _robot36_ycbcr_to_rgb(y_plane, cb_plane, cr_plane)
    # ``mode=`` omitted: Pillow 13 deprecates it; autodetection on a
    # (H, W, 3) uint8 array produces RGB, which is what we want.
    return Image.fromarray(rgb)


def _decode_robot36_line_pair(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    super_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a canonical-broadcast Robot 36 frequency track into a YCbCr image.

    In this layout (used by SimpleSSTV iOS, MMSSTV, and most over-the-air
    encoders) one sync pulse covers two image rows. The per-super-line
    layout, measured against the leading edge of the sync, is::

        SYNC (9 ms)
        SYNC PORCH (3 ms)
        Y_even (88 ms)
        EVEN SEP (4.5 ms)
        COLOR PORCH (1.5 ms)
        Cr (44 ms)              [shared by the two rows in this pair]
        SYNC PORCH (3 ms)       [no sync — just a settling porch]
        Y_odd (88 ms)
        ODD SEP (4.5 ms)
        COLOR PORCH (1.5 ms)
        Cb (44 ms)              [shared by the two rows in this pair]

    Total: 290.5 ms. ``super_starts`` contains ``spec.height // 2``
    indices, one per pair; we produce two Y rows per super-line and
    copy each chroma pair across both rows (nearest-neighbor subsampling,
    matching slowrx).  Sampling and YCbCr→RGB both run through
    ``core.robot36_dsp`` — see the per-line decoder above for rationale.
    """
    width = spec.width
    height = spec.height

    sync_ms = 9.0
    sync_porch_ms = 3.0
    y_scan_ms = 88.0
    inter_ch_gap_ms = 4.5
    porch_ms = 1.5
    c_scan_ms = 44.0

    y0_offset_ms = sync_ms + sync_porch_ms
    cr_offset_ms = y0_offset_ms + y_scan_ms + inter_ch_gap_ms + porch_ms
    y1_offset_ms = cr_offset_ms + c_scan_ms + sync_porch_ms
    cb_offset_ms = y1_offset_ms + y_scan_ms + inter_ch_gap_ms + porch_ms

    y0_off = y0_offset_ms / 1000.0 * fs
    cr_off = cr_offset_ms / 1000.0 * fs
    y1_off = y1_offset_ms / 1000.0 * fs
    cb_off = cb_offset_ms / 1000.0 * fs
    y_span = y_scan_ms / 1000.0 * fs
    c_span = c_scan_ms / 1000.0 * fs

    y_plane = np.zeros((height, width), dtype=np.uint8)
    cb_plane = np.full((height, width), 128, dtype=np.uint8)
    cr_plane = np.full((height, width), 128, dtype=np.uint8)

    n = inst.size
    for pair_idx, sup in enumerate(super_starts):
        if cancel is not None and cancel.is_set():
            return None
        row_even = pair_idx * 2
        row_odd = row_even + 1
        if row_odd >= height:
            break
        y_plane[row_even] = _robot36_sample_scan(inst, sup + y0_off, y_span, width, n)
        y_plane[row_odd] = _robot36_sample_scan(inst, sup + y1_off, y_span, width, n)
        cr_row = _robot36_sample_scan(inst, sup + cr_off, c_span, width, n)
        cb_row = _robot36_sample_scan(inst, sup + cb_off, c_span, width, n)
        # Both rows in the pair share the pair's chroma (nearest-neighbor
        # upsample from the 320×(height/2) chroma grid).
        cr_plane[row_even] = cr_row
        cr_plane[row_odd] = cr_row
        cb_plane[row_even] = cb_row
        cb_plane[row_odd] = cb_row

    rgb = _robot36_ycbcr_to_rgb(y_plane, cb_plane, cr_plane)
    # ``mode=`` omitted: Pillow 13 deprecates it; autodetection on a
    # (H, W, 3) uint8 array produces RGB, which is what we want.
    return Image.fromarray(rgb)


def _decode_martin_rgb(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Martin-family RGB image.

    Martin layout (LINE_START, ``color_layout=("G","B","R")``):
    one sync pulse at the line start, followed by four equal porches
    interleaved with three channel scans in G→B→R order::

        SYNC → PORCH → G_scan → PORCH → B_scan → PORCH → R_scan → PORCH

    Scan time is derived from the spec:
    ``scan_ms = (line_time − sync − 4×porch) / 3``

    Handles M1 (320×256, ~114 s) and M2 (160×256, ~57 s) — any Martin
    variant whose ModeSpec is structured this way.
    """
    width = spec.width
    height = spec.height
    sync_ms = spec.sync_pulse_ms
    porch_ms = spec.sync_porch_ms
    scan_ms = (spec.line_time_ms - sync_ms - 4 * porch_ms) / 3

    g_offset_ms = sync_ms + porch_ms
    b_offset_ms = g_offset_ms + scan_ms + porch_ms
    r_offset_ms = b_offset_ms + scan_ms + porch_ms

    g_offset = g_offset_ms / 1000.0 * fs
    b_offset = b_offset_ms / 1000.0 * fs
    r_offset = r_offset_ms / 1000.0 * fs
    scan_samples = scan_ms / 1000.0 * fs

    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    n = inst.size
    for row, line_start in enumerate(line_starts):
        if cancel is not None and cancel.is_set():
            return None
        rgb[row, :, 1] = _sample_pixels(
            inst, line_start + g_offset, scan_samples, width, n
        )
        rgb[row, :, 2] = _sample_pixels(
            inst, line_start + b_offset, scan_samples, width, n
        )
        rgb[row, :, 0] = _sample_pixels(
            inst, line_start + r_offset, scan_samples, width, n
        )

    return Image.fromarray(rgb)


def _decode_scottie_rgb(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    sync_indices: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Scottie-family RGB image.

    Scottie's defining oddity: the SYNC pulse sits **between blue and
    red** within each line (``BEFORE_RED``). Layout::

        PORCH → G_scan → PORCH → PORCH → B_scan → PORCH → SYNC → PORCH → R_scan → PORCH

    Each per-line index lands on the leading edge of the between-B-and-R
    sync. The decoder steps backward to reach G and B, forward for R.

    Scan time: ``scan_ms = (line_time − sync − 6×porch) / 3``

    Handles S1 (320×256, ~110 s), S2 (160×256, ~71 s), and DX
    (320×256, ~269 s).
    """
    width = spec.width
    height = spec.height
    sync_ms = spec.sync_pulse_ms
    porch_ms = spec.sync_porch_ms
    scan_ms = (spec.line_time_ms - sync_ms - 6 * porch_ms) / 3

    # Offsets relative to the detected SYNC position.
    g_offset_ms = -(porch_ms + scan_ms + porch_ms + porch_ms + scan_ms)
    b_offset_ms = -(porch_ms + scan_ms)
    r_offset_ms = sync_ms + porch_ms

    g_offset = g_offset_ms / 1000.0 * fs
    b_offset = b_offset_ms / 1000.0 * fs
    r_offset = r_offset_ms / 1000.0 * fs
    scan_samples = scan_ms / 1000.0 * fs

    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    n = inst.size
    for row, sync_idx in enumerate(sync_indices):
        if cancel is not None and cancel.is_set():
            return None
        rgb[row, :, 1] = _sample_pixels(
            inst, sync_idx + g_offset, scan_samples, width, n
        )
        rgb[row, :, 2] = _sample_pixels(
            inst, sync_idx + b_offset, scan_samples, width, n
        )
        rgb[row, :, 0] = _sample_pixels(
            inst, sync_idx + r_offset, scan_samples, width, n
        )

    return Image.fromarray(rgb)


def _decode_wraase_rgb(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Wraase SC2-family RGB image.

    Wraase SC2 layout (LINE_START, ``color_layout=("R","G","B")``):
    one sync pulse, a single porch before the first channel only, then
    three back-to-back channel scans with no inter-channel gaps::

        SYNC → PORCH → R_scan → G_scan → B_scan

    Scan time: ``scan_ms = (line_time − sync − porch) / 3``

    Handles SC2-120 (320×256, ~122 s) and SC2-180 (320×256, ~183 s).
    """
    width = spec.width
    height = spec.height
    sync_ms = spec.sync_pulse_ms
    porch_ms = spec.sync_porch_ms
    scan_ms = (spec.line_time_ms - sync_ms - porch_ms) / 3

    r_offset_ms = sync_ms + porch_ms
    g_offset_ms = r_offset_ms + scan_ms
    b_offset_ms = g_offset_ms + scan_ms

    r_offset = r_offset_ms / 1000.0 * fs
    g_offset = g_offset_ms / 1000.0 * fs
    b_offset = b_offset_ms / 1000.0 * fs
    scan_samples = scan_ms / 1000.0 * fs

    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    n = inst.size
    for row, line_start in enumerate(line_starts):
        if cancel is not None and cancel.is_set():
            return None
        rgb[row, :, 0] = _sample_pixels(
            inst, line_start + r_offset, scan_samples, width, n
        )
        rgb[row, :, 1] = _sample_pixels(
            inst, line_start + g_offset, scan_samples, width, n
        )
        rgb[row, :, 2] = _sample_pixels(
            inst, line_start + b_offset, scan_samples, width, n
        )

    return Image.fromarray(rgb)


def _decode_pasokon_rgb(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Pasokon-family RGB image.

    Pasokon layout (LINE_START, ``color_layout=("R","G","B")``):
    one sync pulse followed by four equal inter-channel gaps (one before
    each channel and one trailing)::

        SYNC → GAP → R_scan → GAP → G_scan → GAP → B_scan → GAP

    ``spec.sync_porch_ms`` holds the inter-channel gap value.
    Scan time: ``scan_ms = (line_time − sync − 4×gap) / 3``

    Handles P3 (~203 s), P5 (~304 s), and P7 (~406 s), all 640×496.
    """
    width = spec.width
    height = spec.height
    sync_ms = spec.sync_pulse_ms
    gap_ms = spec.sync_porch_ms   # stored as sync_porch_ms per modes.py convention
    scan_ms = (spec.line_time_ms - sync_ms - 4 * gap_ms) / 3

    r_offset_ms = sync_ms + gap_ms
    g_offset_ms = r_offset_ms + scan_ms + gap_ms
    b_offset_ms = g_offset_ms + scan_ms + gap_ms

    r_offset = r_offset_ms / 1000.0 * fs
    g_offset = g_offset_ms / 1000.0 * fs
    b_offset = b_offset_ms / 1000.0 * fs
    scan_samples = scan_ms / 1000.0 * fs

    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    n = inst.size
    for row, line_start in enumerate(line_starts):
        if cancel is not None and cancel.is_set():
            return None
        rgb[row, :, 0] = _sample_pixels(
            inst, line_start + r_offset, scan_samples, width, n
        )
        rgb[row, :, 1] = _sample_pixels(
            inst, line_start + g_offset, scan_samples, width, n
        )
        rgb[row, :, 2] = _sample_pixels(
            inst, line_start + b_offset, scan_samples, width, n
        )

    return Image.fromarray(rgb)


def _decode_pd(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    super_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a PD-family YCbCr image.

    PD layout (LINE_START, ``color_layout=("Y0","Cr","Cb","Y1")``):
    one sync pulse covers **two** image rows (line-pair format). Each
    super-line contains four equal-length channel scans::

        SYNC → PORCH → Y0_scan → Cr_scan → Cb_scan → Y1_scan

    ``spec.height`` is ``actual_image_height // 2`` (number of sync pulses).
    The output image is ``spec.width × (spec.height * 2)`` pixels.

    Channel scan time: ``ch_ms = (line_time − sync − porch) / 4``

    Handles PD 90/120/160/180/240/290.
    """
    width = spec.width
    n_pairs = spec.height            # stored as actual_height // 2
    height = n_pairs * 2             # true image height

    sync_ms = spec.sync_pulse_ms
    porch_ms = spec.sync_porch_ms
    ch_ms = (spec.line_time_ms - sync_ms - porch_ms) / 4

    y0_off_ms = sync_ms + porch_ms
    cr_off_ms = y0_off_ms + ch_ms
    cb_off_ms = cr_off_ms + ch_ms
    y1_off_ms = cb_off_ms + ch_ms

    y0_off = y0_off_ms / 1000.0 * fs
    cr_off = cr_off_ms / 1000.0 * fs
    cb_off = cb_off_ms / 1000.0 * fs
    y1_off = y1_off_ms / 1000.0 * fs
    ch_span = ch_ms / 1000.0 * fs

    y_plane = np.zeros((height, width), dtype=np.uint8)
    cr_plane = np.full((height, width), 128, dtype=np.uint8)
    cb_plane = np.full((height, width), 128, dtype=np.uint8)

    n = inst.size
    for pair_idx, sup in enumerate(super_starts):
        if cancel is not None and cancel.is_set():
            return None
        row_even = pair_idx * 2
        row_odd = row_even + 1
        if row_odd >= height:
            break
        y_plane[row_even] = _sample_pixels(inst, sup + y0_off, ch_span, width, n)
        y_plane[row_odd] = _sample_pixels(inst, sup + y1_off, ch_span, width, n)
        cr_row = _sample_pixels(inst, sup + cr_off, ch_span, width, n, chroma=True)
        cb_row = _sample_pixels(inst, sup + cb_off, ch_span, width, n, chroma=True)
        # Both rows in the pair share the pair's chroma (nearest-neighbour upsample).
        cr_plane[row_even] = cr_row
        cr_plane[row_odd] = cr_row
        cb_plane[row_even] = cb_row
        cb_plane[row_odd] = cb_row

    ycbcr = np.stack([y_plane, cb_plane, cr_plane], axis=-1)
    return Image.frombytes("YCbCr", (width, height), ycbcr.tobytes()).convert("RGB")


def _bandpass(x: NDArray, fs: int) -> NDArray:
    """Zero-phase bandpass to the SSTV signalling band.

    Returns the input unchanged if ``fs`` is too low to support the
    chosen band edges (guard against pathological test inputs), or if
    the buffer is shorter than ``sosfiltfilt`` can stably pad. All
    other cases run a Butterworth order-4 forward/back filter.
    """
    if x.size < _BANDPASS_MIN_SAMPLES:
        return x
    try:
        sos = bandpass_sos(
            _BANDPASS_LOW_HZ, _BANDPASS_HIGH_HZ, fs, order=_BANDPASS_ORDER
        )
    except ValueError:
        # fs too low for the chosen band edges — skip filtering rather
        # than blowing up the decode path with a construction error.
        return x
    return sosfiltfilt(sos, x)


def _sample_pixels(
    inst: NDArray,
    start: float,
    span_samples: float,
    width: int,
    track_len: int,
    *,
    chroma: bool = False,
) -> NDArray[np.uint8]:
    """Slice a span of the frequency track into ``width`` pixel medians.

    Walks ``width`` evenly-spaced sub-windows over ``[start, start+span)``,
    takes the median frequency in each window's central 60% (matching the
    bit-window dodging trick from ``vis.detect_vis``), and maps to a uint8
    luma. Returns zeros for pixel windows that fall outside the buffer.

    When ``chroma=True`` the default and sub-black-level value is 128
    (the neutral YCbCr midpoint) instead of 0, preventing the bright-green
    fringe that Robot 36 produces when edge pixels sample from the
    sync/porch region at ~1200-1500 Hz.

    Median is more robust to filter ringing at sub-window boundaries than
    a plain mean, and dramatically faster than the per-sample interpolation
    slowrx uses (which we don't need until we add slant correction).
    """
    neutral: int = 128 if chroma else 0
    out = np.full(width, neutral, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    margin = pixel_span * 0.2  # central 60% of each pixel window
    span_lo = SSTV_BLACK_HZ
    span_hi = SSTV_WHITE_HZ
    span_range = span_hi - span_lo
    # Chroma guard: two defences against the green fringe that Robot 36's
    # YCbCr→RGB conversion produces when edge chroma pixels sample from
    # the adjacent porch/sync region.
    #
    # 1. Right-edge pixel guard — the last 1 % of columns (≈3 px at
    #    width 320) are left at neutral-128 rather than sampled, because
    #    their windows inevitably straddle the scan/porch boundary.
    # 2. Frequency floor — any sampled frequency below ~1620 Hz is
    #    replaced with neutral. The 0.15 threshold maps to chroma value
    #    38/255, which is nearly indistinguishable from grey.
    chroma_floor = 0.15 if chroma else 0.0
    guard_pixels = max(3, width // 80) if chroma else 0  # ~1.25 %
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
        # Linear map 1500..2300 → 0..255 with clipping. Inlined to keep
        # this hot loop a single pass over the array.
        norm = (freq - span_lo) / span_range
        if norm < chroma_floor:
            out[col] = neutral
            continue
        elif norm > 1.0:
            norm = 1.0
        out[col] = int(round(norm * 255.0))
    return out


_PIXEL_DECODERS: dict[Mode, Callable[..., Image.Image | None]] = {
    # Robot 36 is dispatched separately in decode_wav/_partial_decode
    # (auto-detects single-line vs line-pair wire format), but register
    # the per-line decoder here so the dict stays complete.
    Mode.ROBOT_36: _decode_robot36,
    # Martin family
    Mode.MARTIN_M1: _decode_martin_rgb,
    Mode.MARTIN_M2: _decode_martin_rgb,
    Mode.MARTIN_M3: _decode_martin_rgb,
    Mode.MARTIN_M4: _decode_martin_rgb,
    # Scottie family
    Mode.SCOTTIE_S1: _decode_scottie_rgb,
    Mode.SCOTTIE_S2: _decode_scottie_rgb,
    Mode.SCOTTIE_DX: _decode_scottie_rgb,
    Mode.SCOTTIE_S3: _decode_scottie_rgb,
    Mode.SCOTTIE_S4: _decode_scottie_rgb,
    # PD family (YCbCr line-pair)
    Mode.PD_50: _decode_pd,
    Mode.PD_90: _decode_pd,
    Mode.PD_120: _decode_pd,
    Mode.PD_160: _decode_pd,
    Mode.PD_180: _decode_pd,
    Mode.PD_240: _decode_pd,
    Mode.PD_290: _decode_pd,
    # Wraase SC2 family
    Mode.WRAASE_SC2_120: _decode_wraase_rgb,
    Mode.WRAASE_SC2_180: _decode_wraase_rgb,
    # Pasokon family
    Mode.PASOKON_P3: _decode_pasokon_rgb,
    Mode.PASOKON_P5: _decode_pasokon_rgb,
    Mode.PASOKON_P7: _decode_pasokon_rgb,
}

# Fail loudly at import time if a Mode was added to the enum without a
# corresponding pixel decoder entry, rather than silently returning None
# when a user tries to receive that mode.
# Note: ROBOT_36 IS in the table (uses _decode_robot36 as a fallback);
# the main decode path auto-detects single-line vs line-pair and dispatches
# directly, but having it here keeps the completeness check simple.
_missing_decoder = set(Mode) - set(_PIXEL_DECODERS)
assert not _missing_decoder, (
    f"Decoder missing for Mode(s): {_missing_decoder}. "
    "Add an entry to _PIXEL_DECODERS in decoder.py."
)


# === partial (progressive) decode helpers ===


def _trim_to_buffer(line_starts: list[int], buf_size: int) -> list[int]:
    """Keep only line starts that fall within the audio buffer.

    ``slant_corrected_line_starts`` happily projects positions beyond the
    end of the frequency track. For progressive decode we need to discard
    those so the pixel decoders only touch samples that exist (out-of-range
    pixel windows in ``_sample_pixels`` already return zeros, but counting
    them as "decoded lines" would over-report progress).
    """
    return [ls for ls in line_starts if 0 <= ls < buf_size]


def _partial_decode(
    mode: Mode,
    spec: ModeSpec,
    inst: "NDArray",
    fs: int,
    vis_end: int,
    *,
    cancel: threading.Event | None = None,
) -> tuple[Image.Image, int, int] | None:
    """Decode as many lines as currently available from a frequency track.

    Returns ``(image, lines_decoded, lines_total)`` or ``None`` if no
    sync candidates are usable yet or if ``cancel`` is set. The image is
    always full-size (``spec.width × spec.height``) with black rows for
    undecoded lines.

    **Progressive decode must NOT apply slant correction here.** Slant
    correction (``slant_corrected_line_starts``) fits a least-squares line
    through all currently-detected sync positions.  As more candidates
    arrive in later flushes the fit changes, which shifts the projected
    positions of already-decoded lines — the top rows appear clean, then
    "break" a few seconds later when the slant parameters update (D-3).

    Instead we use ``walk_sync_grid``, whose anchor-and-walk algorithm
    produces identical positions for already-confirmed lines regardless of
    how many additional candidates arrive later.  Slant correction is
    applied by the final one-shot re-decode in ``RxWorker._dispatch``
    (via ``decode_wav``), so the saved image still benefits from it.
    """
    if mode == Mode.ROBOT_36:
        return _partial_decode_robot36(inst, fs, spec, vis_end, cancel=cancel)

    line_samples = spec.line_time_ms / 1000.0 * fs
    candidates = find_sync_candidates(
        inst,
        fs,
        spec.sync_pulse_ms,
        line_period_samples=line_samples,
        start_idx=vis_end,
    )
    if cancel is not None and cancel.is_set():
        return None
    # Use walk_sync_grid (not slant_corrected_line_starts) so positions are
    # stable across progressive flushes — see docstring for the full rationale.
    line_starts = walk_sync_grid(candidates, line_samples, spec.height)
    usable = _trim_to_buffer(line_starts, inst.size)
    if not usable:
        return None

    decoder_fn = _PIXEL_DECODERS.get(mode)
    if decoder_fn is None:
        return None

    image = decoder_fn(inst, fs, spec, usable, cancel=cancel)
    if image is None:
        return None
    return (image, len(usable), spec.height)


def _partial_decode_robot36(
    inst: "NDArray",
    fs: int,
    spec: ModeSpec,
    vis_end: int,
    *,
    cancel: threading.Event | None = None,
) -> tuple[Image.Image, int, int] | None:
    """Progressive Robot 36 decode with auto-format detection.

    Mirrors ``_decode_robot36_dispatch`` but returns partial images
    instead of ``None`` when fewer than ``spec.height`` lines are
    available.
    """
    line_samples = spec.line_time_ms / 1000.0 * fs
    candidates = find_sync_candidates(
        inst,
        fs,
        spec.sync_pulse_ms,
        line_period_samples=line_samples,
        start_idx=vis_end,
    )
    if len(candidates) < 2:
        return None

    pair_samples = 2.0 * line_samples
    tolerance = 0.25

    diffs = np.diff(np.asarray(candidates, dtype=np.float64))
    if diffs.size == 0:
        return None
    median_diff = float(np.median(diffs))
    if abs(median_diff - line_samples) <= line_samples * tolerance:
        # walk_sync_grid for position stability — see _partial_decode() docstring.
        line_starts = walk_sync_grid(candidates, line_samples, spec.height)
        usable = _trim_to_buffer(line_starts, inst.size)
        if not usable:
            return None
        image = _decode_robot36(inst, fs, spec, usable, cancel=cancel)
        if image is None:
            return None
        return (image, len(usable), spec.height)

    if abs(median_diff - pair_samples) <= pair_samples * tolerance:
        # walk_sync_grid for position stability — see _partial_decode() docstring.
        super_starts = walk_sync_grid(candidates, pair_samples, spec.height // 2)
        usable = _trim_to_buffer(super_starts, inst.size)
        if not usable:
            return None
        image = _decode_robot36_line_pair(inst, fs, spec, usable, cancel=cancel)
        if image is None:
            return None
        lines_decoded = min(len(usable) * 2, spec.height)
        return (image, lines_decoded, spec.height)

    return None


# === streaming wrapper ===

#: Maximum audio retained while hunting for a VIS header (IDLE state).
#: The complete VIS sequence (300 ms leader + 10 ms break + 300 ms
#: calibration + ~86 ms header bits) totals < 700 ms; 3 s is triple
#: headroom while keeping the rolling window under ~1.1 MB at 48 kHz.
_IDLE_WINDOW_S: float = 3.0


@dataclass(frozen=True, slots=True)
class ImageStarted:
    """Emitted when a VIS header is decoded and a mode is locked."""

    mode: Mode
    vis_code: int


@dataclass(frozen=True, slots=True)
class ImageProgress:
    """Emitted during progressive decode with a partial image.

    The ``image`` is full-size (mode-native width × height) with black
    rows for lines not yet decoded. ``lines_decoded / lines_total``
    gives the completion fraction for progress display.
    """

    image: Image.Image
    mode: Mode
    vis_code: int
    lines_decoded: int
    lines_total: int


@dataclass(frozen=True, slots=True)
class ImageComplete:
    """Emitted when a full image has been decoded from the buffered audio."""

    image: Image.Image
    mode: Mode
    vis_code: int


@dataclass(frozen=True, slots=True)
class DecodeError:
    """Emitted when the buffered audio contains a recognizable header
    but the per-mode decode failed (truncated buffer, unsupported mode)."""

    message: str


DecoderEvent = ImageStarted | ImageProgress | ImageComplete | DecodeError


class _DecoderState(Enum):
    """Internal state machine for progressive decode."""

    IDLE = "idle"  # Hunting for VIS header
    DECODING = "decoding"  # VIS locked, accumulating lines


class Decoder:
    """Streaming SSTV decoder with progressive image output.

    Buffers audio chunks via ``feed(samples)``; the UI worker calls
    ``feed`` from its own thread as audio arrives. Each ``feed`` call
    returns a (possibly empty) list of ``DecoderEvent`` objects so the
    caller can react without polling.

    The decoder is a two-state machine:

    * **IDLE** — hunting for a VIS header. Each ``feed`` runs bandpass +
      ``detect_vis`` on the growing buffer. On success, emits
      ``ImageStarted`` and transitions to DECODING.
    * **DECODING** — VIS is locked, accumulating scan lines. Each
      ``feed`` runs bandpass + demod + sync + partial pixel decode on the
      growing buffer. Emits ``ImageProgress`` as new lines become
      decodable (the image is full-size with black rows for undecoded
      lines). Emits ``ImageComplete`` when all lines are present, then
      auto-resets to IDLE so the next transmission is picked up without
      an explicit ``reset()`` call.
    """

    def __init__(
        self,
        fs: int,
        *,
        weak_signal: bool = False,
        incremental_decode: bool = True,
    ) -> None:
        if fs <= 0:
            raise ValueError(f"Sample rate must be positive (got {fs})")
        self._fs = fs
        self._weak_signal = weak_signal
        self._exp_incremental = incremental_decode
        self._buffer: list[np.ndarray] = []
        self._state = _DecoderState.IDLE
        # Set when VIS is detected (DECODING state):
        self._vis_code: int = 0
        self._mode: Mode | None = None
        self._spec: ModeSpec | None = None
        self._vis_end: int = 0
        self._last_lines: int = 0
        # Retained after a complete decode for a high-quality re-decode pass.
        self._last_complete_buffer: np.ndarray | None = None
        # Optional cancellation event — set from the GUI thread to interrupt
        # an in-flight decode.  The RxWorker owns the Event and wires it here.
        self._cancel: threading.Event | None = None
        # Incremental decoder instance — used when incremental_decode is True.
        self._incremental_dec: ScottieS1IncrementalDecoder | None = None
        # How many samples of joined[] have been fed to the incremental decoder.
        self._incremental_total_fed: int = 0

    @property
    def sample_rate(self) -> int:
        return self._fs

    def last_complete_buffer(self) -> np.ndarray | None:
        """Return the raw audio that produced the most recent ``ImageComplete``.

        Survives ``reset()`` so the caller can grab it after the event.
        Returns ``None`` before any image has been fully decoded.
        """
        return self._last_complete_buffer

    def consume_last_buffer(self) -> np.ndarray | None:
        """Return and clear the last-complete-buffer, freeing its memory.

        Use this instead of ``last_complete_buffer()`` when the caller
        is done with the raw audio after a re-decode pass.
        """
        buf = self._last_complete_buffer
        self._last_complete_buffer = None
        return buf

    def set_cancel_event(self, event: threading.Event | None) -> None:
        """Register a ``threading.Event`` that can interrupt in-flight decodes.

        Thread-safe: the event can be set from the GUI thread while ``feed``
        is executing on the worker thread.  When the event is set the decoder
        aborts at the next checkpoint (after bandpass, after Hilbert transform,
        after sync detection, or between pixel-decoder rows) and returns an
        empty event list.  Pass ``None`` to detach the event.
        """
        self._cancel = event

    def _is_cancelled(self) -> bool:
        """Return True if the registered cancel event has been set."""
        return self._cancel is not None and self._cancel.is_set()

    def feed(self, samples: "NDArray") -> list[DecoderEvent]:
        """Append a chunk of audio and return any decoder events it
        triggered. Safe to call from a worker thread.

        In IDLE state, hunts for a VIS header and emits ``ImageStarted``
        on success. In DECODING state, decodes available lines and emits
        ``ImageProgress`` for each new batch of lines, followed by
        ``ImageComplete`` when the image is fully decoded (which
        auto-resets to IDLE).
        """
        arr = np.asarray(samples, dtype=np.float64)
        if arr.ndim != 1:
            return [DecodeError(f"feed expected 1-D, got {arr.ndim}-D")]
        if arr.size > 0:
            self._buffer.append(arr)

        joined = self._joined()
        if joined.size == 0:
            return []

        if self._state == _DecoderState.IDLE:
            return self._feed_idle(joined)
        return self._feed_decoding(joined)

    def _feed_idle(self, joined: "NDArray") -> list[DecoderEvent]:
        """Hunt for a VIS header in the buffered audio."""
        filtered = _bandpass(joined, self._fs)
        vis_result = detect_vis(filtered, self._fs, weak_signal=self._weak_signal)
        if vis_result is None:
            # No VIS found yet. Trim the buffer to a rolling window so
            # that long listening sessions don't exhaust memory. The full
            # VIS sequence is < 700 ms; 3 s is ample headroom.
            max_samples = int(_IDLE_WINDOW_S * self._fs)
            if joined.size > max_samples:
                self._buffer = [joined[-max_samples:]]
            return []

        vis_code, vis_end = vis_result
        mode = mode_from_vis(vis_code)
        if mode is None:
            # Unknown VIS code is a noise false-positive — VIS 0x00 (all
            # zeros, even parity) is the most common case and can occur when
            # silence or loopback audio is misread. Drop samples up to the
            # detected header end, stay in IDLE, and keep hunting. Never
            # emit a DecodeError for this: false VIS detections are expected
            # on low-SNR or silent inputs and should not alarm the user.
            self._buffer = [joined[vis_end:]]
            return []

        spec = MODE_TABLE[mode]
        self._state = _DecoderState.DECODING
        self._vis_code = vis_code
        self._mode = mode
        self._spec = spec
        self._vis_end = vis_end
        self._last_lines = 0

        # Keep the full buffer including the VIS header so that the
        # high-quality single-pass re-decode in RxWorker._dispatch can
        # call decode_wav() on last_complete_buffer() and find the VIS.
        # (The IDLE rolling-window trim already caps pre-VIS audio at 3 s.)

        events: list[DecoderEvent] = [
            ImageStarted(mode=mode, vis_code=vis_code)
        ]

        # Incremental decode path — Scottie / Martin / PD.
        # Robot 36 returns None from the factory and falls through to batch.
        if self._exp_incremental:
            # Lazy import avoids a circular dependency at module load time.
            from open_sstv.core.incremental_decoder import (  # noqa: PLC0415
                make_incremental_decoder,
            )
            inc = make_incremental_decoder(
                spec, self._fs, vis_end_abs=vis_end, start_abs=0,
            )
            if inc is not None:
                self._incremental_dec = inc
                # Pre-feed audio before VIS so the first line's window has
                # the full FILTER_MARGIN of sosfiltfilt padding from the start.
                pre_vis = joined[:vis_end]
                if pre_vis.size > 0:
                    self._incremental_dec.feed(pre_vis)
                self._incremental_total_fed = vis_end
                # Process any post-VIS audio already in the buffer so that
                # callers who feed all samples in a single call still get
                # progress/complete events (same behaviour as the batch path).
                events.extend(self._feed_decoding_incremental(joined))
                return events

        # Batch path: try an immediate partial decode — the buffer may already
        # contain a few scan lines (or even a full image if the caller
        # fed a large chunk).
        inst = instantaneous_frequency(filtered, self._fs)
        progress_events = self._decode_progress(inst, mode, spec, vis_code)
        events.extend(progress_events)
        return events

    def _feed_decoding(self, joined: "NDArray") -> list[DecoderEvent]:
        """Decode available lines from the growing audio buffer."""
        if self._exp_incremental and self._incremental_dec is not None:
            return self._feed_decoding_incremental(joined)

        filtered = _bandpass(joined, self._fs)
        if self._is_cancelled():
            return []
        inst = instantaneous_frequency(filtered, self._fs)
        if self._is_cancelled():
            return []
        return self._decode_progress(
            inst, self._mode, self._spec, self._vis_code  # type: ignore[arg-type]
        )

    def _feed_decoding_incremental(self, joined: "NDArray") -> list[DecoderEvent]:
        """Incremental decode: feed only the new audio chunk to the streaming decoder.

        ``joined`` is the full accumulated buffer. We track how much of it has
        already been fed via ``_incremental_total_fed`` and pass only the tail.
        """
        if self._is_cancelled():
            return []
        assert self._incremental_dec is not None  # guarded by caller
        assert self._mode is not None
        assert self._spec is not None

        new_audio = joined[self._incremental_total_fed:]
        if new_audio.size == 0:
            return []
        self._incremental_total_fed = joined.size

        line_tuples = self._incremental_dec.feed(new_audio)

        events: list[DecoderEvent] = []
        image_height = self._incremental_dec.image_height
        for row_idx, _rgb in line_tuples:
            img = self._incremental_dec.get_image()
            events.append(
                ImageProgress(
                    image=img.copy(),
                    mode=self._mode,
                    vis_code=self._vis_code,
                    lines_decoded=row_idx + 1,
                    lines_total=image_height,
                )
            )

        if self._incremental_dec.complete:
            img = self._incremental_dec.get_image()
            self._last_complete_buffer = self._joined()
            events.append(
                ImageComplete(image=img, mode=self._mode, vis_code=self._vis_code)
            )
            self.reset()

        return events

    def _decode_progress(
        self,
        inst: "NDArray",
        mode: Mode,
        spec: ModeSpec,
        vis_code: int,
    ) -> list[DecoderEvent]:
        """Run partial decode and emit progress or completion events."""
        if self._is_cancelled():
            return []
        partial = _partial_decode(
            mode, spec, inst, self._fs, self._vis_end, cancel=self._cancel
        )
        if partial is None:
            return []

        image, lines, total = partial
        if lines <= self._last_lines:
            return []  # No new lines since last flush
        self._last_lines = lines

        if lines >= total:
            # Retain the raw buffer so callers can run a full-quality
            # single-pass re-decode via ``last_complete_buffer()``.
            self._last_complete_buffer = self._joined()
            events: list[DecoderEvent] = [
                ImageComplete(image=image, mode=mode, vis_code=vis_code)
            ]
            self.reset()
            return events
        return [
            ImageProgress(
                image=image,
                mode=mode,
                vis_code=vis_code,
                lines_decoded=lines,
                lines_total=total,
            )
        ]

    def reset(self) -> None:
        """Drop the buffered audio and reset to IDLE.

        Called automatically after a complete decode, or manually when
        the user clicks "Clear" or changes input device.
        """
        self._buffer.clear()
        self._state = _DecoderState.IDLE
        self._vis_code = 0
        self._mode = None
        self._spec = None
        self._vis_end = 0
        self._last_lines = 0
        self._incremental_dec = None
        self._incremental_total_fed = 0

    def _joined(self) -> "NDArray":
        if not self._buffer:
            return np.array([], dtype=np.float64)
        if len(self._buffer) == 1:
            return self._buffer[0]
        return np.concatenate(self._buffer)


__all__ = [
    "DecodedImage",
    "DecodeError",
    "Decoder",
    "DecoderEvent",
    "ImageComplete",
    "ImageProgress",
    "ImageStarted",
    "decode_wav",
]
