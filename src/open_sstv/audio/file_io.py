# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio file loading.

Thin helper used by both the CLI (``open-sstv-decode``) and the GUI's
File → Decode Audio File… action to load a WAV or FLAC file into a
mono ``float64`` array plus its sample rate.

WAV is handled via stdlib ``wave`` so the dependency surface stays
minimal — anyone with a Python install can decode a WAV.  FLAC is
handled via ``soundfile`` (the same optional ``[flac]`` extra that
backs RX audio recording), with a clear error if the package isn't
installed.

The mono downmix lives in ``core.dsp_utils.to_mono_float32`` so this
module is purely the file-format layer; pixel-side DSP doesn't import
from here.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from open_sstv.core.dsp_utils import to_mono_float32

_log = logging.getLogger(__name__)


def load_audio_file(path: Path) -> tuple[NDArray[np.float64], int]:
    """Load a ``.wav`` or ``.flac`` file as a mono ``float64`` buffer + sample rate.

    Parameters
    ----------
    path:
        Filesystem path to a WAV (any sample width: 8 / 16 / 32-bit PCM)
        or FLAC (any soundfile-supported subtype) file.  Stereo is
        downmixed to mono via the average of the two channels.

    Returns
    -------
    ``(samples, sample_rate)`` — samples are float64 with the same
    amplitude scale as the source (int16 → ±1.0 etc., via
    ``to_mono_float32`` then promoted).

    Raises
    ------
    FileNotFoundError
        If *path* doesn't exist.
    ValueError
        If the file extension isn't recognised, or the WAV sample
        width is unsupported, or the file is malformed.
    ImportError
        FLAC was requested but ``soundfile`` isn't installed.  The
        caller should surface a "install with ``pip install
        \"open-sstv[flac]\"``" hint.
    """
    if not path.exists():
        msg = f"audio file not found: {path}"
        raise FileNotFoundError(msg)

    suffix = path.suffix.lower().lstrip(".")
    if suffix == "wav":
        return _load_wav(path)
    if suffix == "flac":
        return _load_flac(path)
    msg = (
        f"unsupported audio file extension: {path.suffix!r} — "
        "expected .wav or .flac"
    )
    raise ValueError(msg)


def _load_wav(path: Path) -> tuple[NDArray[np.float64], int]:
    """Load a WAV via stdlib ``wave`` (no scipy dependency).

    H-4: open via pathlib so non-ASCII characters in *path* on Windows
    go through the wide-char OS API rather than the ANSI code page.
    """
    with path.open("rb") as fh, wave.open(fh, "rb") as wav:
        n_channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        fs = wav.getframerate()
        n_frames = wav.getnframes()
        raw = wav.readframes(n_frames)

    # M7 (v0.3 audit): a file truncated mid-sample leaves a byte count
    # that isn't a multiple of the sample width — np.frombuffer raises
    # a bare ValueError on that.  Trim the ragged tail byte(s).
    if sample_width > 1 and len(raw) % sample_width:
        _log.warning(
            "WAV %s ends mid-sample (truncated?) — dropping %d trailing byte(s)",
            path.name, len(raw) % sample_width,
        )
        raw = raw[: len(raw) - (len(raw) % sample_width)]

    # Decode raw bytes by sample width.  WAV is always little-endian PCM.
    if sample_width == 1:
        # 8-bit WAV is unsigned per the spec; convert to signed centered
        # around zero so to_mono_float32 sees the right amplitude scale.
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
    elif sample_width == 2:
        samples = np.frombuffer(raw, dtype="<i2")
    elif sample_width == 4:
        samples = np.frombuffer(raw, dtype="<i4")
    else:
        msg = f"unsupported WAV sample width: {sample_width} bytes"
        raise ValueError(msg)

    # M7 (v0.3 audit): a header that lies about the sample rate (zero
    # or negative on a hand-corrupted file) would otherwise surface as
    # a divide-by-zero deep in the decoder with no hint about the file.
    if fs <= 0:
        msg = f"invalid WAV sample rate: {fs}"
        raise ValueError(msg)

    if n_channels > 1:
        # M7: a file truncated mid-frame has a sample count that isn't
        # a whole multiple of the channel count — reshape would raise a
        # bare ValueError.  Trim the ragged tail (≤ one frame of audio)
        # and decode what's there.
        whole = (samples.size // n_channels) * n_channels
        if whole != samples.size:
            _log.warning(
                "WAV %s ends mid-frame (truncated?) — dropping %d "
                "trailing sample(s)",
                path.name, samples.size - whole,
            )
            samples = samples[:whole]
        samples = samples.reshape(-1, n_channels)

    mono = to_mono_float32(samples).astype(np.float64)
    return mono, fs


def _load_flac(path: Path) -> tuple[NDArray[np.float64], int]:
    """Load a FLAC via soundfile (optional ``[flac]`` extra)."""
    try:
        import soundfile as sf  # noqa: PLC0415 — lazy optional dep
    except ImportError as exc:
        raise ImportError(
            "FLAC decoding requires the 'soundfile' package.\n"
            'Install with:  pip install "open-sstv[flac]"'
        ) from exc

    # ``sf.read`` returns float64 in [-1.0, 1.0] for PCM_16 / PCM_24 /
    # PCM_32 and FLOAT subtypes alike, plus the file's native sample
    # rate.  Stereo arrays have shape (n_frames, n_channels); mono is
    # shape (n_frames,).
    samples, fs = sf.read(str(path), dtype="float64", always_2d=False)

    if samples.ndim == 2 and samples.shape[1] > 1:
        # Downmix to mono via channel average — matches the WAV path's
        # ``to_mono_float32`` behaviour.
        samples = samples.mean(axis=1)

    return np.asarray(samples, dtype=np.float64), int(fs)


__all__ = ["load_audio_file"]
