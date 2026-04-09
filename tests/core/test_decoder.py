# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip and unit tests for ``sstv_app.core.decoder``.

The marquee test here is the v1-plan acceptance bound for Robot 36:
encode→decode a 320×240 fixture, assert per-pixel mean absolute luma
error < 5% of the 0..255 range. If this test passes, the entire RX
DSP front-end (dsp_utils → demod → vis → sync → decoder) is wired up
correctly end-to-end and the project has its first proof-of-life
receiver.

Other tests cover the streaming ``Decoder`` wrapper and the
"return None" failure paths so a refactor can't silently start raising.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from sstv_app.core.decoder import (
    DecodedImage,
    Decoder,
    ImageComplete,
    ImageStarted,
    decode_wav,
)
from sstv_app.core.encoder import encode
from sstv_app.core.modes import Mode

# === helpers ===


def _to_float(samples_int16: np.ndarray) -> np.ndarray:
    """Convert PySSTV's int16 samples to the [-1, 1] float64 buffer the
    decoder expects."""
    return samples_int16.astype(np.float64) / 32768.0


def _make_gradient(width: int = 320, height: int = 240) -> Image.Image:
    """The same horizontal/vertical RGB gradient used in test_vis /
    test_sync. Smooth-but-distinctive content makes per-pixel error
    measurement meaningful: a flat grey image would always pass.
    """
    img = Image.new("RGB", (width, height), color=(0, 0, 0))
    pixels = img.load()
    assert pixels is not None
    for x in range(width):
        for y in range(height):
            pixels[x, y] = (
                x * 255 // (width - 1),
                y * 255 // (height - 1),
                128,
            )
    return img


def _mean_abs_luma_error(decoded: Image.Image, original: Image.Image) -> float:
    """Per-pixel mean absolute luma error in 0..255 units.

    Both images are converted to greyscale before comparison so chroma
    artifacts (which we know are present in Robot 36's subsampled
    chroma) don't dominate the metric. The plan's 5% bound is a luma
    bound for the same reason.
    """
    a = np.asarray(decoded.convert("L"), dtype=np.int32)
    b = np.asarray(original.convert("L"), dtype=np.int32)
    if a.shape != b.shape:
        b = np.asarray(
            original.convert("L").resize(decoded.size, Image.Resampling.LANCZOS),
            dtype=np.int32,
        )
    return float(np.mean(np.abs(a - b)))


# === decode_wav: Robot 36 round-trip (the v1 acceptance test) ===


def test_decode_wav_robot36_round_trip_recovers_image() -> None:
    """End-to-end Robot 36: encode an image, decode it back, assert
    per-pixel mean absolute luma error < 5% of the 0..255 range.

    This is the milestone the v1 plan calls out as 'first end-to-end
    RX milestone'. If it passes, the entire DSP front end works.
    """
    fs = 48_000
    original = _make_gradient(320, 240)
    samples = _to_float(encode(original, Mode.ROBOT_36, sample_rate=fs))

    result = decode_wav(samples, fs)
    assert result is not None, "Robot 36 round-trip returned None"
    assert isinstance(result, DecodedImage)
    assert result.mode == Mode.ROBOT_36
    assert result.vis_code == 0x08
    assert result.image.size == (320, 240)
    assert result.image.mode == "RGB"

    # < 5% of 255 ≈ 12.75 luma units. The plan calls this 'mean
    # absolute luma error < 5%'.
    err = _mean_abs_luma_error(result.image, original)
    assert err < 12.75, (
        f"Robot 36 round-trip per-pixel mean abs luma error {err:.2f} "
        f"exceeds the 5% (12.75) bound"
    )


def test_decode_wav_robot36_round_trip_at_44100() -> None:
    """Same round-trip at 44.1 kHz to catch any 48 kHz hardcoding."""
    fs = 44_100
    original = _make_gradient(320, 240)
    samples = _to_float(encode(original, Mode.ROBOT_36, sample_rate=fs))

    result = decode_wav(samples, fs)
    assert result is not None
    assert result.mode == Mode.ROBOT_36
    err = _mean_abs_luma_error(result.image, original)
    assert err < 12.75


def test_decode_wav_robot36_solid_color_recovers_color() -> None:
    """A solid 50% grey image should round-trip to a solid 50% grey
    image (within a luma unit or two of quantization noise)."""
    fs = 48_000
    original = Image.new("RGB", (320, 240), color=(128, 128, 128))
    samples = _to_float(encode(original, Mode.ROBOT_36, sample_rate=fs))

    result = decode_wav(samples, fs)
    assert result is not None
    luma = np.asarray(result.image.convert("L"))
    # Crop edges where filter ringing lives.
    body = luma[10:-10, 10:-10]
    assert abs(float(body.mean()) - 128.0) < 5.0
    assert float(body.std()) < 5.0


