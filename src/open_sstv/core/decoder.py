# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level SSTV decoder.

This module is the buffered front-end of the receive pipeline. It owns:

* a small audio ring buffer (so chunked feeds can be re-windowed without
  callers having to track absolute sample positions),
* the high-level state machine (idle → searching for VIS → decoding image),
* the dispatch into per-mode pixel decoders.

Every mode in ``MODE_TABLE`` has a per-mode pixel decoder registered in
``_PIXEL_DECODERS``; the ``Decoder.feed`` plumbing is mode-agnostic.

Robot 36 is decoded in its one real wire format: a sync pulse on every
line (150 ms period), Y then one chroma scan per line, Cr on even lines
and Cb on odd lines — the Robot 36 spec as implemented by slowrx, MMSSTV,
QSSTV, PySSTV and our own encoder. Its pixels are sampled with the
slowrx port in ``core.robot36_dsp`` (shared with the live decoder), not
the windowed-median ``demod.sample_pixels`` every other mode uses.

Public API
----------

``decode_wav(samples, fs) -> DecodedImage | None``
    One-shot decode of an entire audio buffer. Returns the first complete
    image found, or ``None`` if no VIS or supported mode was detected.
    This is what the CLI and the round-trip tests call.

``Decoder``
    Streaming, pull-model API used by ``ui/workers.py``. Callers push
    audio chunks via ``feed(samples)`` and get back ``DecoderEvent``
    objects (``ImageStarted``, ``ImageProgress``, ``ImageComplete``).
    After VIS lock the per-line ``core.incremental_decoder`` backends do
    the work; a legacy batch path (``incremental_decode=False``) re-runs
    the partial decode over the whole buffer.

Line timing
-----------

``decode_wav`` finds raw sync candidates after the VIS, then fits a
least-squares line through them (``slant.slant_corrected_line_starts``)
to absorb TX/RX clock drift; Robot 36 uses the plain grid walk
(``sync.walk_sync_grid``) instead (OP2-15). Both are anchored on the
expected line-0 position from the VIS end, so a missed sync on line 0
or 1 doesn't shift the image (v0.6.14 audit D-H1).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from open_sstv.core import robot36_dsp
from open_sstv.core.demod import instantaneous_frequency
from open_sstv.core.demod import sample_pixels as _sample_pixels
from open_sstv.core.dsp_utils import sstv_bandpass as _bandpass
from open_sstv.core.modes import MODE_TABLE, Mode, ModeSpec, mode_from_vis
from open_sstv.core.robot36_dsp import (
    sample_scan as _robot36_sample_scan,
)
from open_sstv.core.robot36_dsp import (
    ycbcr_to_rgb as _robot36_ycbcr_to_rgb,
)
from open_sstv.core.slant import slant_corrected_line_starts
from open_sstv.core.sync import (
    find_sync_candidates,
    line0_sync_hint,
    walk_sync_grid,
)
from open_sstv.core.vis import detect_vis

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from open_sstv.core.incremental_decoder import IncrementalDecoder


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """A successfully decoded SSTV image plus the metadata callers need."""

    image: Image.Image
    mode: Mode
    vis_code: int


_log = logging.getLogger(__name__)


