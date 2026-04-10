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

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from scipy.signal import sosfiltfilt

from sstv_app.core.demod import (
    SSTV_BLACK_HZ,
    SSTV_WHITE_HZ,
    instantaneous_frequency,
)
from sstv_app.core.dsp_utils import bandpass_sos
from sstv_app.core.modes import MODE_TABLE, Mode, ModeSpec, mode_from_vis
from sstv_app.core.slant import slant_corrected_line_starts
from sstv_app.core.sync import find_sync_candidates
from sstv_app.core.vis import detect_vis

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
) -> Image.Image | None:
    """Slice a frequency track into a Robot 36 YCbCr image.

    See the module docstring for the per-line layout. ``line_starts`` is
    the list of sample indices where each scan line begins (one per
    image row, ``spec.height`` long).
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

    sync_samples = sync_ms / 1000.0 * fs
    sync_porch_samples = sync_porch_ms / 1000.0 * fs
    y_scan_samples = y_scan_ms / 1000.0 * fs
    inter_ch_gap_samples = inter_ch_gap_ms / 1000.0 * fs
    porch_samples = porch_ms / 1000.0 * fs
    c_scan_samples = c_scan_ms / 1000.0 * fs

    y_offset = sync_samples + sync_porch_samples
    c_offset = y_offset + y_scan_samples + inter_ch_gap_samples + porch_samples

    y_plane = np.zeros((height, width), dtype=np.uint8)
    cb_plane = np.zeros((height, width), dtype=np.uint8)
    cr_plane = np.zeros((height, width), dtype=np.uint8)

    n = inst.size
    for row, line_start in enumerate(line_starts):
        y_start = line_start + y_offset
        c_start = line_start + c_offset
        y_row = _sample_pixels(inst, y_start, y_scan_samples, width, n)
        c_row = _sample_pixels(inst, c_start, c_scan_samples, width, n)
        y_plane[row] = y_row
        # PySSTV: even lines (row%2 == 0) carry Cr, odd lines carry Cb.
        # We fill the matching plane on the line that sent the chroma; the
        # other plane stays zero for now and is filled in by the
        # neighbor-pair pass below.
        if row % 2 == 0:
            cr_plane[row] = c_row
        else:
            cb_plane[row] = c_row

    # Chroma subsampling: each line only carries one of the two chroma
    # channels, so we copy the missing chroma from the adjacent line in
    # the same Y/Cb/Cr pair. (Real Robot 36 decoders can interpolate; for
    # v1 we use nearest-neighbor copy, which is what slowrx does.)
    for row in range(height):
        if row % 2 == 0:
            # Need Cb from the next line (or previous if at the very end).
            src = row + 1 if row + 1 < height else row - 1
            cb_plane[row] = cb_plane[src]
        else:
            src = row - 1 if row - 1 >= 0 else row + 1
            cr_plane[row] = cr_plane[src]

    ycbcr = np.stack([y_plane, cb_plane, cr_plane], axis=-1)
    # Construct via ``frombytes`` rather than ``fromarray(mode="YCbCr")``:
    # Pillow 13 deprecates the explicit ``mode`` parameter on ``fromarray``
    # in favor of auto-detection, which would give us "RGB" instead of
    # "YCbCr". ``frombytes`` is the modern way to label color space.
    return Image.frombytes("YCbCr", (width, height), ycbcr.tobytes()).convert(
        "RGB"
    )


def _decode_robot36_line_pair(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    super_starts: list[int],
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
    matching ``_decode_robot36``).
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
    cb_plane = np.zeros((height, width), dtype=np.uint8)
    cr_plane = np.zeros((height, width), dtype=np.uint8)

    n = inst.size
    for pair_idx, sup in enumerate(super_starts):
        row_even = pair_idx * 2
        row_odd = row_even + 1
        if row_odd >= height:
            break
        y_plane[row_even] = _sample_pixels(
            inst, sup + y0_off, y_span, width, n
        )
        y_plane[row_odd] = _sample_pixels(
            inst, sup + y1_off, y_span, width, n
        )
        cr_row = _sample_pixels(inst, sup + cr_off, c_span, width, n)
        cb_row = _sample_pixels(inst, sup + cb_off, c_span, width, n)
        # Both rows in the pair share the pair's chroma (nearest-neighbor
        # upsample from the 320×(height/2) chroma grid).
        cr_plane[row_even] = cr_row
        cr_plane[row_odd] = cr_row
        cb_plane[row_even] = cb_row
        cb_plane[row_odd] = cb_row

    ycbcr = np.stack([y_plane, cb_plane, cr_plane], axis=-1)
    return Image.frombytes("YCbCr", (width, height), ycbcr.tobytes()).convert(
        "RGB"
    )


def _decode_martin_m1(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    line_starts: list[int],
) -> Image.Image | None:
    """Slice a frequency track into a Martin M1 RGB image.

    PySSTV's Martin M1 emits each line as::

        SYNC (4.862 ms) → BLACK porch (0.572 ms)
        GREEN scan (146.432 ms) → BLACK porch
        BLUE scan (146.432 ms)  → BLACK porch
        RED scan (146.432 ms)   → BLACK porch

    The line-start sample index from ``find_line_starts`` lands on the
    leading edge of the SYNC pulse, so each channel's offset from
    ``line_start`` is just (sync + porch) for green, plus a scan + porch
    for each subsequent channel.
    """
    width = spec.width
    height = spec.height

    sync_ms = 4.862
    porch_ms = 0.572
    scan_ms = 146.432

    # Channel-scan start offsets, in milliseconds from the line-start sync.
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


def _decode_scottie_s1(
    inst: NDArray,
    fs: int,
    spec: ModeSpec,
    sync_indices: list[int],
) -> Image.Image | None:
    """Slice a frequency track into a Scottie S1 RGB image.

    Scottie's defining oddity: the SYNC pulse sits **between blue and
    red** within each line, not at the line start. PySSTV's Scottie S1
    emits each line as::

        BLACK porch → GREEN scan → BLACK porch → BLACK porch
        BLUE scan   → BLACK porch → SYNC (9 ms) → BLACK porch
        RED scan    → BLACK porch

    Each per-line index from ``find_line_starts`` lands on the leading
    edge of the *between blue and red* sync pulse — i.e. partway into
    the line. The decoder reaches green and blue by stepping
    **backward** from the sync, and reaches red by stepping forward.
    """
    width = spec.width
    height = spec.height

    sync_ms = 9.0
    porch_ms = 1.5
    scan_ms = 138.24 - porch_ms  # PySSTV: SCAN = 138.24 - INTER_CH_GAP

    # Offsets relative to the *detected sync* index.
    #   Green scan starts (porch + scan + porch + porch + scan) ms before sync.
    #   Blue scan starts (porch + scan) ms before sync (= 138.24 ms back).
    #   Red scan starts (sync + porch) ms after sync.
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
) -> NDArray[np.uint8]:
    """Slice a span of the frequency track into ``width`` pixel medians.

    Walks ``width`` evenly-spaced sub-windows over ``[start, start+span)``,
    takes the median frequency in each window's central 60% (matching the
    bit-window dodging trick from ``vis.detect_vis``), and maps to a uint8
    luma. Returns zeros for pixel windows that fall outside the buffer.

    Median is more robust to filter ringing at sub-window boundaries than
    a plain mean, and dramatically faster than the per-sample interpolation
    slowrx uses (which we don't need until we add slant correction).
    """
    out = np.zeros(width, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    margin = pixel_span * 0.2  # central 60% of each pixel window
    span_lo = SSTV_BLACK_HZ
    span_hi = SSTV_WHITE_HZ
    span_range = span_hi - span_lo
    for col in range(width):
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
        if norm < 0.0:
            norm = 0.0
        elif norm > 1.0:
            norm = 1.0
        out[col] = int(round(norm * 255.0))
    return out


_PIXEL_DECODERS: dict[Mode, callable] = {
    Mode.ROBOT_36: _decode_robot36,
    Mode.MARTIN_M1: _decode_martin_m1,
    Mode.SCOTTIE_S1: _decode_scottie_s1,
}


# === streaming wrapper ===


@dataclass(frozen=True, slots=True)
class ImageStarted:
    """Emitted when a VIS header is decoded and a mode is locked."""

    mode: Mode
    vis_code: int


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


DecoderEvent = ImageStarted | ImageComplete | DecodeError


class Decoder:
    """Streaming SSTV decoder with a pull-model API.

    Buffers audio chunks via ``feed(samples)``; the UI worker calls
    ``feed`` from its own thread as audio arrives. Each ``feed`` call
    returns a (possibly empty) list of ``DecoderEvent`` objects so the
    caller can react without polling.

    The Phase 2 implementation is intentionally simple: every ``feed``
    triggers a single ``decode_wav`` over the entire accumulated buffer.
    This wastes work for very long sessions (we re-detect the same VIS
    over and over), but for typical SSTV transmissions (under 2 minutes)
    it's well within budget. A streaming state-machine refactor is
    explicitly post-v1.
    """

    def __init__(self, fs: int) -> None:
        if fs <= 0:
            raise ValueError(f"Sample rate must be positive (got {fs})")
        self._fs = fs
        self._buffer: list[np.ndarray] = []
        self._last_emitted: int = 0  # number of images we've already yielded

    @property
    def sample_rate(self) -> int:
        return self._fs

    def feed(self, samples: NDArray) -> list[DecoderEvent]:
        """Append a chunk of audio and return any decoder events it
        triggered. Safe to call from a worker thread."""
        arr = np.asarray(samples, dtype=np.float64)
        if arr.ndim != 1:
            return [DecodeError(f"feed expected 1-D, got {arr.ndim}-D")]
        if arr.size > 0:
            self._buffer.append(arr)

        joined = self._joined()
        if joined.size == 0:
            return []
        result = decode_wav(joined, self._fs)
        if result is None:
            return []
        if self._last_emitted >= 1:
            # Already emitted this image; suppress duplicates.
            return []
        self._last_emitted += 1
        return [
            ImageStarted(mode=result.mode, vis_code=result.vis_code),
            ImageComplete(
                image=result.image, mode=result.mode, vis_code=result.vis_code
            ),
        ]

    def reset(self) -> None:
        """Drop the buffered audio and reset the duplicate-suppression
        state. Useful between images or when the user changes input
        device."""
        self._buffer.clear()
        self._last_emitted = 0

    def _joined(self) -> NDArray:
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
    "ImageStarted",
    "decode_wav",
]
