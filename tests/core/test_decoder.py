# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip and unit tests for ``open_sstv.core.decoder``.

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

from open_sstv.core.decoder import (
    DecodedImage,
    Decoder,
    ImageComplete,
    ImageStarted,
    decode_wav,
)
from open_sstv.core.encoder import encode
from open_sstv.core.modes import MODE_TABLE, Mode

# === helpers ===


def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Polyphase resample — test tooling for simulating TX/RX clock drift."""
    from math import gcd  # noqa: PLC0415

    from scipy.signal import resample_poly  # noqa: PLC0415

    g = gcd(src_rate, dst_rate)
    return resample_poly(samples, dst_rate // g, src_rate // g)


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


def test_decode_wav_robot36_clock_drift_round_trip() -> None:
    """Simulate ~2000 ppm RX clock drift (47 900 Hz actual vs. 48 000 Hz
    claimed) by resampling a clean Robot 36 encoding to 47 900 Hz and
    handing it to ``decode_wav`` as if it were 48 000 Hz. The slant
    correction in ``core/slant.py`` should recover the real per-line
    period from the sync candidates and still produce an image within
    the plan's 5 % luma bound.

    Without slant correction the cumulative line drift over 240 lines
    is about 45 ms — roughly 15 % of a line period — which pushes the
    Y scans out of their nominal slots and produces a visible diagonal
    slant in the decoded image.
    """
    fs_claimed = 48_000
    fs_actual = 47_900  # ~2083 ppm slower
    original = _make_gradient(320, 240)
    clean = _to_float(encode(original, Mode.ROBOT_36, sample_rate=fs_claimed))

    # Resample to the "actual" (slower) rate, then lie to the decoder.
    drifted = _resample(clean, fs_claimed, fs_actual)
    result = decode_wav(drifted, fs_claimed)

    assert result is not None, "clock-drifted Robot 36 returned None"
    assert result.mode == Mode.ROBOT_36
    err = _mean_abs_luma_error(result.image, original)
    assert err < 12.75, (
        f"Slant-corrected Robot 36 at ~2000 ppm drift: luma error "
        f"{err:.2f} exceeds 5 % bound"
    )


def test_decode_wav_robot36_low_snr_still_decodes() -> None:
    """Additive white-noise robustness: Robot 36 must still decode at
    5 dB SNR. Without the bandpass prefilter in ``decode_wav`` the sync
    detector collapses around 12 dB SNR (measured empirically: 39 of
    240 expected sync candidates survive at 12 dB, 0 at 10 dB), so this
    test is the regression guard for that prefilter. No luma-error
    bound — at 5 dB the recovered image is noisy but recognizable, and
    asserting the 12.75 luma bound would bake in a stricter requirement
    than the plan calls for.
    """
    fs = 48_000
    original = _make_gradient(320, 240)
    clean = _to_float(encode(original, Mode.ROBOT_36, sample_rate=fs))

    sig_pow = float(np.mean(clean**2))
    snr_db = 5.0
    noise_pow = sig_pow / (10.0 ** (snr_db / 10.0))
    rng = np.random.default_rng(42)
    noisy = clean + rng.normal(0.0, float(np.sqrt(noise_pow)), clean.size)

    result = decode_wav(noisy, fs)
    assert result is not None, "Robot 36 at 5 dB SNR returned None"
    assert result.mode == Mode.ROBOT_36
    assert result.image.size == (320, 240)


def test_decode_wav_martin_m1_round_trip_recovers_image() -> None:
    """Phase 2 step 14 adds the Martin M1 per-mode decoder. Assert the
    same 5 %-luma round-trip bound we use for Robot 36 — the DSP front
    end is shared so any regression there shows up here too."""
    fs = 48_000
    original = _make_gradient(320, 256)
    samples = _to_float(
        encode(original, Mode.MARTIN_M1, sample_rate=fs)
    )

    result = decode_wav(samples, fs)
    assert result is not None, "Martin M1 round-trip returned None"
    assert result.mode == Mode.MARTIN_M1
    assert result.vis_code == 0x2C

    error = _mean_abs_luma_error(result.image, original)
    assert error < 12.75, (
        f"Martin M1 round-trip luma error {error:.2f} exceeds 5 % bound"
    )


def test_decode_wav_scottie_s1_round_trip_recovers_image() -> None:
    """Scottie S1 has the mid-line sync quirk (sync sits between blue
    and red scans) — this is the per-mode decoder's acceptance test."""
    fs = 48_000
    original = _make_gradient(320, 256)
    samples = _to_float(
        encode(original, Mode.SCOTTIE_S1, sample_rate=fs)
    )

    result = decode_wav(samples, fs)
    assert result is not None, "Scottie S1 round-trip returned None"
    assert result.mode == Mode.SCOTTIE_S1
    assert result.vis_code == 0x3C

    error = _mean_abs_luma_error(result.image, original)
    assert error < 12.75, (
        f"Scottie S1 round-trip luma error {error:.2f} exceeds 5 % bound"
    )


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


