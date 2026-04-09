# SPDX-License-Identifier: GPL-3.0-or-later
"""SSTV encoder — thin facade over PySSTV.

PySSTV (MIT) already implements the encoder for every mode we care about.
This module exists so the rest of the app never imports ``pysstv`` directly:
it gives us one place to (a) translate from our ``Mode`` enum to PySSTV's
class objects, (b) preprocess images (resize to mode-native dimensions,
convert to RGB) before handing them off, and (c) return a single NumPy
array for the audio output layer instead of PySSTV's per-sample generator.

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
from pysstv.color import MartinM1, Robot36, ScottieS1
from pysstv.sstv import SSTV

from sstv_app.core.modes import MODE_TABLE, Mode

if TYPE_CHECKING:
    from os import PathLike

#: PySSTV class for each ``Mode`` we ship in v1. Adding a new mode is one
#: line here plus one ``MODE_TABLE`` entry in ``core/modes.py``.
_PYSSTV_CLASSES: dict[Mode, type[SSTV]] = {
    Mode.ROBOT_36: Robot36,
    Mode.MARTIN_M1: MartinM1,
    Mode.SCOTTIE_S1: ScottieS1,
}

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

    pil_image = image if isinstance(image, Image.Image) else Image.open(image)
    spec = MODE_TABLE[mode]
    prepared = _prepare_image(pil_image, spec.width, spec.height)

    sstv_cls = _PYSSTV_CLASSES[mode]
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
