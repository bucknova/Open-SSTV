# SPDX-License-Identifier: GPL-3.0-or-later
"""Robot 36-specific DSP helpers — Python port of slowrx's video path.

Used by both the batch decoder (``core.decoder._decode_robot36`` /
``_decode_robot36_line_pair``) and the incremental decoder
(``core.incremental_decoder._Robot36*IncrementalDecoder``).  Kept in a
standalone module so the two decoders share the same implementation
verbatim — the earlier split (batch used a windowed median + PIL,
incremental used its own slowrx port) produced visibly different
colors for saturated yellow / cyan / green pixels and a whole chain
of patch-on-patch fixes we eventually gave up on.

Port reference: slowrx by Oona Räisänen (OH7SR), GPL v3
(https://github.com/windytan/slowrx).  Specifically the ``video.c``
per-pixel sampling loop and the YUV→RGB matrix at the end.

Design notes
------------
1. **Per-pixel sampling is a small symmetric mean**, not a windowed
   central-60 % median.  slowrx reads one FFT-peak frequency value
   per pixel at the planned sample time; we approximate that with a
   5-sample mean of the Hilbert-derived instantaneous-frequency track
   centred on each pixel's theoretical centre sample.  That absorbs
   per-sample Hilbert noise while staying well inside a single pixel
   (Robot 36 chroma is ~6.6 samples/pixel at 48 kHz), so neighbouring
   pixels and the chroma-to-sync boundary can't contaminate the read.

2. **No chroma floor.**  Byte values in [0, 38] (frequencies in
   [1500, 1620] Hz) are legitimate low-chroma readings — saturated
   yellow has Cb ≈ 0, cyan has Cr ≈ 0, green has Cr ≈ 21.  An earlier
   implementation clamped these to neutral 128 as "noise," which
   broke every saturated secondary color.

3. **No sync reject / no right-edge guard pixels.**  slowrx trusts its
   demodulator at the edges and so do we.  Filter ringing at the
   chroma-to-sync transition on the Cb scan's rightmost pixel can
   push readings below 1500 Hz; the per-pixel byte mapping saturates
   to 0 in that case, which is consistent with slowrx's ``clip()`` and
   nearly invisible in practice.  (A previous attempt at a
   "_SYNC_REJECT_HZ" threshold produced its own right-edge stripe
   artefact, reinforcing that less defensive handling is better.)

4. **Direct integer YCbCr → RGB matrix** from slowrx's ``video.c``,
   bypassing PIL.  Numerically close to BT.601 full-range but with
   rounded coefficients; gives bit-for-bit reproducibility against the
   reference decoder.  Cr / Cb are centered at 128 — the offsets
   ``-17850 / 13260 / -22695`` bake in the pedestal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_WHITE_HZ

if TYPE_CHECKING:
    from numpy.typing import NDArray


#: Half-width of the mean window used for per-pixel frequency reads.
#: A 5-sample symmetric mean (HALF=2 → samples [c-2, c-1, c, c+1, c+2])
#: absorbs Hilbert-transform per-sample noise while staying well inside
#: a single chroma pixel (~6.6 samples at 48 kHz), so neighbouring
#: pixels and the chroma-to-sync boundary can't contaminate the read.
SAMPLE_HALF: int = 2

#: slowrx byte scaling: ``byte = (freq - 1500) / 3.1372549`` maps the
#: 1500-2300 Hz signalling band to 0-255.  ``3.1372549 ≈ 800/255``.
HZ_PER_BYTE: float = (SSTV_WHITE_HZ - SSTV_BLACK_HZ) / 255.0


def sample_pixel(
    inst: "NDArray",
    center_sample: float,
    track_len: int,
) -> int:
    """Read a single pixel value slowrx-style from an instantaneous-
    frequency track.

    Takes a 5-sample mean centred on ``center_sample`` (clamped to the
    track bounds) and maps the average frequency to a byte in [0, 255]
    via slowrx's ``(f - 1500) / 3.1372549`` linear rule.  Out-of-band
    frequencies saturate rather than being replaced by a neutral value;
    this matches slowrx's ``clip()`` behaviour.
    """
    c = int(round(center_sample))
    lo = c - SAMPLE_HALF
    hi = c + SAMPLE_HALF + 1
    if lo < 0:
        lo = 0
    if hi > track_len:
        hi = track_len
    if hi <= lo:
        return 0
    freq = float(np.mean(inst[lo:hi]))
    byte_val = (freq - SSTV_BLACK_HZ) / HZ_PER_BYTE
    if byte_val < 0.0:
        return 0
    if byte_val > 255.0:
        return 255
    return int(round(byte_val))


def sample_scan(
    inst: "NDArray",
    start: float,
    span_samples: float,
    width: int,
    track_len: int,
) -> "NDArray[np.uint8]":
    """Sample ``width`` pixels across a scan span, slowrx-style.

    ``start`` is the float sample index where the scan begins;
    ``span_samples`` is its total length in samples.  Pixel ``k``'s
    centre is ``start + (k + 0.5) · (span_samples / width)`` — matching
    slowrx's ``PixelGrid[k].Time`` layout.  No edge guard.
    """
    out = np.zeros(width, dtype=np.uint8)
    if width <= 0 or span_samples <= 0:
        return out
    pixel_span = span_samples / width
    for col in range(width):
        center = start + (col + 0.5) * pixel_span
        out[col] = sample_pixel(inst, center, track_len)
    return out


def ycbcr_to_rgb(
    y: "NDArray[np.uint8]",
    cb: "NDArray[np.uint8]",
    cr: "NDArray[np.uint8]",
) -> "NDArray[np.uint8]":
    """Integer YCbCr → RGB conversion from slowrx (``video.c``).

    .. code-block:: c

        R = clip((100·Y + 140·(Cr)         - 17850) / 100)
        G = clip((100·Y -  71·(Cr) - 33·(Cb) + 13260) / 100)
        B = clip((100·Y + 178·(Cb)         - 22695) / 100)

    The offsets bake in the Cb/Cr pedestal around 128 (the scaled
    factors × 128 give the ``-17850 / 13260 / -22695`` constants, give or
    take rounding).  Numerically equivalent to BT.601 full-range with
    coefficients rounded to two decimals, which is what slowrx uses.

    Operates element-wise on equal-shape uint8 arrays; returns an
    ``(H, W, 3)`` uint8 array in R, G, B order.
    """
    y32 = y.astype(np.int32)
    cb32 = cb.astype(np.int32)
    cr32 = cr.astype(np.int32)
    r = (100 * y32 + 140 * cr32 - 17850) // 100
    g = (100 * y32 - 71 * cr32 - 33 * cb32 + 13260) // 100
    b = (100 * y32 + 178 * cb32 - 22695) // 100
    rgb = np.stack(
        (
            np.clip(r, 0, 255).astype(np.uint8),
            np.clip(g, 0, 255).astype(np.uint8),
            np.clip(b, 0, 255).astype(np.uint8),
        ),
        axis=-1,
    )
    return rgb


__all__ = [
    "HZ_PER_BYTE",
    "SAMPLE_HALF",
    "sample_pixel",
    "sample_scan",
    "ycbcr_to_rgb",
]