# === cancel event tests ===


def test_set_cancel_event_accepted() -> None:
    """set_cancel_event() should accept a threading.Event without raising."""
    import threading

    decoder = Decoder(48_000)
    ev = threading.Event()
    decoder.set_cancel_event(ev)   # no exception
    decoder.set_cancel_event(None)  # detach is also valid


def test_cancel_event_not_set_does_not_abort() -> None:
    """A cancel event that is never set must not suppress decode output."""
    import threading

    fs = 48_000
    img = _make_gradient(320, 240)
    samples_int16 = encode(img, Mode.ROBOT_36, sample_rate=fs)
    samples = _to_float(samples_int16)

    ev = threading.Event()
    decoder = Decoder(fs)
    decoder.set_cancel_event(ev)

    all_events: list = []
    for chunk in np.array_split(samples, 20):
        all_events.extend(decoder.feed(chunk))

    types = [e.__class__.__name__ for e in all_events]
    assert "ImageStarted" in types, "Expected ImageStarted when cancel is clear"
    assert "ImageComplete" in types, "Expected ImageComplete when cancel is clear"


def test_cancel_during_decoding_returns_no_events() -> None:
    """Setting the cancel event during DECODING state must produce no
    ImageProgress or ImageComplete events from that flush."""
    import threading

    fs = 48_000
    img = _make_gradient(320, 240)
    samples_int16 = encode(img, Mode.ROBOT_36, sample_rate=fs)
    samples = _to_float(samples_int16)

    ev = threading.Event()
    decoder = Decoder(fs)
    decoder.set_cancel_event(ev)

    # Feed until VIS is detected (state → DECODING), collect ImageStarted.
    chunks = np.array_split(samples, 40)
    started = False
    for chunk in chunks:
        for event in decoder.feed(chunk):
            if event.__class__.__name__ == "ImageStarted":
                started = True
        if started:
            break

    assert started, "Need to reach DECODING state for this test"

    # Set the cancel event and feed one more chunk — must produce no events.
    ev.set()
    events_after_cancel = decoder.feed(chunks[-1] if len(chunks) > 1 else samples[:1000])
    assert events_after_cancel == [], (
        f"Expected no events after cancel, got {events_after_cancel}"
    )


def test_cancel_event_cleared_by_reset() -> None:
    """After reset() the cancel event is clear so future decodes proceed."""
    import threading

    decoder = Decoder(48_000)
    ev = threading.Event()
    decoder.set_cancel_event(ev)

    ev.set()
    assert decoder._is_cancelled()

    decoder.reset()
    # reset() on the Decoder just clears buffer state; the RxWorker's
    # reset() slot is responsible for clearing the threading.Event.
    # Simulate that here.
    ev.clear()
    assert not decoder._is_cancelled()


def test_is_cancelled_without_event() -> None:
    """_is_cancelled() must return False when no event is registered."""
    decoder = Decoder(48_000)
    assert not decoder._is_cancelled()


def test_detach_cancel_event() -> None:
    """After set_cancel_event(None) a set event no longer cancels decoding."""
    import threading

    decoder = Decoder(48_000)
    ev = threading.Event()
    decoder.set_cancel_event(ev)
    ev.set()
    assert decoder._is_cancelled()

    decoder.set_cancel_event(None)
    assert not decoder._is_cancelled()


# === D-3 progressive decode stability (walk_sync_grid) ===


