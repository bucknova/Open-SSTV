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
    _decode_robot36_dispatch,
    decode_wav,
)
from open_sstv.core.dsp_utils import resample_to
from open_sstv.core.encoder import encode
from open_sstv.core.modes import MODE_TABLE, Mode

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


def _byte_to_freq(b: int) -> float:
    """Inverse of decoder's freq→luma map (1500..2300 → 0..255)."""
    return 1500.0 + (2300.0 - 1500.0) * b / 255.0


def _synthesize_robot36_line_pair_track(
    image: Image.Image, fs: int
) -> np.ndarray:
    """Fabricate the per-sample frequency track of a canonical broadcast
    Robot 36 WAV (120 super-lines, no VIS preamble).

    Each super-line encodes two image rows as::

        SYNC (9 ms, 1200 Hz)
        SYNC PORCH (3 ms, 1500 Hz)
        Y_even (88 ms @ 0.275 ms/pixel)
        EVEN SEP (4.5 ms, 1500 Hz)
        COLOR PORCH (1.5 ms, 1900 Hz)
        Cr (44 ms @ 0.1375 ms/pixel, averaged across the row pair)
        SYNC PORCH (3 ms, 1500 Hz)
        Y_odd (88 ms)
        ODD SEP (4.5 ms, 2300 Hz)
        COLOR PORCH (1.5 ms, 1900 Hz)
        Cb (44 ms, averaged across the row pair)

    Total 290.5 ms per super-line, 120 super-lines → ~34.86 s. Used by
    the line-pair dispatch test; the production dispatcher should detect
    this layout from the 290 ms inter-sync spacing and route it through
    ``_decode_robot36_line_pair``.
    """
    ycbcr = np.asarray(image.convert("YCbCr"), dtype=np.int32)
    height, width, _ = ycbcr.shape
    assert height % 2 == 0, "Robot 36 needs an even number of rows"
    y_pix_ms = 88.0 / width
    c_pix_ms = 44.0 / width

    segments: list[tuple[float, float]] = []

    def push(freq_hz: float, dur_ms: float) -> None:
        segments.append((freq_hz, dur_ms / 1000.0))

    for row_even in range(0, height, 2):
        row_odd = row_even + 1
        push(1200.0, 9.0)  # sync
        push(1500.0, 3.0)  # sync porch
        for x in range(width):
            push(_byte_to_freq(int(ycbcr[row_even, x, 0])), y_pix_ms)
        push(1500.0, 4.5)  # even separator
        push(1900.0, 1.5)  # color porch
        for x in range(width):
            cr_avg = (int(ycbcr[row_even, x, 2]) + int(ycbcr[row_odd, x, 2])) // 2
            push(_byte_to_freq(cr_avg), c_pix_ms)
        push(1500.0, 3.0)  # mid-pair sync porch (no sync)
        for x in range(width):
            push(_byte_to_freq(int(ycbcr[row_odd, x, 0])), y_pix_ms)
        push(2300.0, 4.5)  # odd separator
        push(1900.0, 1.5)  # color porch
        for x in range(width):
            cb_avg = (int(ycbcr[row_even, x, 1]) + int(ycbcr[row_odd, x, 1])) // 2
            push(_byte_to_freq(cb_avg), c_pix_ms)

    out: list[np.ndarray] = []
    for freq, dur in segments:
        n = int(round(dur * fs))
        if n > 0:
            out.append(np.full(n, freq, dtype=np.float64))
    return np.concatenate(out)


def test_decode_robot36_line_pair_round_trip_recovers_image() -> None:
    """Canonical broadcast Robot 36 (120 super-lines, 290 ms spacing) —
    the layout SimpleSSTV iOS and MMSSTV emit on air. The dispatcher
    should auto-detect this from the raw sync spacing and route it to
    ``_decode_robot36_line_pair``.
    """
    fs = 48_000
    original = _make_gradient(320, 240)
    spec = MODE_TABLE[Mode.ROBOT_36]

    inst = _synthesize_robot36_line_pair_track(original, fs)
    # No VIS preamble in the synthesized track — call the dispatcher
    # directly with vis_end=0 to exercise the layout auto-detection.
    image = _decode_robot36_dispatch(inst, fs, spec, vis_end=0)

    assert image is not None, "line-pair dispatch returned None"
    assert image.size == (320, 240)
    assert image.mode == "RGB"

    err = _mean_abs_luma_error(image, original)
    assert err < 12.75, (
        f"Robot 36 line-pair round-trip luma error {err:.2f} exceeds 5 % bound"
    )


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
    drifted = resample_to(clean, fs_claimed, fs_actual)
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


def test_decode_robot36_dispatch_apply_slant_correct_false_calls_walk() -> None:
    """_decode_robot36_dispatch(apply_slant_correct=False) must use
    walk_sync_grid rather than slant_corrected_line_starts (OP2-15).

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
        # decode_wav passes apply_slant_correct=False for Robot 36, so
        # running a full Robot 36 decode exercises the no-slant path.
        from open_sstv.core.encoder import encode
        import numpy as np
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

