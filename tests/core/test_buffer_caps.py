# SPDX-License-Identifier: GPL-3.0-or-later
"""v0.4.1 audit medium #10: a stalled decode (VIS lock + dead carrier)
must not grow memory without bound.  Reproduces the audit scenario at
8 kHz to keep the test fast: real Robot 36 VIS, then silence for well
past the cap."""
from __future__ import annotations

import numpy as np

from open_sstv.core.decoder import Decoder, ImageStarted
from open_sstv.core.encoder import encode
from open_sstv.core.modes import MODE_TABLE, Mode

_FS = 8_000


def _vis_then_silence_decoder() -> Decoder:
    """Feed a real VIS then silence until well past the DECODING cap."""
    from PIL import Image

    audio = encode(
        Image.new("RGB", (320, 240), color=(90, 60, 30)),
        Mode.ROBOT_36,
        sample_rate=_FS,
    ).astype(np.float64) / 32767.0

    dec = Decoder(fs=_FS)
    started = False
    # 2 s of real signal (leader + VIS + first lines)…
    lead = audio[: 2 * _FS]
    for i in range(0, lead.size, _FS // 2):
        for ev in dec.feed(lead[i : i + _FS // 2]):
            if isinstance(ev, ImageStarted):
                started = True
    assert started, "VIS must lock before the stall is simulated"

    # …then dead carrier far beyond the cap (~65 s for Robot 36).
    silence = np.zeros(_FS // 2)
    total_s = int(MODE_TABLE[Mode.ROBOT_36].total_duration_s * 3)
    for _ in range(total_s * 2):
        dec.feed(silence)
    return dec


def test_stalled_decode_buffers_stay_bounded() -> None:
    dec = _vis_then_silence_decoder()
    spec = MODE_TABLE[Mode.ROBOT_36]
    cap = int((spec.total_duration_s * 1.5 + 10.0) * _FS)

    front_total = sum(a.size for a in dec._buffer)
    assert front_total <= cap + _FS, (
        f"Decoder buffer {front_total} samples exceeds cap {cap}"
    )
    assert dec._retained_buffer_trimmed is True

    backend = dec._incremental_dec
    assert backend is not None
    # Robot 36 wraps the real backend in a format-detection shim whose
    # own _pending buffer is bounded by design (3 s fallback) — the
    # unbounded-growth risk lives in the inner IncrementalDecoderBase.
    inner = getattr(backend, "_backend", backend)
    assert inner is not None, "detection fallback must have selected a backend"
    backend_cap = (
        int(inner._grid_len * inner._line_samp * 1.5)
        + inner._g_lookback
        + inner.FILTER_MARGIN
    )
    assert len(inner._buf) <= backend_cap + _FS, (
        f"backend buffer {len(inner._buf)} exceeds cap {backend_cap}"
    )


def test_normal_decode_keeps_full_fidelity_buffer() -> None:
    # A clean, complete decode must still offer last_complete_buffer —
    # the cap only withholds it when trimming actually happened.
    from PIL import Image

    audio = encode(
        Image.new("RGB", (320, 240), color=(10, 200, 120)),
        Mode.ROBOT_36,
        sample_rate=_FS,
    ).astype(np.float64) / 32767.0
    dec = Decoder(fs=_FS)
    for i in range(0, audio.size, _FS):
        dec.feed(audio[i : i + _FS])
    dec.feed(np.zeros(_FS))  # flush tail
    assert dec.last_complete_buffer() is not None