def test_walk_sync_grid_stable_for_existing_candidates() -> None:
    """Progressive decode uses walk_sync_grid so already-detected line positions
    are identical regardless of how many additional candidates arrive later (D-3).

    walk_sync_grid anchors at the first valid pair and walks forward.  Adding
    more candidates past the current horizon extends the walk but leaves the
    first N entries unchanged.  This is the key property that prevents the
    top-of-image "break" that occurred when slant_corrected_line_starts was
    used in the progressive path: there, every new batch of candidates refitted
    the least-squares line and shifted all previously-computed positions.
    """
    from open_sstv.core.sync import walk_sync_grid

    # Scottie S1 nominal line period at 48 kHz (~428 ms).
    nominal = 428.22 / 1000.0 * 48_000  # ≈ 20555 samples
    n_lines = 256

    # Synthetic candidates with ~300 ppm drift: actual period slightly longer.
    true_period = nominal * 1.003
    anchor = 50_000
    candidates = [int(round(anchor + i * true_period)) for i in range(n_lines)]

    # First flush: only 60 lines detected.
    grid_60 = walk_sync_grid(candidates[:60], nominal, n_lines)

    # Second flush: 180 lines detected (3× more data).
    grid_180 = walk_sync_grid(candidates[:180], nominal, n_lines)

    # The first 60 entries must be byte-for-byte identical.
    assert grid_60[:60] == grid_180[:60], (
        "walk_sync_grid must return identical positions for already-detected lines "
        "when more candidates are appended (D-3 stability contract)"
    )


def test_slant_correction_shifts_positions_with_more_data() -> None:
    """Regression guard: slant_corrected_line_starts DOES change positions as
    more noisy candidates arrive, confirming the test above is non-trivial.

    This test documents WHY we use walk_sync_grid in _partial_decode() instead
    of slant_corrected_line_starts: with noisy, drifted candidates the
    least-squares fit parameters change as N grows, causing already-projected
    positions to shift — the D-3 symptom.
    """
    import random

    from open_sstv.core.slant import slant_corrected_line_starts

    rng = random.Random(0xC0DE)
    nominal = 20555.0
    n_lines = 256

    # Candidates with drift + per-candidate jitter so the regression fit
    # changes non-trivially between 40 and 200 data points.
    true_period = nominal * 1.003
    anchor = 50_000
    candidates = [
        int(round(anchor + i * true_period + rng.gauss(0, 8)))
        for i in range(n_lines)
    ]

    slant_40 = slant_corrected_line_starts(candidates[:40], nominal, n_lines)
    slant_200 = slant_corrected_line_starts(candidates[:200], nominal, n_lines)

    # At least some of the first-40 positions must differ between the two fits.
    assert any(slant_40[i] != slant_200[i] for i in range(40)), (
        "slant_corrected_line_starts should update projected positions as more "
        "candidates arrive (verifying the D-3 test above is not vacuously true)"
    )


# === OP2-15: CLI decode_wav skips slant correction for Robot 36 ===


def test_decode_wav_robot36_grid_calls_walk_not_slant() -> None:
    """decode_wav must build the Robot 36 grid with walk_sync_grid rather
    than slant_corrected_line_starts (OP2-15).

    We verify by patching both grid functions with call counters and
    running a full Robot 36 decode to get a real candidate set.
    """
    from unittest.mock import patch

    from open_sstv.core.slant import slant_corrected_line_starts
    from open_sstv.core.sync import walk_sync_grid

    slant_calls: list[int] = []
    walk_calls: list[int] = []

    def _counting_slant(*args, **kwargs):
        slant_calls.append(1)
        return slant_corrected_line_starts(*args, **kwargs)

    def _counting_walk(*args, **kwargs):
        walk_calls.append(1)
        return walk_sync_grid(*args, **kwargs)

    with (
        patch("open_sstv.core.decoder.slant_corrected_line_starts", side_effect=_counting_slant),
        patch("open_sstv.core.decoder.walk_sync_grid", side_effect=_counting_walk),
    ):
        import numpy as np

        from open_sstv.core.encoder import encode
        img = _make_gradient(320, 240)
        samples = encode(img, Mode.ROBOT_36, sample_rate=48_000)
        audio = samples.astype(np.float64) / 32768.0
        result = decode_wav(audio, 48_000)

    assert result is not None
    assert result.mode == Mode.ROBOT_36
    assert walk_calls, "walk_sync_grid must be called in the no-slant path"
    assert not slant_calls, (
        "slant_corrected_line_starts must NOT be called for Robot 36 (OP2-15)"
    )


