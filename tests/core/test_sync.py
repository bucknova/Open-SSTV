# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.sync``.

Two layers, mirroring ``test_vis.py``:

* **Synthetic** — fabricate frequency tracks directly with NumPy. We don't
  need real FM signals here because ``sync`` consumes a frequency track,
  not raw audio. Faster and easier to reason about than building a full
  phase-continuous signal for every test.
* **Integration** — encode a real image with PySSTV, run the full
  decode pipeline (audio → IF → ``detect_vis`` → candidates → grid walk)
  and assert the line-start grid matches the mode's expected geometry.
  This is the test the v1 plan calls for ("240 line starts spaced ~150 ms
  apart with std-dev < 1 sample").
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.demod import instantaneous_frequency
from open_sstv.core.encoder import encode
from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.core.sync import (
    find_sync_candidates,
    line0_sync_hint,
    walk_sync_grid,
)
from open_sstv.core.vis import detect_vis

# === helpers ===


def find_line_starts(freq_track, fs, spec, start_idx=0, *, hint=None):
    """Candidates → grid walk, the composition every decoder uses.

    (``sync.find_line_starts`` itself was test-only and was removed in
    the v0.6.14 audit cleanup; this keeps its tests meaningful.)
    """
    line_samples = spec.line_time_ms / 1000.0 * fs
    cands = find_sync_candidates(
        freq_track, fs, spec.sync_pulse_ms,
        line_period_samples=line_samples, start_idx=start_idx,
    )
    return walk_sync_grid(cands, line_samples, spec.height, first_slot_hint=hint)


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


def test_find_line_starts_survives_upward_frequency_offset() -> None:
    """A DC shift of the whole frequency track must not defeat sync detection.

    RX LO drift, Doppler, a deliberate FM offset between TX and RX, or a
    heavy-noise bias in the Hilbert demodulator can all shift the entire
    instantaneous-frequency track upward (or downward) by 100–200 Hz
    without changing the *relative* spacing between sync level and body
    level. The old hard ``1100 < f < 1300`` band collapsed completely at
    ~100 Hz of upward shift, because every sync pulse would land at
    1300+ Hz and fall out of the window.

    The Phase 2.5 adaptive threshold tracks the local sync floor inside
    a rolling window and applies a 1450 Hz hard cap as the back-stop, so
    up to ~250 Hz of upward shift is tolerated before the cap pins the
    threshold below the shifted sync level. This test constructs a
    Robot 36 track with every frequency bumped by +150 Hz (syncs at
    1350 Hz, body at 2050 Hz) and asserts the detector still finds all
    240 line starts.
    """
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    track = _synthesize_line_freq_track(spec, num_lines=spec.height, fs=fs)
    # Shift the whole track upward by 150 Hz. Sync pulses that used to
    # sit at 1200 Hz now sit at 1350 Hz — outside the old hard band but
    # still below the 1450 Hz adaptive cap.
    track = track + 150.0
    starts = find_line_starts(track, fs, spec)
    assert len(starts) == spec.height
    diffs = np.diff(np.asarray(starts, dtype=np.float64))
    expected_spacing = spec.line_time_ms / 1000.0 * fs
    np.testing.assert_allclose(diffs.mean(), expected_spacing, rtol=0.001)
    # Grid should still be tight — the shift doesn't affect relative timing.
    assert diffs.std() < 1.0


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


# === v0.6.14 audit D-H1: missed sync on line 0 / 1 ===


_P = 7200.0  # Robot 36 line period at 48 kHz
_HINT = 1000.0  # line-0 sync position (vis_end for LINE_START modes)


def _grid_cands(missing: set[int], n: int = 240) -> list[int]:
    return [int(_HINT + i * _P) for i in range(n) if i not in missing]


def test_walk_grid_missed_line1_sync_keeps_line0_anchor_without_hint() -> None:
    """Gap of 2·P out of line 0 must still anchor on line 0 (it used to
    anchor on line 2 and lose two rows)."""
    grid = walk_sync_grid(_grid_cands({1}), _P, 240)
    assert len(grid) == 240
    assert grid[0] == int(_HINT)
    assert grid[1] == int(round(_HINT + _P))
    assert grid[239] == int(_HINT + 239 * _P)


@pytest.mark.parametrize("missing", [{0}, {0, 1}, {1}, {0, 2}])
def test_walk_grid_extrapolates_missed_leading_syncs_from_hint(missing) -> None:
    grid = walk_sync_grid(_grid_cands(missing), _P, 240, first_slot_hint=_HINT)
    assert len(grid) == 240
    expected = [int(_HINT + i * _P) for i in range(240)]
    assert max(abs(g - e) for g, e in zip(grid, expected, strict=True)) <= 1


def test_walk_grid_hint_skips_off_lattice_leading_candidate() -> None:
    """A stray candidate between the VIS and line 0 that happens to be
    one period before line 1 (line 0 missed) used to become row 0."""
    cands = [int(_HINT + 0.4 * _P)] + _grid_cands({0})
    grid = walk_sync_grid(cands, _P, 240, first_slot_hint=_HINT)
    assert abs(grid[0] - _HINT) <= 1
    assert abs(grid[1] - (_HINT + _P)) <= 1


def test_line0_hint_scottie_offset() -> None:
    spec = MODE_TABLE[Mode.SCOTTIE_S1]
    # sep + G + sep + B = 1.5 + 138.24 + 1.5 + 138.24 ms
    assert line0_sync_hint(spec, 48_000, 0) == pytest.approx(279.48 * 48)
    assert line0_sync_hint(MODE_TABLE[Mode.MARTIN_M1], 48_000, 123) == 123.0


@pytest.mark.parametrize("missing", [{0}, {1}, {0, 1}])
def test_robot36_real_audio_missed_leading_sync_keeps_rows_and_parity(missing) -> None:
    """End to end on real encoder audio: blank the line-0/1 sync pulses
    and the grid must still start at line 0 (so Robot 36 even rows stay
    Cr, odd rows Cb)."""
    fs = 48_000
    spec = MODE_TABLE[Mode.ROBOT_36]
    audio = encode(Image.new("RGB", (320, 240), (90, 160, 30)), Mode.ROBOT_36,
                   sample_rate=fs).astype(np.float64) / 32768.0
    inst = instantaneous_frequency(audio, fs)
    _code, vis_end = detect_vis(audio, fs)
    clean = find_line_starts(inst, fs, spec, start_idx=vis_end)
    n_sync = int(spec.sync_pulse_ms / 1000 * fs)
    damaged = inst.copy()
    for line in missing:
        s = clean[line]
        damaged[s - 50:s + n_sync + 50] = 1500.0
    hint = line0_sync_hint(spec, fs, vis_end)
    got = find_line_starts(damaged, fs, spec, start_idx=vis_end, hint=hint)
    assert len(got) == spec.height
    assert max(abs(a - b) for a, b in zip(got, clean, strict=True)) <= 3


# === v0.6.14 audit: sync start timing (line-0 offset, D-L3) ===


def test_clipped_leading_run_takes_start_from_trailing_edge() -> None:
    """A run in progress at ``start_idx`` (line 0 abutting the VIS stop
    bit) has no visible leading edge; its start must come from the
    trailing edge so it's on the same footing as every other line (it
    used to land ~0.3 ms early on real audio)."""
    fs = 48_000
    spec = MODE_TABLE[Mode.MARTIN_M4]
    track = _synthesize_line_freq_track(spec, 6, fs)
    # Prepend a 30 ms "stop bit" at sync level, then search from inside
    # the merged run, 1 ms after the true sync start.
    stop = np.full(int(0.030 * fs), 1200.0)
    full = np.concatenate([stop, track])
    line_samples = spec.line_time_ms / 1000 * fs
    cands = find_sync_candidates(
        full, fs, spec.sync_pulse_ms, line_samples,
        start_idx=stop.size + int(0.001 * fs),
    )
    # (The synthetic track has instant steps, so the ~4-sample trailing-
    # edge lag correction tuned for real bandpassed audio shows up here.)
    assert abs(cands[0] - stop.size) <= 5
    assert abs((cands[1] - cands[0]) - line_samples) <= 5


@pytest.mark.parametrize(
    "mode", [Mode.MARTIN_M4, Mode.SCOTTIE_S4, Mode.ROBOT_36, Mode.PD_50, Mode.WRAASE_SC2_120]
)
def test_sync_candidates_are_unbiased_on_encoder_audio(mode: Mode) -> None:
    """Candidate starts sit within ~1 sample of the encoder's true sync
    starts for every mode family. The leading-edge threshold crossing was
    ~10 samples (0.2 ms) late whenever the edge came down from image
    content, which a porch-less layout (spec Scottie: B runs straight into
    the sync — v0.6.14 audit D-L3) turns into a corrupted last B pixel."""
    from open_sstv.core.decoder import _bandpass  # noqa: PLC0415
    from open_sstv.core.encoder import _PYSSTV_CLASSES  # noqa: PLC0415

    fs = 48_000
    spec = MODE_TABLE[mode]
    cls = _PYSSTV_CLASSES[mode]
    rng = np.random.default_rng(7)
    img = Image.fromarray(rng.integers(0, 256, (cls.HEIGHT, cls.WIDTH, 3), dtype=np.uint8))
    sstv = cls(img, fs, 16)
    t_ms = 0.0
    true: list[float] = []
    for freq, dur in sstv.gen_freq_bits():
        if freq == 1200 and abs(dur - spec.sync_pulse_ms) < 1e-6:
            true.append(t_ms * fs / 1000)
        t_ms += dur
    audio = np.fromiter(sstv.gen_samples(), dtype=np.int16) / 32768.0
    inst = instantaneous_frequency(_bandpass(audio, fs), fs)
    cands = np.asarray(find_sync_candidates(
        inst, fs, spec.sync_pulse_ms, spec.line_time_ms / 1000 * fs,
        start_idx=int(true[0]) - 100,
    ))
    err = np.array([cands[np.argmin(np.abs(cands - x))] - x for x in true])
    assert abs(err.mean()) < 2.0
    assert np.abs(err).max() < 8.0
