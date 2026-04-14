# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.core.vis.detect_vis``.

Two test layers:

* **Synthetic** — directly fabricate a VIS-header buffer with NumPy. Lets
  us cover edge cases (parity mismatch, missing stop bit, leading silence,
  noisy header) without spending time inside PySSTV.
* **Integration** — encode a real image with PySSTV in each of our v1
  modes and assert ``detect_vis`` recovers the right code from the WAV.
  This is the gold standard: if the synthetic tests pass but the
  integration tests fail, our model of the VIS frequency / timing /
  endianness is wrong.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.encoder import encode
from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.core.vis import (
    VIS_BIT0_HZ,
    VIS_BIT1_HZ,
    VIS_BIT_DURATION_S,
    VIS_BREAK_DURATION_S,
    VIS_LEADER_DURATION_S,
    VIS_LEADER_HZ,
    VIS_SYNC_HZ,
    detect_vis,
)


# === helpers ===


def _phase_continuous(segments: list[tuple[float, float]], fs: int) -> np.ndarray:
    """Synthesize a phase-continuous concatenation of (freq_hz, duration_s) segments.

    Phase continuity is what makes a real FM signal look smooth to a Hilbert-
    based demodulator; if we just stitched independent sinusoids the
    instantaneous-frequency track would have nasty discontinuities at the
    boundaries that the smoother can't hide.
    """
    out: list[np.ndarray] = []
    phase = 0.0
    for freq, dur in segments:
        n = int(round(dur * fs))
        if n == 0:
            continue
        t = np.arange(n) / fs
        out.append(np.sin(2 * np.pi * freq * t + phase))
        phase = (phase + 2 * np.pi * freq * (n / fs)) % (2 * np.pi)
    return np.concatenate(out).astype(np.float64)


def _synthesize_vis(vis_code: int, fs: int = 48_000) -> np.ndarray:
    """Build a VIS-header-only audio buffer for the given code.

    Mirrors PySSTV's ``gen_freq_bits`` exactly: 300 ms leader, 10 ms break,
    300 ms leader, 30 ms start bit, 7 LSB-first data bits, even-parity bit,
    30 ms stop bit. No image data follows.
    """
    segments: list[tuple[float, float]] = [
        (VIS_LEADER_HZ, VIS_LEADER_DURATION_S),
        (VIS_SYNC_HZ, VIS_BREAK_DURATION_S),
        (VIS_LEADER_HZ, VIS_LEADER_DURATION_S),
        (VIS_SYNC_HZ, VIS_BIT_DURATION_S),  # start bit
    ]
    code = vis_code
    num_ones = 0
    for _ in range(7):
        bit = code & 1
        code >>= 1
        num_ones += bit
        segments.append(
            (VIS_BIT1_HZ if bit == 1 else VIS_BIT0_HZ, VIS_BIT_DURATION_S)
        )
    parity_freq = VIS_BIT1_HZ if num_ones % 2 == 1 else VIS_BIT0_HZ
    segments.append((parity_freq, VIS_BIT_DURATION_S))
    segments.append((VIS_SYNC_HZ, VIS_BIT_DURATION_S))  # stop bit
    return _phase_continuous(segments, fs)


# === synthetic tests ===


@pytest.mark.parametrize(
    "vis_code",
    [
        0x08,  # Robot 36
        0x2C,  # Martin M1
        0x3C,  # Scottie S1
        0x00,  # all zeros (parity = 0)
        0x7F,  # all ones (parity = 1)
        0x55,  # alternating
    ],
)
def test_detect_vis_synthetic_round_trip(vis_code: int) -> None:
    audio = _synthesize_vis(vis_code)
    result = detect_vis(audio, 48_000)
    assert result is not None
    code, end_idx = result
    assert code == vis_code
    # The end index should be within the buffer.
    assert 0 < end_idx <= len(audio)


def test_detect_vis_returns_none_for_silence() -> None:
    silence = np.zeros(48_000, dtype=np.float64)  # 1 second of nothing
    assert detect_vis(silence, 48_000) is None


def test_detect_vis_returns_none_for_noise() -> None:
    rng = np.random.default_rng(seed=42)
    noise = rng.standard_normal(48_000).astype(np.float64) * 0.1
    assert detect_vis(noise, 48_000) is None


def test_detect_vis_returns_none_for_empty_buffer() -> None:
    assert detect_vis(np.array([], dtype=np.float64), 48_000) is None


def test_detect_vis_returns_none_for_2d_input() -> None:
    assert detect_vis(np.zeros((10, 2), dtype=np.float64), 48_000) is None


