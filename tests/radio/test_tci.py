# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.radio.tci``.

All tests are hardware-free: no real WebSocket connection is opened.
The suite covers the three areas Kevin flagged in the PR review:

* ``TciConnection.send_tx_audio_chunk`` — silence dither (AetherSDR
  stereo-detection workaround) and audio header layout.
* ``TciRig._on_text`` — VFO A/B filtering, TRX index filtering, READY
  handling, and malformed input resilience.
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from open_sstv.radio.tci import TciConnection, TciRig


# ---------------------------------------------------------------------------
# send_tx_audio_chunk — silence dither
# ---------------------------------------------------------------------------

class TestSendTxAudioChunkSilenceDither:
    """The AetherSDR stereo-detection heuristic checks whether consecutive
    sample pairs differ by < 1e-6.  All-zero silence (CW inter-element gaps)
    triggers this test and is processed as n/2 stereo pairs at half the
    expected duration — corrupting CW timing.  The fix replaces every
    near-zero sample with alternating ±1e-5 dither so no even/odd pair is
    ever equal."""

    def _make_conn(self) -> tuple[TciConnection, list[bytes]]:
        """Return a TciConnection whose send_binary() records payloads."""
        sent: list[bytes] = []
        conn = TciConnection.__new__(TciConnection)
        conn._sample_rate = 48_000
        conn._send_lock = __import__("threading").Lock()
        conn._ws = type("FakeWS", (), {"send_binary": lambda self, d: sent.append(d)})()
        return conn, sent

    def test_silence_chunk_has_no_consecutive_equal_pairs(self) -> None:
        """All-zero input must produce no pair (samples[2i], samples[2i+1]) == (x, x)."""
        conn, sent = self._make_conn()
        silence = np.zeros(2048, dtype=np.float32)
        conn.send_tx_audio_chunk(silence, sample_rate=48_000)

        header_size = 64
        payload = np.frombuffer(sent[0][header_size:], dtype=np.float32)
        assert len(payload) == 2048
        # No consecutive even/odd pair should be identical.
        even = payload[0::2]
        odd = payload[1::2]
        assert not np.any(np.abs(even - odd) < 1e-6), (
            "Silence dither failed: at least one even/odd pair is equal — "
            "AetherSDR will misclassify the chunk as stereo"
        )

    def test_dither_is_inaudible(self) -> None:
        """Dither amplitude must be well below -90 dBFS."""
        conn, sent = self._make_conn()
        conn.send_tx_audio_chunk(np.zeros(512, dtype=np.float32), sample_rate=48_000)
        payload = np.frombuffer(sent[0][64:], dtype=np.float32)
        max_amp = float(np.max(np.abs(payload)))
        assert max_amp < 1e-4, f"Dither amplitude {max_amp:.2e} is too large"

    def test_tone_chunk_passes_through_unchanged(self) -> None:
        """Samples that are not near-zero must be byte-identical after dither.

        A 800 Hz sine at 48 kHz has exact zero-crossings (every 30 samples).
        Those samples legitimately trigger the dither, so we only assert that
        samples whose original magnitude was >= 1e-6 are preserved exactly.
        Dithered samples must remain within the inaudible ±1e-5 band.
        """
        conn, sent = self._make_conn()
        t = np.arange(4800, dtype=np.float32) / 48_000
        tone = (np.sin(2 * np.pi * 800 * t) * 0.9).astype(np.float32)
        conn.send_tx_audio_chunk(tone, sample_rate=48_000)
        payload = np.frombuffer(sent[0][64:], dtype=np.float32)

        loud = np.abs(tone) >= 1e-6
        np.testing.assert_array_equal(payload[loud], tone[loud])
        assert np.all(np.abs(payload[~loud]) <= 1e-5), (
            "Dithered zero-crossing samples exceeded ±1e-5"
        )

    def test_mixed_chunk_silence_portion_is_dithered(self) -> None:
        """A chunk with tone then silence: the silent half must be dithered,
        and the resulting full chunk must have no consecutive equal pairs."""
        conn, sent = self._make_conn()
        t = np.arange(2400, dtype=np.float32) / 48_000
        tone = (np.sin(2 * np.pi * 800 * t) * 0.9).astype(np.float32)
        mixed = np.concatenate([tone, np.zeros(2400, dtype=np.float32)])
        conn.send_tx_audio_chunk(mixed, sample_rate=48_000)
        payload = np.frombuffer(sent[0][64:], dtype=np.float32)
        even, odd = payload[0::2], payload[1::2]
        assert not np.any(np.abs(even - odd) < 1e-6)


# ---------------------------------------------------------------------------
# send_tx_audio_chunk — header layout
# ---------------------------------------------------------------------------