def _sanitize_audio(arr: NDArray) -> NDArray:
    """Replace non-finite samples (NaN / ±Inf) with silence.

    v0.4.0 audit high #3: glitchy drivers and virtual audio cables can
    hand the decoder NaN samples.  ``sosfiltfilt`` then smears them
    across the whole filter window and the pixel samplers crash on
    ``int(round(nan))`` — in the streaming decoder the offending line
    window was never consumed, so every subsequent ``feed()`` re-raised
    and RX was dead until a reset.  One ``isfinite`` pass per chunk at
    the two public entry points is cheap insurance; the per-sampler
    guards downstream are defence in depth.
    """
    if np.isfinite(arr).all():
        return arr
    _log.warning(
        "audio chunk contains non-finite samples (driver glitch?) — "
        "replacing with silence"
    )
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def decode_wav(
    samples: NDArray, fs: int
) -> DecodedImage | None:
    """Decode an audio buffer into a single SSTV image.

    Looks for the first VIS header, dispatches into the right per-mode
    decoder, and returns the resulting image. Returns ``None`` if no
    VIS was found or the VIS code isn't a mode in ``MODE_TABLE``.

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
    arr = _sanitize_audio(arr)

    # Bandpass-filter to the SSTV signalling band before demodulation.
    # Without this the Hilbert transform sees noise from DC to Nyquist,
    # which inflates FM-demod variance and makes the sync detector
    # collapse around 12 dB SNR. Zero-phase (``sosfiltfilt``) is
    # mandatory — any group delay would shift sync positions and corrupt
    # the per-line pixel slicing downstream. See ``dsp_utils`` for the
    # edge choices and the empirical measurements.
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
    line_starts = _line_grid(mode, spec, inst, fs, vis_end, slant=True)
    if len(line_starts) < spec.height:
        return None
    # Reject truncated buffers: slant projection happily extrapolates
    # past the end of ``inst`` and ``_sample_pixels`` silently fills
    # out-of-range pixels with neutral values, which would hand callers a
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


def _line_grid(
    mode: Mode,
    spec: ModeSpec,
    inst: NDArray,
    fs: int,
    vis_end: int,
    *,
    slant: bool,
) -> list[int]:
    """Sync candidates after ``vis_end`` → per-line start positions.

    ``slant=True`` fits a least-squares line through the candidates
    (one-shot decodes); ``slant=False`` walks the grid, whose positions
    for already-confirmed lines never move as more audio arrives
    (progressive decodes — see ``_partial_decode``). Robot 36 always
    walks (OP2-15: its batch result must match the live decoder, which
    never slant-fits). Both are anchored on the line-0 position implied
    by the VIS end, so a missed line-0/1 sync is extrapolated back rather
    than shifting the whole image up (v0.6.14 audit D-H1).
    """
    line_samples = spec.line_time_ms / 1000.0 * fs
    candidates = find_sync_candidates(
        inst,
        fs,
        spec.sync_pulse_ms,
        line_period_samples=line_samples,
        start_idx=vis_end,
    )
    hint = line0_sync_hint(spec, fs, vis_end)
    if slant and mode != Mode.ROBOT_36:
        return slant_corrected_line_starts(
            candidates, line_samples, spec.height, first_slot_hint=hint
        )
    return walk_sync_grid(
        candidates, line_samples, spec.height, first_slot_hint=hint
    )


# === per-mode pixel decoders ===


def _decode_robot36(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
    *,
    cancel: threading.Event | None = None,
) -> Image.Image | None:
    """Slice a frequency track into a Robot 36 YCbCr image.

    Delegates per-pixel sampling and YCbCr→RGB to the slowrx port in
    ``core.robot36_dsp`` (the same code the live decoder runs):
    single-sample-with-5-sample-mean reads (no chroma floor, no
    right-edge guard), then slowrx's integer matrix for the colour
    conversion.  See that module's docstring for why this is not the
    shared ``_sample_pixels`` path that every other mode uses.

    ``line_starts`` is the list of sample indices where each scan line
    begins (one per image row, ``spec.height`` long).
    """
    width = spec.width
    height = spec.height

    y_offset_samples = robot36_dsp.Y_OFFSET_MS / 1000.0 * fs
    c_offset_samples = robot36_dsp.C_OFFSET_MS / 1000.0 * fs
    y_scan_samples = robot36_dsp.Y_SCAN_MS / 1000.0 * fs
    c_scan_samples = robot36_dsp.C_SCAN_MS / 1000.0 * fs

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
        # Even lines (row%2 == 0) carry Cr (R-Y), odd lines carry Cb (B-Y).
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

    Handles M1 (320×256, ~114 s), M2 (320×256, ~58 s) and the 320×128
    M3 / M4 — any Martin variant whose ModeSpec is structured this way.
    M2 and M4 differ from M1 and M3 only in scan time: they run the same
    320 columns at half the pixel dwell, so nothing here special-cases
    them; ``scan_ms`` falls out of the spec's ``line_time_ms``.
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
    red** within each line (``BEFORE_RED``). Spec layout (v0.6.14 audit
    D-L3; see ``modes.py``)::

        SEP → G_scan → SEP → B_scan → SYNC → PORCH → R_scan

    Each per-line index lands on the leading edge of the between-B-and-R
    sync. The decoder steps backward to reach G and B, forward for R.

    Scan time: ``scan_ms = (line_time − sync − 3×sep) / 3``

    Handles S1 (320×256, ~110 s), S2 (320×256, ~71 s), DX (320×256,
    ~269 s), and the 320×128 S3 / S4.  As with Martin, the half-time
    variants are the same width at half the pixel dwell.
    """
    width = spec.width
    height = spec.height
    sync_ms = spec.sync_pulse_ms
    porch_ms = spec.sync_porch_ms
    scan_ms = (spec.line_time_ms - sync_ms - 3 * porch_ms) / 3

    # Offsets relative to the detected SYNC position. (Channel *starts*
    # are where PySSTV's old split put them too, so pre-v0.6.15 Open-SSTV
    # audio still decodes — minus ~3.5 px of porch at each channel end.)
    g_offset_ms = -(scan_ms + porch_ms + scan_ms)
    b_offset_ms = -scan_ms
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


