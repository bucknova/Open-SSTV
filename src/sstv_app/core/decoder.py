# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level SSTV decoder.

This module is the buffered front-end of the receive pipeline. It owns:

* a small audio ring buffer (so chunked feeds can be re-windowed without
  callers having to track absolute sample positions),
* the high-level state machine (idle → searching for VIS → decoding image),
* the dispatch into per-mode pixel decoders.

Phase 2 step 13 ships the **Robot 36** decoder. Martin M1 and Scottie S1
land in step 14, which only adds new ``_decode_*`` functions and a dict
entry — the ``Decoder.feed`` plumbing is mode-agnostic.

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

from sstv_app.core.demod import (
    SSTV_BLACK_HZ,
    SSTV_WHITE_HZ,
    instantaneous_frequency,
)
from sstv_app.core.modes import MODE_TABLE, Mode, ModeSpec, mode_from_vis
from sstv_app.core.sync import find_line_starts
from sstv_app.core.vis import detect_vis

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

    vis_result = detect_vis(arr, fs)
    if vis_result is None:
        return None
    vis_code, vis_end = vis_result

    mode = mode_from_vis(vis_code)
    if mode is None:
        return None

    spec = MODE_TABLE[mode]
    inst = instantaneous_frequency(arr, fs)
    line_starts = find_line_starts(inst, fs, spec, start_idx=vis_end)
    if len(line_starts) < spec.height:
        # Buffer didn't contain enough sync pulses for a full image.
        return None

    decoder = _PIXEL_DECODERS.get(mode)
    if decoder is None:
        return None
    image = decoder(inst, fs, spec, line_starts)
    if image is None:
        return None
    return DecodedImage(image=image, mode=mode, vis_code=vis_code)


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