class TestSendTxAudioChunkHeader:
    """The 64-byte TciAudioHeader must have the correct field values so
    AetherSDR routes the frame to the TX pipeline."""

    _HEADER_FMT = "<16I"  # 16 × uint32 LE

    def _capture_header(self, samples: np.ndarray, rate: int = 48_000) -> tuple:
        sent: list[bytes] = []
        conn = TciConnection.__new__(TciConnection)
        conn._sample_rate = rate
        conn._send_lock = __import__("threading").Lock()
        conn._ws = type("FakeWS", (), {"send_binary": lambda self, d: sent.append(d)})()
        conn.send_tx_audio_chunk(samples, sample_rate=rate)
        return struct.unpack_from(self._HEADER_FMT, sent[0], 0)

    def test_type_field_is_tx_audio(self) -> None:
        fields = self._capture_header(np.ones(256, dtype=np.float32))
        type_field = fields[6]
        assert type_field == 2, f"Expected type=2 (TX_AUDIO), got {type_field}"

    def test_format_field_is_float32(self) -> None:
        fields = self._capture_header(np.ones(256, dtype=np.float32))
        assert fields[2] == 3, "Expected format=3 (float32)"

    def test_channels_field_is_mono(self) -> None:
        fields = self._capture_header(np.ones(256, dtype=np.float32))
        assert fields[7] == 1, "Expected channels=1 (mono)"

    def test_length_matches_sample_count(self) -> None:
        n = 4800
        fields = self._capture_header(np.ones(n, dtype=np.float32))
        assert fields[5] == n, f"Expected length={n}, got {fields[5]}"

    def test_sample_rate_uses_caller_rate_not_rx_rate(self) -> None:
        """The declared rate must be the generation rate passed by the caller,
        not self._sample_rate (which is the RX rate from rx_samplerate:)."""
        sent: list[bytes] = []
        conn = TciConnection.__new__(TciConnection)
        conn._sample_rate = 24_000  # simulate server reporting 24 kHz RX
        conn._send_lock = __import__("threading").Lock()
        conn._ws = type("FakeWS", (), {"send_binary": lambda self, d: sent.append(d)})()
        conn.send_tx_audio_chunk(np.ones(256, dtype=np.float32), sample_rate=48_000)
        fields = struct.unpack_from(self._HEADER_FMT, sent[0], 0)
        assert fields[1] == 48_000, (
            f"Header declared rate {fields[1]} Hz but caller specified 48000 Hz — "
            "RX rate must not override TX rate"
        )


# ---------------------------------------------------------------------------
# TciRig._on_text — parser correctness
# ---------------------------------------------------------------------------

def _make_rig() -> TciRig:
    """Return a TciRig with a stub connection (no WebSocket)."""
    rig = TciRig.__new__(TciRig)
    rig._last_freq = 0
    rig._last_mode = ""
    rig._last_ptt = False
    rig._strength = 0
    rig._freq_received = False
    rig._mode_received = False
    rig._ptt_received = False
    rig._freq_event = __import__("threading").Event()
    rig._mode_event = __import__("threading").Event()
    rig._ptt_event = __import__("threading").Event()
    return rig


class TestOnTextParser:
    def test_vfo_a_updates_freq(self) -> None:
        rig = _make_rig()
        rig._on_text("vfo:0,0,14074000")
        assert rig._last_freq == 14_074_000
        assert rig._freq_received

    def test_vfo_b_does_not_update_freq(self) -> None:
        """VFO B events (index 1) must be silently ignored — M6 fix."""
        rig = _make_rig()
        rig._last_freq = 14_074_000
        rig._freq_received = True
        rig._on_text("vfo:0,1,7074000")  # VFO B
        assert rig._last_freq == 14_074_000, "VFO B overwrote VFO A frequency"

    def test_trx_false_updates_ptt(self) -> None:
        rig = _make_rig()
        rig._on_text("trx:0,false")
        assert rig._ptt_received
        assert rig._last_ptt is False

    def test_trx_true_tci_updates_ptt(self) -> None:
        """trx:0,true,tci; — three-field form used for TCI keying."""
        rig = _make_rig()
        rig._on_text("trx:0,true,tci")
        assert rig._ptt_received
        assert rig._last_ptt is True

    def test_trx_secondary_trx_ignored(self) -> None:
        """TRX 1 events must not affect TRX 0 PTT state."""
        rig = _make_rig()
        rig._on_text("trx:1,true")
        assert not rig._ptt_received, "TRX 1 event updated TRX 0 PTT state"

    def test_modulation_updates_mode(self) -> None:
        rig = _make_rig()
        rig._on_text("modulation:0,USB")
        assert rig._last_mode == "USB"
        assert rig._mode_received

    def test_malformed_vfo_does_not_raise(self) -> None:
        rig = _make_rig()
        rig._on_text("vfo:0,0,notanumber")
        assert not rig._freq_received

    def test_truncated_trx_does_not_raise(self) -> None:
        rig = _make_rig()
        rig._on_text("trx:0")
        assert not rig._ptt_received

    def test_unknown_event_is_ignored(self) -> None:
        rig = _make_rig()
        rig._on_text("some_future_event:0,value")
        # No exception; no state change.
        assert not rig._freq_received
        assert not rig._ptt_received