_PIXEL_DECODERS: dict[Mode, Callable[..., Image.Image | None]] = {
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
    inst: NDArray,
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
    The walk is anchored on the VIS end (``first_slot_hint``), so a
    missed line-0/1 sync no longer shifts the image (audit D-H1).
    """
    line_starts = _line_grid(mode, spec, inst, fs, vis_end, slant=False)
    if cancel is not None and cancel.is_set():
        return None
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
      ``detect_vis`` on the rolling window. On success, emits
      ``ImageStarted`` and transitions to DECODING.
    * **DECODING** — VIS is locked, accumulating scan lines.

      *Default path* (``incremental_decode=True``): each ``feed`` passes
      only the new chunk to the per-mode ``IncrementalDecoder`` (O(chunk);
      the retained audio is just appended to a list for the re-decode
      snapshot).
      The decoder emits one ``ImageProgress`` per newly completed row,
      so the image appears line-by-line in O(1 line period) per flush.
      All 22 supported modes use this path.

      *Batch fallback* (``incremental_decode=False``): each ``feed``
      reruns bandpass + demod + sync + pixel decode over the full
      growing buffer. CPU cost is O(N) per flush → O(N²) total.

      In both cases the decoder emits ``ImageComplete`` when all scan
      lines are present, then auto-resets to IDLE and re-feeds whatever
      audio followed the image (so back-to-back images in one buffer all
      decode).
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
        self._incremental_decode = incremental_decode
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
        # Running sample count of ``_buffer`` (v0.6.14 audit D-M1: so the
        # hot path never has to concatenate or re-sum the chunk list).
        self._buffered: int = 0
        # Incremental decoder instance — used when incremental_decode is True.
        # Annotated against the IncrementalDecoder Protocol so mypy accepts
        # any concrete backend (Scottie, Martin, PD, Wraase, Pasokon, Robot 36).
        self._incremental_dec: IncrementalDecoder | None = None
        # High-water mark for lines_decoded; prevents backward progress events
        # from Robot 36 per-line back-fill re-emissions.
        self._incremental_max_row: int = 0
        # v0.4.0 audit medium #10: set when the DECODING-state buffer cap
        # trimmed retained audio — the full-fidelity re-decode buffer is
        # then incomplete and must not be offered at completion.
        self._retained_buffer_trimmed: bool = False

    @property
    def sample_rate(self) -> int:
        return self._fs

    def _enforce_decoding_buffer_cap(self) -> None:
        """Bound the DECODING-state buffer on the incremental path.

        v0.4.0 audit medium #10 (reproduced): a VIS lock followed by
        dead carrier grew the retained buffer without limit — ~2.8 GB
        per hour for a headless caller of the streaming API — with an
        O(N²) ``_joined()`` concat on top.  The retained audio's only
        DECODING-state consumer is the full-fidelity re-decode snapshot
        at completion, so past a generous cap (mode duration × 1.5 +
        10 s, mirroring the RX watchdog's total-elapsed budget) we drop
        the head and simply withhold that snapshot for this image.

        The legacy batch path (``incremental_decode=False``) re-decodes
        from the VIS on every flush and structurally needs the whole
        buffer — it stays uncapped and remains bounded by the caller's
        watchdog, as before.
        """
        if (
            self._state != _DecoderState.DECODING
            or not self._incremental_decode
            or self._incremental_dec is None
            or self._spec is None
        ):
            return
        cap = int((self._spec.total_duration_s * 1.5 + 10.0) * self._fs)
        if self._buffered <= cap:
            return
        self._set_buffer(self._joined()[self._buffered - cap:])
        if not self._retained_buffer_trimmed:
            self._retained_buffer_trimmed = True
            _log.warning(
                "decode buffer exceeded %.0f s cap (stalled decode?) — "
                "trimming; the full-quality re-decode pass will be "
                "unavailable for this image",
                cap / self._fs,
            )

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

    def feed(self, samples: NDArray) -> list[DecoderEvent]:
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
            arr = _sanitize_audio(arr)
            self._buffer.append(arr)
            self._buffered += arr.size
            self._enforce_decoding_buffer_cap()

        if self._buffered == 0:
            return []

        if self._state == _DecoderState.IDLE:
            return self._feed_idle(self._joined())
        if self._incremental_decode and self._incremental_dec is not None:
            # v0.6.14 audit D-M1: the incremental backend only ever needs
            # the new chunk. This used to re-concatenate the whole retained
            # buffer on every 0.1 s feed (O(N²); ~31 ms + 100 MB of
            # allocation per feed late in a PD-290 / P7 image) only to
            # slice the tail back off it.
            return self._feed_decoding_incremental(arr)
        return self._feed_decoding(self._joined())

    def _feed_idle(self, joined: NDArray) -> list[DecoderEvent]:
        """Hunt for a VIS header in the buffered audio."""
        filtered = _bandpass(joined, self._fs)
        vis_result = detect_vis(filtered, self._fs, weak_signal=self._weak_signal)
        if vis_result is None:
            # No VIS found yet. Trim the buffer to a rolling window so
            # that long listening sessions don't exhaust memory. The full
            # VIS sequence is < 700 ms; 3 s is ample headroom.
            max_samples = int(_IDLE_WINDOW_S * self._fs)
            if joined.size > max_samples:
                self._set_buffer(joined[-max_samples:])
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
            #
            # M15: keep a small preamble window (200 ms) before the
            # rejected vis_end so a genuine VIS arriving tightly behind
            # the noise spike (e.g. signal coming up mid-preamble while a
            # false detection has just resolved) is still discoverable on
            # the next feed.  Trimming exactly to vis_end was swallowing
            # the leading 1900 Hz leader tone of a real VIS that landed
            # within ~100 ms of a noise-induced false detect.
            _PREAMBLE_WINDOW_S = 0.2
            preamble_samples = int(_PREAMBLE_WINDOW_S * self._fs)
            keep_start = max(0, vis_end - preamble_samples)
            self._set_buffer(joined[keep_start:])
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

        # Incremental decode path — covers all 22 modes (Scottie, Martin,
        # PD, Wraase, Pasokon, Robot 36).  ``make_incremental_decoder``
        # has an entry for every Mode value, so when this branch is taken
        # ``inc`` is never None — but we assert defensively to make any
        # future Mode addition that misses the factory fail loudly at
        # first RX rather than silently falling through to the batch
        # path (OP-07: stale comment removed; the old fallthrough was
        # dead code).
        if self._incremental_decode:
            # Lazy import avoids a circular dependency at module load time.
            from open_sstv.core.incremental_decoder import (  # noqa: PLC0415
                make_incremental_decoder,
            )
            inc = make_incremental_decoder(
                spec, self._fs, vis_end_abs=vis_end, start_abs=0,
            )
            assert inc is not None, (
                f"make_incremental_decoder returned None for known mode "
                f"{mode!r} — add a backend in incremental_decoder.py."
            )
            self._incremental_dec = inc
            # Pre-feed audio before VIS so the first line's window has
            # the full FILTER_MARGIN of sosfiltfilt padding from the start.
            pre_vis = joined[:vis_end]
            if pre_vis.size > 0:
                self._incremental_dec.feed(pre_vis)
            # Process any post-VIS audio already in the buffer so that
            # callers who feed all samples in a single call still get
            # progress/complete events (same behaviour as the batch path).
            events.extend(self._feed_decoding_incremental(joined[vis_end:]))
            return events

        # Batch path: try an immediate partial decode — the buffer may already
        # contain a few scan lines (or even a full image if the caller
        # fed a large chunk).
        inst = instantaneous_frequency(filtered, self._fs)
        progress_events = self._decode_progress(inst, mode, spec, vis_code)
        events.extend(progress_events)
        return events

    def _feed_decoding(self, joined: NDArray) -> list[DecoderEvent]:
        """Decode available lines from the growing audio buffer."""
        if self._incremental_decode and self._incremental_dec is not None:
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

    def _feed_decoding_incremental(self, new_audio: NDArray) -> list[DecoderEvent]:
        """Incremental decode: feed only ``new_audio`` (audio not yet seen
        by the streaming backend) and translate its rows into events."""
        if self._is_cancelled():
            return []
        assert self._incremental_dec is not None  # guarded by caller
        assert self._mode is not None
        assert self._spec is not None

        if new_audio.size == 0:
            return []

        line_tuples = self._incremental_dec.feed(new_audio)

        events: list[DecoderEvent] = []
        image_height = self._incremental_dec.image_height
        max_row = self._incremental_max_row
        for row_idx, _rgb in line_tuples:
            # Only emit progress when lines_decoded would advance.  Robot 36
            # per-line back-fill re-emits the previous row after each new row
            # arrives (chroma neighbour update); without this guard the
            # progress counter ticks backward, causing UI flicker.
            if row_idx + 1 <= max_row:
                continue
            max_row = row_idx + 1
            img = self._incremental_dec.get_image()
            events.append(
                ImageProgress(
                    image=img.copy(),
                    mode=self._mode,
                    vis_code=self._vis_code,
                    # L2: defensive bound — ``max_row`` is set from
                    # ``row_idx + 1`` and ``image_height`` is the
                    # mode's static height.  walk_sync_grid already
                    # caps ``row_idx < image_height``, so the upper
                    # bound is guaranteed by contract, but pin it
                    # explicitly here so a future per-mode helper
                    # that loses the cap can't drive the progress
                    # bar past 100 %.
                    lines_decoded=min(max_row, image_height),
                    lines_total=image_height,
                )
            )
        self._incremental_max_row = max_row

        if self._incremental_dec.complete:
            img = self._incremental_dec.get_image()
            # Audit #10: a cap-trimmed buffer is missing its start —
            # offering it for the full-quality re-decode would produce
            # a corrupt image, so withhold the snapshot instead.
            self._last_complete_buffer = (
                None if self._retained_buffer_trimmed else self._joined()
            )
            events.append(
                ImageComplete(image=img, mode=self._mode, vis_code=self._vis_code)
            )
            events.extend(self._reset_and_refeed_tail())

        return events

    def _decode_progress(
        self,
        inst: NDArray,
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
            events.extend(self._reset_and_refeed_tail())
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

    def _reset_and_refeed_tail(self) -> list[DecoderEvent]:
        """Reset after ``ImageComplete`` without losing the audio after it.

        v0.6.14 audit (Decoder.reset low): completion used to ``reset()``
        and drop the rest of the buffer, so a single large ``feed`` (a WAV
        with several images back to back, or a chunk that straddles the
        end of one image and the next VIS) lost every image after the
        first. Keep everything from just before the estimated image end —
        the VIS end plus the mode's body duration, less 0.5 % + 100 ms of
        slack for clock drift — and feed it back through IDLE.
        """
        tail: NDArray | None = None
        if self._spec is not None and not self._retained_buffer_trimmed:
            body = self._spec.total_duration_s * self._fs
            end_est = int(self._vis_end + body * 0.995 - 0.1 * self._fs)
            if 0 <= end_est < self._buffered:
                tail = self._joined()[end_est:]
        self.reset()
        if tail is None or tail.size == 0:
            return []
        return self.feed(tail)

    def reset(self) -> None:
        """Drop the buffered audio and reset to IDLE.

        Called automatically after a complete decode, or manually when
        the user clicks "Clear" or changes input device.
        """
        self._buffer.clear()
        self._buffered = 0
        self._state = _DecoderState.IDLE
        self._vis_code = 0
        self._mode = None
        self._spec = None
        self._vis_end = 0
        self._last_lines = 0
        self._incremental_dec = None
        self._incremental_max_row = 0
        self._retained_buffer_trimmed = False

    def _set_buffer(self, arr: NDArray) -> None:
        """Replace the buffered audio with ``arr`` (keeps the count right)."""
        self._buffer = [arr]
        self._buffered = int(arr.size)

    def _joined(self) -> NDArray:
        """The buffered audio as one array. Collapses the chunk list as a
        side effect so repeated calls don't re-concatenate."""
        if not self._buffer:
            return np.array([], dtype=np.float64)
        if len(self._buffer) > 1:
            self._buffer = [np.concatenate(self._buffer)]
        return self._buffer[0]


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
