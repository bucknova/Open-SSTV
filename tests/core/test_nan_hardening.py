# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.4.1 audit high #3: non-finite audio must never crash or wedge
the decoder.  NaN passes ``<``/``>`` range checks (NaN comparisons are
False) and ``int(round(nan))`` raises — previously the offending line
window was never consumed, so the streaming decoder re-raised on every
subsequent feed forever."""
from __future__ import annotations

import numpy as np

from open_sstv.core.decoder import Decoder, _sanitize_audio, decode_wav
from open_sstv.core.robot36_dsp import sample_pixel


class TestSanitizeAudio:
    def test_finite_passthrough_is_same_object(self) -> None:
        arr = np.zeros(64, dtype=np.float64)
        assert _sanitize_audio(arr) is arr  # no copy on the hot path

    def test_nan_and_inf_become_silence(self) -> None:
        arr = np.array([0.5, np.nan, np.inf, -np.inf, -0.5])
        out = _sanitize_audio(arr)
        assert np.isfinite(out).all()
        assert out[0] == 0.5 and out[4] == -0.5
        assert out[1] == out[2] == out[3] == 0.0


class TestSamplerGuards:
    def test_robot36_sample_pixel_nan_is_black(self) -> None:
        inst = np.full(64, np.nan)
        assert sample_pixel(inst, 32.0, 64) == 0


class TestDecoderFeedNaN:
    def test_feed_nan_chunk_does_not_raise(self) -> None:
        dec = Decoder(fs=48_000)
        chunk = np.full(4800, np.nan)
        events = dec.feed(chunk)  # must not raise
        # And the decoder keeps working afterwards — the wedge signature
        # was every subsequent feed re-raising.
        for _ in range(5):
            dec.feed(np.zeros(4800))
        assert isinstance(events, list)

    def test_decode_wav_with_nan_does_not_raise(self) -> None:
        rng = np.random.default_rng(42)
        arr = rng.standard_normal(48_000)
        arr[1000:1010] = np.nan
        assert decode_wav(arr, 48_000) is None  # noise + NaN → no VIS, no crash