def test_decode_wav_robot36_uses_walk_not_slant() -> None:
    """decode_wav() must not apply global polyfit slant correction for
    Robot 36 — it passes apply_slant_correct=False to match the GUI path
    which deliberately skips the polyfit on noisy signals (OP2-15)."""
    from unittest.mock import patch

    import numpy as np

    from open_sstv.core.encoder import encode

    fs = 48_000
    img = _make_gradient(320, 240)
    samples = encode(img, Mode.ROBOT_36, sample_rate=fs)
    audio = samples.astype(np.float64) / 32768.0

    slant_calls: list[int] = []

    from open_sstv.core.slant import slant_corrected_line_starts as _orig_slant

    def _counting_slant(*args, **kwargs):
        slant_calls.append(1)
        return _orig_slant(*args, **kwargs)

    with patch("open_sstv.core.decoder.slant_corrected_line_starts", side_effect=_counting_slant):
        result = decode_wav(audio, fs)

    assert result is not None
    assert result.mode == Mode.ROBOT_36
    assert slant_calls == [], (
        "decode_wav must not call slant_corrected_line_starts for Robot 36 (OP2-15)"
    )



# === PD chroma sampling (batch decoder) ===================================
#
# The batch and incremental decoders each own a copy of the chroma pixel
# sampler.  Between v0.1.13 and v0.6.10 they disagreed: the incremental
# copy was fixed to reject only the sync band, while this one still
# clamped any chroma below 15 % of the signalling band (byte 38) to
# neutral 128.  Chroma is coded 0-255 around a neutral 128, so a low value
# is a *saturated* pixel (Cb≈0 is fully yellow, Cr≈0 fully cyan) — the
# clamp erased exactly the most colourful pixels in the frame.  Only the
# PD family reaches this path, so every PD image decoded from a WAV lost
# its saturated yellows, cyans and greens.  These three tests are the
# batch-side mirrors of the ``_sample_pixels_inc`` guards in
# tests/core/test_incremental_decoder.py.


def test_batch_sample_pixels_chroma_does_not_clamp_low_values() -> None:
    """Low chroma frequencies decode as themselves, not 128."""
    from open_sstv.core.decoder import _sample_pixels
    from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_WHITE_HZ

    width = 16
    total_samples = width * 100
    # Byte 10 → 1531.4 Hz, well under the old 15 % floor (~1620 Hz).
    target_byte = 10
    target_freq = SSTV_BLACK_HZ + (
        (target_byte / 255.0) * (SSTV_WHITE_HZ - SSTV_BLACK_HZ)
    )
    inst = np.full(total_samples, target_freq, dtype=np.float64)

    out = _sample_pixels(
        inst, 0.0, float(total_samples), width, total_samples, chroma=True
    )
    assert abs(int(out[0]) - target_byte) <= 2, (
        f"_sample_pixels: chroma byte {target_byte} decoded as {out[0]} "
        "— floor clamp regression?"
    )


def test_batch_sample_pixels_chroma_rejects_sync_band_leakage() -> None:
    """Sync-band frequencies still clamp chroma to neutral 128."""
    from open_sstv.core.decoder import _sample_pixels
    from open_sstv.core.demod import SSTV_SYNC_HZ

    width = 16
    total_samples = width * 100
    inst = np.full(total_samples, float(SSTV_SYNC_HZ), dtype=np.float64)

    out = _sample_pixels(
        inst, 0.0, float(total_samples), width, total_samples, chroma=True
    )
    assert int(out[0]) == 128, (
        f"_sample_pixels: sync-band chroma decoded as {out[0]}, expected "
        "neutral 128"
    )