def test_detect_vis_tolerates_leading_silence() -> None:
    """A real RX feed has audio long before the VIS header — make sure
    leading silence doesn't break detection."""
    audio = np.concatenate(
        [np.zeros(int(0.5 * 48_000), dtype=np.float64), _synthesize_vis(0x2C)]
    )
    result = detect_vis(audio, 48_000)
    assert result is not None
    assert result[0] == 0x2C


def test_detect_vis_rejects_buffer_with_only_partial_vis() -> None:
    """If the buffer cuts off mid-VIS (no stop bit), we should fail
    cleanly rather than return a half-decoded code."""
    full = _synthesize_vis(0x2C)
    # Drop the last 100 ms (kills the stop bit and parity).
    truncated = full[: -int(0.1 * 48_000)]
    assert detect_vis(truncated, 48_000) is None


def test_detect_vis_returns_end_index_after_stop_bit() -> None:
    audio = _synthesize_vis(0x08)
    result = detect_vis(audio, 48_000)
    assert result is not None
    _, end_idx = result
    # The buffer is exactly the VIS header — end_idx should land at the
    # very end (within ~1 ms slop for boundary alignment).
    assert abs(end_idx - len(audio)) < int(0.001 * 48_000)


def test_detect_vis_rejects_corrupted_parity() -> None:
    """Flip the parity bit in a synthetic header — detection should fail."""
    # Build the segments manually so we can corrupt the parity.
    vis_code = 0x2C  # Martin M1, num_ones = 3 (odd) => parity should be 1100 Hz
    fs = 48_000
    segments: list[tuple[float, float]] = [
        (VIS_LEADER_HZ, VIS_LEADER_DURATION_S),
        (VIS_SYNC_HZ, VIS_BREAK_DURATION_S),
        (VIS_LEADER_HZ, VIS_LEADER_DURATION_S),
        (VIS_SYNC_HZ, VIS_BIT_DURATION_S),
    ]
    code = vis_code
    for _ in range(7):
        bit = code & 1
        code >>= 1
        segments.append(
            (VIS_BIT1_HZ if bit == 1 else VIS_BIT0_HZ, VIS_BIT_DURATION_S)
        )
    # CORRUPTED parity: send 1300 Hz where it should be 1100 Hz.
    segments.append((VIS_BIT0_HZ, VIS_BIT_DURATION_S))
    segments.append((VIS_SYNC_HZ, VIS_BIT_DURATION_S))
    audio = _phase_continuous(segments, fs)
    assert detect_vis(audio, fs) is None


def test_detect_vis_44_1khz_sample_rate() -> None:
    """Same logic should work at the other common audio rate."""
    audio = _synthesize_vis(0x08, fs=44_100)
    result = detect_vis(audio, 44_100)
    assert result is not None
    assert result[0] == 0x08


# === integration tests against real PySSTV-encoded WAVs ===


@pytest.fixture(scope="module")
def gradient_image() -> Image.Image:
    # A simple horizontal gradient — content doesn't matter for VIS detection,
    # we just need a real image to feed to PySSTV.
    img = Image.new("RGB", (320, 256), color=(0, 0, 0))
    pixels = img.load()
    assert pixels is not None
    for x in range(320):
        for y in range(256):
            pixels[x, y] = (x * 255 // 319, y * 255 // 255, 128)
    return img


@pytest.mark.parametrize(
    "mode",
    [Mode.ROBOT_36, Mode.MARTIN_M1, Mode.SCOTTIE_S1],
)
def test_detect_vis_against_real_pysstv_encoder(
    mode: Mode, gradient_image: Image.Image
) -> None:
    """Encode an image with the actual PySSTV encoder and decode the VIS.

    This is the integration test the v1 plan calls out: if our model of
    the VIS frequencies, timings, or bit ordering is wrong, this is the
    test that catches it.
    """
    samples_int16 = encode(gradient_image, mode, sample_rate=48_000)
    # Convert to float64 in [-1, 1] for the detector. We deliberately do
    # this conversion in-line rather than via dsp_utils.to_mono_float32
    # so the test exercises detect_vis's amplitude-invariance directly.
    samples = samples_int16.astype(np.float64) / 32768.0

    result = detect_vis(samples, 48_000)
    assert result is not None, f"VIS not detected in real {mode.value} encoding"
    code, end_idx = result
    assert code == MODE_TABLE[mode].vis_code
    # The end index should be in the leader-plus-VIS region, well before
    # the bulk of the image data.
    leader_plus_vis_samples = int((0.6 + 0.3) * 48_000)
    assert end_idx < leader_plus_vis_samples + int(0.05 * 48_000)
