# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.core.sync``.

Two layers, mirroring ``test_vis.py``:

* **Synthetic** — fabricate frequency tracks directly with NumPy. We don't
  need real FM signals here because ``sync`` consumes a frequency track,
  not raw audio. Faster and easier to reason about than building a full
  phase-continuous signal for every test.
* **Integration** — encode a real image with PySSTV, run the full
  decode pipeline (audio → IF → ``detect_vis`` → ``find_line_starts``)
  and assert the line-start grid matches the mode's expected geometry.
  This is the test the v1 plan calls for ("240 line starts spaced ~150 ms
  apart with std-dev < 1 sample").
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from sstv_app.core.demod import instantaneous_frequency
from sstv_app.core.encoder import encode
from sstv_app.core.modes import MODE_TABLE, Mode
from sstv_app.core.sync import find_leader, find_line_starts
from sstv_app.core.vis import detect_vis

# === helpers ===


def _make_freq_track(
    segments: list[tuple[float, float]], fs: int
) -> np.ndarray:
    """Build a synthetic frequency track from ``(freq_hz, duration_s)`` segments.

    Unlike ``_phase_continuous`` in ``test_vis.py``, this returns the
    *frequency-vs-time* track directly — sync consumes the IF array, not
    raw audio, so we can skip the Hilbert step entirely.
    """
    out: list[np.ndarray] = []
    for freq, dur in segments:
        n = int(round(dur * fs))
        if n > 0:
            out.append(np.full(n, freq, dtype=np.float64))
    if not out:
        return np.array([], dtype=np.float64)
    return np.concatenate(out)


def _synthesize_leader_freq_track(fs: int = 48_000) -> np.ndarray:
    """Frequency track for a full VIS leader (300 ms + 10 ms break + 300 ms +
    30 ms start bit + 30 ms data zero). Used to exercise ``find_leader``.

    The trailing data tone (1300 Hz) keeps the start bit run from extending
    to the very end of the buffer, so the leader detector must actually
    *detect* the boundary rather than coincidentally hit the buffer end.
    """
    return _make_freq_track(
        [
            (1900.0, 0.300),
            (1200.0, 0.010),
            (1900.0, 0.300),
            (1200.0, 0.030),  # start bit
            (1300.0, 0.030),  # one data bit so the run is bounded
        ],
        fs,
    )


def _synthesize_line_freq_track(
    spec, num_lines: int, fs: int = 48_000
) -> np.ndarray:
    """Build a frequency track that *looks* like ``num_lines`` SSTV scan
    lines: each line is one mode-correct sync pulse @ 1200 Hz followed by a
    line's worth of mid-luma (1900 Hz) padding.

    Real lines have porches and per-channel scans at varying frequencies,
    but for sync detection we only need the 1200 Hz pulses to land at the
    right grid positions; the rest of the track just needs to *not* live
    in the 1100–1300 Hz sync band so it isn't picked up as a sync run.
    """
    sync_dur_s = spec.sync_pulse_ms / 1000.0
    line_dur_s = spec.line_time_ms / 1000.0
    body_dur_s = line_dur_s - sync_dur_s
    segments: list[tuple[float, float]] = []
    for _ in range(num_lines):
        segments.append((1200.0, sync_dur_s))
        segments.append((1900.0, body_dur_s))
    return _make_freq_track(segments, fs)


# === find_leader unit tests ===


def test_find_leader_locates_start_bit_in_synthetic_leader() -> None:
    fs = 48_000
    track = _synthesize_leader_freq_track(fs)
    idx = find_leader(track, fs)
    assert idx is not None
    # The start bit begins at 300 + 10 + 300 = 610 ms into the track.
    expected = int(round(0.610 * fs))
    # Allow ±2 ms slop for the boxcar smoother's leading-edge correction.
    assert abs(idx - expected) < int(0.002 * fs)


def test_find_leader_returns_none_for_empty_buffer() -> None:
    assert find_leader(np.array([], dtype=np.float64), 48_000) is None


def test_find_leader_returns_none_for_2d_input() -> None:
    assert find_leader(np.zeros((10, 2), dtype=np.float64), 48_000) is None


def test_find_leader_returns_none_for_constant_non_sync_track() -> None:
    track = np.full(48_000, 1900.0, dtype=np.float64)
    assert find_leader(track, 48_000) is None


def test_find_leader_rejects_short_sync_runs() -> None:
    """A 10 ms sync run (the mid-leader break) must NOT register as a
    leader — only ≥20 ms runs should."""
    fs = 48_000
    track = _make_freq_track(
        [
            (1900.0, 0.100),
            (1200.0, 0.010),  # break — too short
            (1900.0, 0.100),
        ],
        fs,
    )
    assert find_leader(track, fs) is None


# === find_line_starts unit tests ===


@pytest.mark.parametrize(
    "mode",
    [Mode.ROBOT_36, Mode.MARTIN_M1, Mode.SCOTTIE_S1],
)
def test_find_line_starts_synthetic_grid_matches_expected_count(
    mode: Mode,
) -> None:
    """Given a clean synthetic line grid, we should recover every line."""
    fs = 48_000
    spec = MODE_TABLE[mode]
    track = _synthesize_line_freq_track(spec, num_lines=spec.height, fs=fs)
    starts = find_line_starts(track, fs, spec, start_idx=0)
    assert len(starts) == spec.height


@pytest.mark.parametrize(
    "mode",
    [Mode.ROBOT_36, Mode.MARTIN_M1, Mode.SCOTTIE_S1],
)
def test_find_line_starts_synthetic_grid_has_uniform_spacing(
    mode: Mode,
) -> None:
    """Detected line spacings should be tight against the nominal grid."""
    fs = 48_000
    spec = MODE_TABLE[mode]
    track = _synthesize_line_freq_track(spec, num_lines=spec.height, fs=fs)
    starts = find_line_starts(track, fs, spec, start_idx=0)
    assert len(starts) >= 2
    diffs = np.diff(np.asarray(starts, dtype=np.float64))
    expected_spacing = spec.line_time_ms / 1000.0 * fs
    # Std-dev should be tiny on a perfectly clean synthetic grid.
    assert diffs.std() < 1.0
    # And the mean spacing should match the mode's nominal line time.
    np.testing.assert_allclose(diffs.mean(), expected_spacing, rtol=0.001)


def test_find_line_starts_caps_at_spec_height() -> None:
    """Even with extra trailing pulses, we shouldn't return more than
    ``spec.height`` line starts."""
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    extra = 30
    track = _synthesize_line_freq_track(
        spec, num_lines=spec.height + extra, fs=fs
    )
    starts = find_line_starts(track, fs, spec, start_idx=0)
    assert len(starts) == spec.height


def test_find_line_starts_honors_start_idx_offset() -> None:
    """Sync pulses *before* ``start_idx`` should be ignored."""
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    pre_pad = int(0.5 * fs)  # 500 ms of leading silence-band padding
    pad = np.full(pre_pad, 1900.0, dtype=np.float64)
    body = _synthesize_line_freq_track(spec, num_lines=10, fs=fs)
    track = np.concatenate([pad, body])

    starts = find_line_starts(track, fs, spec, start_idx=pre_pad)
    assert len(starts) == 10
    # Every returned index should land at or after the offset.
    assert all(s >= pre_pad for s in starts)
    # First detected line should sit very close to the offset.
    assert abs(starts[0] - pre_pad) < int(0.005 * fs)


def test_find_line_starts_returns_empty_for_empty_buffer() -> None:
    spec = MODE_TABLE[Mode.ROBOT_36]
    assert find_line_starts(np.array([], dtype=np.float64), 48_000, spec) == []


def test_find_line_starts_returns_empty_for_2d_input() -> None:
    spec = MODE_TABLE[Mode.ROBOT_36]
    assert (
        find_line_starts(np.zeros((10, 2), dtype=np.float64), 48_000, spec)
        == []
    )


def test_find_line_starts_returns_empty_for_out_of_range_start_idx() -> None:
    spec = MODE_TABLE[Mode.ROBOT_36]
    track = np.full(1000, 1900.0, dtype=np.float64)
    # start_idx past the end of the buffer.
    assert find_line_starts(track, 48_000, spec, start_idx=2000) == []
    # And a negative start_idx.
    assert find_line_starts(track, 48_000, spec, start_idx=-1) == []


def test_find_line_starts_returns_empty_when_no_pulses_present() -> None:
    spec = MODE_TABLE[Mode.ROBOT_36]
    track = np.full(int(2.0 * 48_000), 1900.0, dtype=np.float64)
    assert find_line_starts(track, 48_000, spec) == []


def test_find_line_starts_skips_short_noise_spikes() -> None:
    """A 1 ms noise spike in the sync band should not be picked up as a
    line sync — only runs ≥50% of the mode's sync length count."""
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    segments: list[tuple[float, float]] = [
        (1900.0, 0.050),
        (1200.0, 0.001),  # too short — should be ignored
        (1900.0, 0.100),
    ]
    track = _make_freq_track(segments, fs)
    assert find_line_starts(track, fs, spec) == []


def test_find_line_starts_recovers_from_missing_line() -> None:
    """A dropped sync pulse mid-track should not derail subsequent detections.

    We synthesize 10 lines, then blank out the 5th line's sync pulse by
    overwriting it with mid-luma. The detector should still return 10
    line starts (with the missing one filled in by the predicted slot).
    """
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    track = _synthesize_line_freq_track(spec, num_lines=10, fs=fs)
    line_samples = int(round(spec.line_time_ms / 1000.0 * fs))
    sync_samples = int(round(spec.sync_pulse_ms / 1000.0 * fs))
    blank_start = 4 * line_samples  # 5th line (0-indexed)
    track[blank_start : blank_start + sync_samples] = 1900.0

    starts = find_line_starts(track, fs, spec)
    assert len(starts) == 10
    # The recovered 5th-line index should still be on the grid.
    expected = starts[0] + 4 * line_samples
    assert abs(starts[4] - expected) < int(0.005 * fs)


def test_find_line_starts_survives_click_noise_inside_syncs() -> None:
    """Click noise inside sync pulses must not chop them into sub-threshold runs.

    FM demodulation of a noisy narrow-band signal produces occasional
    large phase-slip "click" spikes in the instantaneous-frequency track
    — short (a handful of samples) but far outside the 1100–1300 Hz
    sync band. A boxcar pre-smoother averages those spikes *into* the
    sync band, dragging the smoothed track above the 1300 Hz threshold
    at the click location and splitting one legitimate sync run into
    two sub-threshold runs that the length filter then rejects.

    The Phase 2.5 median pre-smoother rejects these spikes outright as
    long as each click is shorter than half the window. This test
    regresses that by injecting two 5-sample 3500 Hz spikes into every
    sync pulse of a clean synthetic Robot 36 grid and asserting the
    full line count still comes back.
    """
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    track = _synthesize_line_freq_track(spec, num_lines=spec.height, fs=fs)
    line_samples = int(round(spec.line_time_ms / 1000.0 * fs))
    sync_samples = int(round(spec.sync_pulse_ms / 1000.0 * fs))
    click_len = 5
    for line in range(spec.height):
        sync_start = line * line_samples
        for offset in (sync_samples // 3, 2 * sync_samples // 3):
            track[sync_start + offset : sync_start + offset + click_len] = 3500.0

    starts = find_line_starts(track, fs, spec)
    assert len(starts) == spec.height


# === integration tests against real PySSTV-encoded WAVs ===


@pytest.fixture(scope="module")
def gradient_image() -> Image.Image:
    img = Image.new("RGB", (320, 256), color=(0, 0, 0))
    pixels = img.load()
    assert pixels is not None
    for x in range(320):
        for y in range(256):
            pixels[x, y] = (x * 255 // 319, y * 255 // 255, 128)
    return img


#: PySSTV rounds each sub-segment of a scan line (sync, porches, scans) to
#: an integer number of samples, so successive lines alternate between
#: rounding-up and rounding-down by ~half the residual. Empirically this
#: yields ±2-3 samples of line-spacing jitter even on a perfect WAV.
#: Synthetic-grid tests above pin the algorithm's true resolution (<1
#: sample); this looser bound is for the real-world encoder.
_PYSSTV_LINE_SPACING_SAMPLE_TOLERANCE: float = 5.0


@pytest.mark.parametrize(
    "mode",
    [Mode.ROBOT_36, Mode.MARTIN_M1, Mode.SCOTTIE_S1],
)
def test_find_line_starts_against_real_pysstv_encoding(
    mode: Mode, gradient_image: Image.Image
) -> None:
    """Encode a real image, run the full pipeline, assert the line grid.

    The synthetic-grid tests above already pin the detector's intrinsic
    resolution (<1 sample std-dev). This test catches a different class of
    bug: any drift between PySSTV's idea of where lines start and our
    detector's idea. PySSTV's per-segment integer-sample rounding adds
    a few samples of natural jitter, so the bound here is correspondingly
    looser than the synthetic case.
    """
    fs = 48_000
    spec = MODE_TABLE[mode]
    samples_int16 = encode(gradient_image, mode, sample_rate=fs)
    samples = samples_int16.astype(np.float64) / 32768.0

    inst = instantaneous_frequency(samples, fs)
    vis_result = detect_vis(samples, fs)
    assert vis_result is not None, f"VIS not detected in real {mode.value} encoding"
    _, vis_end = vis_result

    starts = find_line_starts(inst, fs, spec, start_idx=vis_end)

    # Expected: every line of the image.
    assert len(starts) == spec.height, (
        f"{mode.value}: expected {spec.height} lines, got {len(starts)}"
    )

    diffs = np.diff(np.asarray(starts, dtype=np.float64))
    expected_spacing = spec.line_time_ms / 1000.0 * fs
    # Mean spacing should match the nominal line time within ~0.1%.
    np.testing.assert_allclose(diffs.mean(), expected_spacing, rtol=0.001)
    # Std-dev should sit within a few samples (PySSTV's quantization jitter).
    assert diffs.std() < _PYSSTV_LINE_SPACING_SAMPLE_TOLERANCE, (
        f"{mode.value}: line spacing std-dev {diffs.std():.3f} samples "
        f"exceeds the {_PYSSTV_LINE_SPACING_SAMPLE_TOLERANCE} sample bound"
    )


def test_find_line_starts_robot36_spacing_is_150ms(
    gradient_image: Image.Image,
) -> None:
    """Plan-specific check: Robot 36 lines must be ~150 ms apart.

    The plan calls out 240 lines spaced ~150 ms apart specifically for
    Robot 36; this test pins that exact number so a future regression
    in the line-time math gets caught here rather than in a vague
    spacing-mismatch error from the parametrized test above.
    """
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    samples_int16 = encode(gradient_image, Mode.ROBOT_36, sample_rate=fs)
    samples = samples_int16.astype(np.float64) / 32768.0

    inst = instantaneous_frequency(samples, fs)
    vis_result = detect_vis(samples, fs)
    assert vis_result is not None
    _, vis_end = vis_result

    starts = find_line_starts(inst, fs, spec, start_idx=vis_end)
    assert len(starts) == 240

    diffs_ms = np.diff(np.asarray(starts, dtype=np.float64)) / fs * 1000.0
    np.testing.assert_allclose(diffs_ms.mean(), 150.0, rtol=0.005)
    # Sub-sample equivalent of the synthetic bound: 5 samples @ 48 kHz = ~0.1 ms.
    assert diffs_ms.std() < 0.15