# === decode_wav: failure modes ===


def test_decode_wav_returns_none_for_silence() -> None:
    silence = np.zeros(48_000, dtype=np.float64)
    assert decode_wav(silence, 48_000) is None


def test_decode_wav_returns_none_for_empty_buffer() -> None:
    assert decode_wav(np.array([], dtype=np.float64), 48_000) is None


def test_decode_wav_returns_none_for_2d_input() -> None:
    assert decode_wav(np.zeros((10, 2), dtype=np.float64), 48_000) is None


def test_decode_wav_returns_none_for_truncated_image() -> None:
    """If the buffer ends mid-image we should return None rather than
    decoding garbage rows. We feed a Robot 36 audio buffer truncated
    to half its length."""
    fs = 48_000
    samples = _to_float(
        encode(_make_gradient(320, 240), Mode.ROBOT_36, sample_rate=fs)
    )
    truncated = samples[: samples.size // 2]
    assert decode_wav(truncated, fs) is None


def test_decode_wav_returns_none_for_unsupported_mode_vis() -> None:
    """Phase 2 step 13 only ships Robot 36 — Martin / Scottie are
    locked out at the dispatcher level until step 14. We assert this
    here so the next step's commit can flip the dict and immediately
    pass these (currently expected-None) round-trips for the new modes.
    """
    fs = 48_000
    samples = _to_float(
        encode(_make_gradient(320, 256), Mode.MARTIN_M1, sample_rate=fs)
    )
    # decode_wav recognizes the VIS but the per-mode decoder dict
    # doesn't have an entry yet, so it returns None.
    assert decode_wav(samples, fs) is None


# === Decoder streaming wrapper ===


def test_decoder_feed_chunked_buffer_yields_image_complete() -> None:
    """The streaming Decoder should produce ImageStarted + ImageComplete
    events once the buffered audio contains a full image, regardless of
    how it was chunked."""
    fs = 48_000
    samples = _to_float(
        encode(_make_gradient(320, 240), Mode.ROBOT_36, sample_rate=fs)
    )
    decoder = Decoder(fs)
    chunk = 4096
    events: list = []
    for start in range(0, samples.size, chunk):
        events.extend(decoder.feed(samples[start : start + chunk]))

    started = [e for e in events if isinstance(e, ImageStarted)]
    completed = [e for e in events if isinstance(e, ImageComplete)]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].mode == Mode.ROBOT_36
    assert completed[0].image.size == (320, 240)


def test_decoder_does_not_emit_duplicate_images() -> None:
    """Re-feeding the same audio after a successful decode should not
    re-emit the image. The plan's pull model is one-image-then-reset."""
    fs = 48_000
    samples = _to_float(
        encode(_make_gradient(320, 240), Mode.ROBOT_36, sample_rate=fs)
    )
    decoder = Decoder(fs)
    first = decoder.feed(samples)
    second = decoder.feed(samples[:100])  # any extra feed call
    assert any(isinstance(e, ImageComplete) for e in first)
    assert not any(isinstance(e, ImageComplete) for e in second)


def test_decoder_reset_allows_new_image() -> None:
    """After ``reset()`` the next image-bearing feed should produce a
    fresh ImageComplete."""
    fs = 48_000
    samples = _to_float(
        encode(_make_gradient(320, 240), Mode.ROBOT_36, sample_rate=fs)
    )
    decoder = Decoder(fs)
    decoder.feed(samples)
    decoder.reset()
    events = decoder.feed(samples)
    assert any(isinstance(e, ImageComplete) for e in events)


def test_decoder_feed_silence_returns_no_events() -> None:
    decoder = Decoder(48_000)
    assert decoder.feed(np.zeros(48_000, dtype=np.float64)) == []


def test_decoder_rejects_2d_feed() -> None:
    decoder = Decoder(48_000)
    events = decoder.feed(np.zeros((10, 2), dtype=np.float64))
    assert len(events) == 1
    # Should be a DecodeError, but we keep this assertion structural.
    assert events[0].__class__.__name__ == "DecodeError"


def test_decoder_rejects_non_positive_sample_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        Decoder(0)
    with pytest.raises(ValueError, match="positive"):
        Decoder(-1)