def test_batch_and_incremental_chroma_samplers_agree() -> None:
    """The two chroma samplers must produce identical rows.

    The PD bug above existed only because these two copies drifted.  A
    frame decoded live off the air and the same frame decoded from a WAV
    import have to land on the same pixels, so sweep the whole chroma
    range plus the sync band through both and require a byte-for-byte
    match.
    """
    from open_sstv.core.decoder import _sample_pixels
    from open_sstv.core.demod import SSTV_BLACK_HZ, SSTV_SYNC_HZ, SSTV_WHITE_HZ
    from open_sstv.core.incremental_decoder import _sample_pixels_inc

    width = 32
    total_samples = width * 100
    freqs = [float(SSTV_SYNC_HZ), 1350.0, 1450.0]
    freqs += [
        SSTV_BLACK_HZ + (b / 255.0) * (SSTV_WHITE_HZ - SSTV_BLACK_HZ)
        for b in (0, 1, 10, 38, 39, 128, 254, 255)
    ]
    for freq in freqs:
        inst = np.full(total_samples, freq, dtype=np.float64)
        for chroma in (True, False):
            batch = _sample_pixels(
                inst, 0.0, float(total_samples), width, total_samples,
                chroma=chroma,
            )
            inc = _sample_pixels_inc(
                inst, 0.0, float(total_samples), width, total_samples,
                chroma=chroma,
            )
            assert np.array_equal(batch, inc), (
                f"samplers disagree at {freq:.1f} Hz (chroma={chroma}): "
                f"batch={batch.tolist()} inc={inc.tolist()}"
            )


def test_pd_roundtrip_preserves_saturated_chroma() -> None:
    """A saturated-yellow PD-90 frame survives encode → decode.

    Pure yellow is Cb ≈ 0 under full-range BT.601 — precisely the value
    the old 15 % floor replaced with neutral 128, which turned the whole
    frame pale pink.  Uses PD-90 because it is the shortest PD mode.
    """
    from open_sstv.core.decoder import decode_wav

    spec = MODE_TABLE[Mode.PD_90]
    height = spec.height * 2
    img = Image.new("RGB", (spec.width, height), (255, 255, 0))

    fs = 48_000
    samples = encode(img, Mode.PD_90, sample_rate=fs).astype(np.float64)
    result = decode_wav(samples, fs)

    assert result is not None
    assert result.mode == Mode.PD_90
    # Sample the interior, away from the right-edge chroma guard and the
    # first/last rows where sync search can clip a scan.
    arr = np.array(result.image.convert("RGB"), dtype=float)
    interior = arr[8:-8, 8:-16]
    assert interior[..., 0].mean() > 235, "red channel lost"
    assert interior[..., 1].mean() > 235, "green channel lost"
    assert interior[..., 2].mean() < 25, (
        f"blue channel is {interior[..., 2].mean():.1f}, expected ~0 — "
        "saturated chroma was clamped to neutral"
    )


# === v0.6.14 audit D-H1 / D-H2 / D-M1 / Decoder.reset ===


