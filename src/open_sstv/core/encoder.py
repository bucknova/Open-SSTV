# SPDX-License-Identifier: GPL-3.0-or-later
"""SSTV encoder — thin facade over PySSTV.

PySSTV (MIT) already implements the encoder for every mode we care about
*except* Robot 36, whose upstream class emits a "single-line" format that
virtually no real-world decoder can understand. This module patches that
one mode with ``Robot36LinePair``, a subclass that emits the canonical
line-pair format used by MMSSTV, SimpleSSTV (iOS), and over-the-air
transmissions — where one sync pulse covers two image rows.

For all other modes, the PySSTV class is used as-is. This module exists
so the rest of the app never imports ``pysstv`` directly: it gives us one
place to (a) translate from our ``Mode`` enum to PySSTV's class objects,
(b) preprocess images (resize to mode-native dimensions, convert to RGB)
before handing them off, and (c) return a single NumPy array for the
audio output layer instead of PySSTV's per-sample generator.

PySSTV does **not** auto-resize input images — it calls ``image.getpixel``
at integer coordinates up to ``WIDTH × HEIGHT`` and crashes (or wraps) if
the image is the wrong size. The facade resizes with Pillow LANCZOS so
callers can pass any image and trust it'll come out at the mode's native
resolution. We also normalize to ``RGB`` so palette / RGBA / grayscale
inputs all work without surprising the encoder.

Public API:
    encode(image, mode, sample_rate=48000) -> np.ndarray  # int16 PCM, mono
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from pysstv.color import (
    MartinM1,
    MartinM2,
    PD90,
    PD120,
    PD160,
    PD180,
    PD240,
    PD290,
    PasokonP3,
    PasokonP5,
    PasokonP7,
    Robot36,
    ScottieDX,
    ScottieS1,
    ScottieS2,
    WraaseSC2120,
    WraaseSC2180,
)
from pysstv.sstv import (
    SSTV,
    FREQ_BLACK,
    FREQ_SYNC,
    FREQ_VIS_START,
    FREQ_WHITE,
    byte_to_freq,
)

from open_sstv.core.modes import MODE_TABLE, Mode

if TYPE_CHECKING:
    from os import PathLike


# ---------------------------------------------------------------------------
# Robot 36 line-pair encoder
# ---------------------------------------------------------------------------

class Robot36LinePair(Robot36):
    """Robot 36 encoder that emits the canonical *line-pair* format.

    PySSTV's ``Robot36`` produces a single-line format (one sync per image
    row, alternating Cr / Cb on consecutive lines). Real-world decoders —
    MMSSTV, SimpleSSTV iOS, slowrx, QSSTV — expect the ITU line-pair
    layout where one sync pulse covers *two* image rows.

    Super-line layout (300 ms, 120 total for a 320×240 image)::

        ── even half (150 ms) ──
        SYNC          9.0 ms  @ 1200 Hz
        SYNC PORCH    3.0 ms  @ 1500 Hz
        Y_even       88.0 ms  (320 pixels, luminance of even row)
        EVEN SEP      4.5 ms  @ 1500 Hz
        COLOR PORCH   1.5 ms  @ 1900 Hz
        Cr           44.0 ms  (320 pixels, averaged from even + odd rows)
        ── odd half (150 ms) ──
        SYNC          9.0 ms  @ 1200 Hz
        SYNC PORCH    3.0 ms  @ 1500 Hz
        Y_odd        88.0 ms  (320 pixels, luminance of odd row)
        ODD SEP       4.5 ms  @ 2300 Hz
        COLOR PORCH   1.5 ms  @ 1900 Hz
        Cb           44.0 ms  (320 pixels, averaged from even + odd rows)

    Each half is a complete 150 ms line with its own sync pulse — decoders
    like MMSSTV and slowrx detect the 1200 Hz sync at the start of *each*
    half to stay locked. Total: 2 × 150 ms × 120 pairs = 36 000 ms = 36 s
    (matching the mode name).

    Chroma is averaged between the two rows in each pair, which is what
    every reference decoder assumes (the 4:2:0-ish subsampling inherent
    in Robot 36's design).
    """

    def gen_image_tuples(self):
        """Yield ``(freq_hz, duration_ms)`` tuples for the image payload."""
        yuv = self.image.convert("YCbCr").load()
        y_pixel_ms = self.Y_SCAN / self.WIDTH       # 88 / 320 = 0.275 ms
        c_pixel_ms = self.C_SCAN / self.WIDTH       # 44 / 320 = 0.1375 ms

        for row in range(0, self.HEIGHT, 2):
            # Collect pixel data for both rows in the pair.
            even_pixels = [yuv[col, row] for col in range(self.WIDTH)]
            odd_pixels = [yuv[col, row + 1] for col in range(self.WIDTH)]

            # ============ EVEN HALF (150 ms) ============

            # ---- SYNC (9 ms @ 1200 Hz) ----
            yield FREQ_SYNC, self.SYNC

            # ---- SYNC PORCH (3 ms @ 1500 Hz) ----
            yield FREQ_BLACK, self.SYNC_PORCH

            # ---- Y_even (88 ms) ----
            for p in even_pixels:
                yield byte_to_freq(p[0]), y_pixel_ms

            # ---- EVEN SEPARATOR (4.5 ms @ 1500 Hz) ----
            yield FREQ_BLACK, self.INTER_CH_GAP

            # ---- COLOR PORCH (1.5 ms @ 1900 Hz) ----
            yield FREQ_VIS_START, self.PORCH

            # ---- Cr channel (44 ms, averaged from both rows) ----
            # strict=True: even and odd rows are both built from the same
            # ``range(self.WIDTH)``, so any future change that diverges
            # them (a slicing bug, an early-break optimisation, a half-row
            # tail) raises ValueError instead of silently shortening the
            # chroma row to the shorter list and producing a torn image.
            for ep, op in zip(even_pixels, odd_pixels, strict=True):
                cr = (ep[2] + op[2]) / 2
                yield byte_to_freq(cr), c_pixel_ms

            # ============ ODD HALF (150 ms) ============

            # ---- SYNC (9 ms @ 1200 Hz) ----
            yield FREQ_SYNC, self.SYNC

            # ---- SYNC PORCH (3 ms @ 1500 Hz) ----
            yield FREQ_BLACK, self.SYNC_PORCH

            # ---- Y_odd (88 ms) ----
            for p in odd_pixels:
                yield byte_to_freq(p[0]), y_pixel_ms

            # ---- ODD SEPARATOR (4.5 ms @ 2300 Hz) ----
            yield FREQ_WHITE, self.INTER_CH_GAP

            # ---- COLOR PORCH (1.5 ms @ 1900 Hz) ----
            yield FREQ_VIS_START, self.PORCH

            # ---- Cb channel (44 ms, averaged from both rows) ----
            # strict=True for the same reason as the Cr pairing above.
            for ep, op in zip(even_pixels, odd_pixels, strict=True):
                cb = (ep[1] + op[1]) / 2
                yield byte_to_freq(cb), c_pixel_ms


# ---------------------------------------------------------------------------
# Thin PySSTV subclasses for modes not in the upstream library
# ---------------------------------------------------------------------------
# Martin M3/M4 and Scottie S3/S4 are height-only variants of M1/M2 and
# S1/S2 respectively — every timing constant stays the same; only HEIGHT
# (and VIS_CODE) differs.  PD-50 is PD-90 with a slower pixel clock.
# PySSTV's encoder reads WIDTH/HEIGHT/SCAN/VIS_CODE as class attributes,
# so a one-line subclass is sufficient for each.

class MartinM3(MartinM1):
    """Martin M3 — 320×128 pixels, same line timing as M1, VIS 36."""
    VIS_CODE = 36
    HEIGHT = 128


class MartinM4(MartinM2):
    """Martin M4 — 160×128 pixels, same line timing as M2, VIS 32."""
    VIS_CODE = 32
    HEIGHT = 128


class ScottieS3(ScottieS1):
    """Scottie S3 — 320×128 pixels, same line timing as S1, VIS 52."""
    VIS_CODE = 52
    HEIGHT = 128


class ScottieS4(ScottieS2):
    """Scottie S4 — 160×128 pixels, same line timing as S2, VIS 48."""
    VIS_CODE = 48
    HEIGHT = 128


class PD50(PD90):
    """PD-50 — 320×256 pixels, pixel time 0.286 ms (vs PD-90's 0.532 ms), VIS 93."""
    VIS_CODE = 93
    PIXEL = 0.286


# ---------------------------------------------------------------------------
# Mode → PySSTV class mapping
# ---------------------------------------------------------------------------

#: PySSTV class for each ``Mode`` we ship in v1. Adding a new mode is one
#: line here plus one ``MODE_TABLE`` entry in ``core/modes.py``.
#: Robot 36 uses our custom line-pair subclass instead of upstream's
#: single-line ``Robot36`` so that transmitted images decode correctly in
#: MMSSTV, SimpleSSTV, and other real-world receivers.
_PYSSTV_CLASSES: dict[Mode, type[SSTV]] = {
    Mode.ROBOT_36: Robot36LinePair,
    Mode.MARTIN_M1: MartinM1,
    Mode.MARTIN_M2: MartinM2,
    Mode.MARTIN_M3: MartinM3,
    Mode.MARTIN_M4: MartinM4,
    Mode.SCOTTIE_S1: ScottieS1,
    Mode.SCOTTIE_S2: ScottieS2,
    Mode.SCOTTIE_DX: ScottieDX,
    Mode.SCOTTIE_S3: ScottieS3,
    Mode.SCOTTIE_S4: ScottieS4,
    Mode.PD_50: PD50,
    Mode.PD_90: PD90,
    Mode.PD_120: PD120,
    Mode.PD_160: PD160,
    Mode.PD_180: PD180,
    Mode.PD_240: PD240,
    Mode.PD_290: PD290,
    Mode.WRAASE_SC2_120: WraaseSC2120,
    Mode.WRAASE_SC2_180: WraaseSC2180,
    Mode.PASOKON_P3: PasokonP3,
    Mode.PASOKON_P5: PasokonP5,
    Mode.PASOKON_P7: PasokonP7,
}

# Fail loudly at import time if a Mode was added to the enum without a
# corresponding encoder entry, rather than silently crashing at first TX.
_missing_encoder = set(Mode) - set(_PYSSTV_CLASSES)
assert not _missing_encoder, (
    f"Encoder missing for Mode(s): {_missing_encoder}. "
    "Add an entry to _PYSSTV_CLASSES in encoder.py."
)

#: Default sound card sample rate. 48 kHz is the lowest rate every modern
#: USB sound card and Mac built-in audio supports natively without internal
#: resampling, and it leaves comfortable headroom above the 2.3 kHz top of
#: the SSTV audio band. Callers can override per-call.
DEFAULT_SAMPLE_RATE = 48_000

#: PySSTV quantizes ``gen_samples`` to this many bits. 16 matches WAV files
#: and what ``sounddevice`` wants for an ``int16`` output stream.
_BITS_PER_SAMPLE = 16


def encode(
    image: Image.Image | str | "PathLike[str]",
    mode: Mode,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """Encode an image as SSTV audio samples.

    Parameters
    ----------
    image:
        A Pillow ``Image`` already in memory, or a filesystem path to one.
        Any mode (RGBA, P, L, RGB) is accepted; it'll be converted to RGB
        and resized with LANCZOS to the mode's native dimensions.
    mode:
        Which SSTV mode to transmit in. Must be a key of ``MODE_TABLE``.
    sample_rate:
        Output sample rate in Hz. Defaults to 48 kHz; pass 44100 if you
        explicitly need CD-rate WAVs.

    Returns
    -------
    np.ndarray
        1-D ``int16`` array of mono PCM samples ready for ``sounddevice.play``
        or ``scipy.io.wavfile.write``. Length is approximately
        ``sample_rate * MODE_TABLE[mode].total_duration_s`` (plus VIS leader).
    """
    if mode not in _PYSSTV_CLASSES:
        msg = f"Unsupported SSTV mode: {mode!r}. Known modes: {sorted(MODE_TABLE)}"
        raise ValueError(msg)

    if isinstance(image, Image.Image):
        pil_image = image
    else:
        try:
            pil_image = Image.open(image)
        except Image.DecompressionBombError as exc:
            msg = f"Refusing to encode oversized image {image!r}: {exc}"
            raise ValueError(msg) from exc
    sstv_cls = _PYSSTV_CLASSES[mode]
    # Use the PySSTV class's own WIDTH/HEIGHT for image preparation rather than
    # spec.width/spec.height: PD modes store height = actual_height // 2 in the
    # spec (one entry per sync pulse / super-line) so the decoder finds the right
    # number of sync pulses, but PySSTV's PD classes expect the full image height.
    prepared = _prepare_image(pil_image, sstv_cls.WIDTH, sstv_cls.HEIGHT)

    sstv = sstv_cls(prepared, sample_rate, _BITS_PER_SAMPLE)
    # ``gen_samples`` yields Python ints quantized to ``_BITS_PER_SAMPLE``;
    # ``np.fromiter`` with an explicit count would require pre-computing the
    # length, so we let NumPy grow the buffer (one allocation per encode is
    # fine — we're nowhere near a hot path).
    return np.fromiter(sstv.gen_samples(), dtype=np.int16)


def _prepare_image(image: Image.Image, width: int, height: int) -> Image.Image:
    """Resize and color-convert an image to a mode's native dimensions.

    Always returns a fresh image so the caller's original is untouched.
    LANCZOS is the right resample filter for natural photos at the small
    sizes SSTV uses (320×240 / 320×256); it preserves edges better than
    bilinear without the ringing of bicubic on noisy sources.
    """
    rgb = image.convert("RGB") if image.mode != "RGB" else image.copy()
    if rgb.size != (width, height):
        rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
    return rgb


__all__ = ["DEFAULT_SAMPLE_RATE", "encode"]