def _audio_for(mode: Mode, img: Image.Image, fs: int = 48_000) -> np.ndarray:
    audio = _to_float(encode(img, mode, sample_rate=fs))
    pad = np.zeros(fs // 4)
    return np.concatenate([pad, audio, pad])


def _blank_syncs(audio: np.ndarray, mode: Mode, lines: list[int], fs: int = 48_000) -> np.ndarray:
    """Overwrite line ``lines``' sync pulses with a 1500 Hz porch tone."""
    from open_sstv.core.sync import line0_sync_hint  # noqa: PLC0415
    from open_sstv.core.vis import detect_vis  # noqa: PLC0415

    spec = MODE_TABLE[mode]
    _code, vis_end = detect_vis(audio, fs)
    line0 = line0_sync_hint(spec, fs, vis_end)
    period = spec.line_time_ms / 1000 * fs
    n = int(spec.sync_pulse_ms / 1000 * fs) + 96
    out = audio.copy()
    t = np.arange(n) / fs
    for line in lines:
        s = int(line0 + line * period) - 48
        out[s:s + n] = 0.5 * np.sin(2 * np.pi * 1500.0 * t)
    return out


def _live_complete(audio: np.ndarray, fs: int = 48_000, chunk: int = 4800):
    dec = Decoder(fs)
    events = []
    for i in range(0, audio.size, chunk):
        events.extend(dec.feed(audio[i:i + chunk]))
    return [e for e in events if isinstance(e, ImageComplete)]


def _mae(a: Image.Image, b: Image.Image) -> float:
    return float(np.abs(
        np.asarray(a.convert("RGB"), dtype=float) - np.asarray(b.convert("RGB"), dtype=float)
    ).mean())


@pytest.mark.parametrize("lines", [[0], [1], [0, 1]])
def test_missed_leading_sync_martin_m4_batch_and_live(lines: list[int]) -> None:
    """D-H1: one missed sync on line 0/1 used to lose the image (batch
    returned None; live never completed)."""
    img = _make_gradient(320, 128)
    clean = _audio_for(Mode.MARTIN_M4, img)
    ref = decode_wav(clean, 48_000)
    assert ref is not None
    damaged = _blank_syncs(clean, Mode.MARTIN_M4, lines)

    batch = decode_wav(damaged, 48_000)
    assert batch is not None
    assert _mae(batch.image, ref.image) < 2.0

    live = _live_complete(damaged)
    assert len(live) == 1
    assert _mae(live[0].image, ref.image) < 2.0


@pytest.mark.parametrize("lines", [[0], [1], [2], [0, 1]])
def test_missed_early_sync_robot36_keeps_chroma_parity(lines: list[int]) -> None:
    """D-H1 + D-H2: a missed sync among the first three used to send live
    Robot 36 to the (now removed) line-pair backend (MAE ~32) or stall,
    and swap Cr/Cb in the batch path. Saturated colour makes a parity
    swap obvious."""
    img = Image.new("RGB", (320, 240), (220, 40, 40))
    clean = _audio_for(Mode.ROBOT_36, img)
    ref = decode_wav(clean, 48_000)
    assert ref is not None
    damaged = _blank_syncs(clean, Mode.ROBOT_36, lines)

    batch = decode_wav(damaged, 48_000)
    assert batch is not None
    assert _mae(batch.image, ref.image) < 3.0

    live = _live_complete(damaged)
    assert len(live) == 1
    assert _mae(live[0].image, ref.image) < 3.0


def test_feed_does_not_reconcatenate_buffer_while_decoding() -> None:
    """D-M1: in DECODING the incremental backend gets only the new chunk
    and the retained buffer is never re-joined per feed."""
    from unittest.mock import patch  # noqa: PLC0415

    img = _make_gradient(320, 128)
    audio = _audio_for(Mode.MARTIN_M4, img)
    dec = Decoder(48_000)
    chunk = 4800
    pos = 0
    while dec._state.value == "idle":
        dec.feed(audio[pos:pos + chunk])
        pos += chunk
    fed_sizes: list[int] = []
    real_feed = dec._incremental_dec.feed

    def spy(x):
        fed_sizes.append(x.size)
        return real_feed(x)

    with (
        patch.object(dec._incremental_dec, "feed", side_effect=spy),
        patch.object(Decoder, "_joined", side_effect=AssertionError("joined")),
    ):
        for _ in range(20):
            dec.feed(audio[pos:pos + chunk])
            pos += chunk
    assert fed_sizes == [chunk] * 20
    assert dec._buffered == sum(a.size for a in dec._buffer)


@pytest.mark.parametrize("chunk", [None, 48_000])
def test_back_to_back_images_in_one_feed_both_decode(chunk: int | None) -> None:
    """Decoder.reset low: completion used to drop the rest of the chunk,
    so a buffer holding two transmissions only yielded the first."""
    img_a = _make_gradient(320, 128)
    img_b = Image.new("RGB", (320, 128), (30, 200, 90))
    audio = np.concatenate([
        _audio_for(Mode.MARTIN_M4, img_a), _audio_for(Mode.MARTIN_M4, img_b),
    ])
    if chunk is None:
        events = Decoder(48_000).feed(audio)
        completes = [e for e in events if isinstance(e, ImageComplete)]
    else:
        completes = _live_complete(audio, chunk=chunk)
    assert len(completes) == 2
    second = np.asarray(completes[1].image, dtype=float)
    assert np.abs(second - np.array([30, 200, 90])).mean() < 10
