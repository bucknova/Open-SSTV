# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.ui.workers.TxWorker``.

Run on the test thread, with ``encode`` and ``play_blocking`` patched out
so the worker completes in milliseconds rather than the ~115 s a real
Martin M1 transmission would take. The ``qapp`` fixture (provided by
pytest-qt) ensures a ``QApplication`` exists for the QObject base class.

These tests are marked ``gui`` because they require a Qt application;
``pytest -m "not gui"`` will skip them on truly headless workers without
even an offscreen Qt platform.
"""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig
from open_sstv.radio.exceptions import RigConnectionError
from open_sstv.ui.workers import TxWorker, _CW_GAP_S, _make_two_tone, _TEST_TONE_DURATION_S

pytestmark = pytest.mark.gui


@pytest.fixture
def fake_samples() -> np.ndarray:
    return np.zeros(100, dtype=np.int16)


@pytest.fixture(autouse=True)
def patch_encode_and_playback(
    monkeypatch: pytest.MonkeyPatch, fake_samples: np.ndarray
) -> Iterator[dict[str, MagicMock]]:
    """Replace ``encode`` with an instant no-op and ``play_blocking`` with
    a mock that doesn't touch the audio device. Tests that need to
    inspect call args grab the mocks from the yielded dict."""
    encode_mock = MagicMock(return_value=fake_samples)
    play_mock = MagicMock()
    stop_mock = MagicMock()
    monkeypatch.setattr("open_sstv.ui.workers.encode", encode_mock)
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.play_blocking", play_mock)
    monkeypatch.setattr("open_sstv.ui.workers.output_stream.stop", stop_mock)
    yield {"encode": encode_mock, "play": play_mock, "stop": stop_mock}


@pytest.fixture
def gradient_image() -> Image.Image:
    return Image.new("RGB", (100, 100), color=(128, 128, 128))


def _record_signals(worker: TxWorker) -> dict[str, list]:
    """Subscribe to every TxWorker signal and stash payloads in lists."""
    log: dict[str, list] = {
        "started": [],
        "complete": [],
        "aborted": [],
        "error": [],
    }
    worker.transmission_started.connect(lambda: log["started"].append(True))
    worker.transmission_complete.connect(lambda: log["complete"].append(True))
    worker.transmission_aborted.connect(lambda: log["aborted"].append(True))
    worker.error.connect(lambda msg: log["error"].append(msg))
    return log


# === happy paths ===


def test_transmit_emits_started_then_complete(
    qapp, gradient_image: Image.Image
) -> None:
    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    assert log["started"] == [True]
    assert log["complete"] == [True]
    assert log["aborted"] == []
    assert log["error"] == []


def test_transmit_calls_play_blocking_with_encoded_samples(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    fake_samples: np.ndarray,
) -> None:
    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0, sample_rate=48_000)
    worker.transmit(gradient_image, Mode.ROBOT_36)

    play = patch_encode_and_playback["play"]
    play.assert_called_once()
    args, kwargs = play.call_args
    # play_blocking(samples, sample_rate, device=...)
    np.testing.assert_array_equal(args[0], fake_samples)
    assert args[1] == 48_000
    assert kwargs["device"] is None


def test_transmit_keys_and_unkeys_rig(
    qapp, gradient_image: Image.Image
) -> None:
    rig = MagicMock(spec=["set_ptt", "open", "close"])
    worker = TxWorker(rig=rig, ptt_delay_s=0)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    # Two PTT calls in order: True, then False.
    assert [c.args for c in rig.set_ptt.call_args_list] == [(True,), (False,)]


# === stop / abort ===


def test_request_stop_sets_event_and_calls_sd_stop(
    qapp, patch_encode_and_playback: dict[str, MagicMock]
) -> None:
    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0)
    worker.request_stop()

    patch_encode_and_playback["stop"].assert_called_once()
    assert worker._stop_event.is_set()


def test_stop_during_ptt_delay_aborts_before_audio(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user clicks Stop while the worker is in the PTT settle
    delay, no audio should be played — but PTT must still be unkeyed.

    We simulate "stop pressed during the sleep" by replacing
    ``time.sleep`` with a function that sets the stop flag, since
    ``transmit`` clears the flag at entry so the test can't pre-set it."""
    rig = MagicMock(spec=["set_ptt"])
    worker = TxWorker(rig=rig, ptt_delay_s=0.1)
    log = _record_signals(worker)

    def stop_during_sleep(_secs: float) -> None:
        worker._stop_event.set()

    monkeypatch.setattr("open_sstv.ui.workers.time.sleep", stop_during_sleep)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    patch_encode_and_playback["play"].assert_not_called()
    assert log["aborted"] == [True]
    assert log["complete"] == []
    # PTT must still be cycled cleanly even on abort.
    assert [c.args for c in rig.set_ptt.call_args_list] == [(True,), (False,)]


# === error paths ===


def test_encode_failure_emits_error_and_does_not_key(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
) -> None:
    patch_encode_and_playback["encode"].side_effect = ValueError("bad mode")
    rig = MagicMock(spec=["set_ptt"])
    worker = TxWorker(rig=rig, ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    assert log["error"] and "bad mode" in log["error"][0]
    assert log["started"] == []
    rig.set_ptt.assert_not_called()


def test_ptt_failure_aborts_before_playback(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
) -> None:
    """A real backend that fails to key must NOT play audio — the user
    asked for rig control and we mustn't transmit on whatever frequency
    the rig happens to be sitting on."""
    rig = MagicMock(spec=["set_ptt"])
    rig.set_ptt.side_effect = RigConnectionError("daemon dead")
    worker = TxWorker(rig=rig, ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    patch_encode_and_playback["play"].assert_not_called()
    assert log["error"] and "daemon dead" in log["error"][0]
    assert log["started"] == []
    assert log["complete"] == []


def test_playback_failure_still_unkeys_rig(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
) -> None:
    """Even if play_blocking raises mid-transmission, the rig must come
    out of TX — never leave the radio stuck-keyed."""
    patch_encode_and_playback["play"].side_effect = RuntimeError("audio device gone")
    rig = MagicMock(spec=["set_ptt"])
    worker = TxWorker(rig=rig, ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    # Both PTT calls happened.
    assert [c.args for c in rig.set_ptt.call_args_list] == [(True,), (False,)]
    assert log["error"] and "audio device gone" in log["error"][0]
    # No completion signal, since playback didn't actually finish.
    assert log["complete"] == []


def test_unkey_failure_is_reported_but_doesnt_block_complete(
    qapp,
    gradient_image: Image.Image,
) -> None:
    """If the rig refuses to unkey, we report it as an error but the
    transmission still counts as complete (the audio went out fine)."""
    calls: list[bool] = []

    def fake_set_ptt(on: bool) -> None:
        calls.append(on)
        if not on:
            raise RigConnectionError("ptt-off rejected")

    rig = MagicMock(spec=["set_ptt"])
    rig.set_ptt.side_effect = fake_set_ptt
    worker = TxWorker(rig=rig, ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit(gradient_image, Mode.ROBOT_36)

    assert calls == [True, False]
    assert log["complete"] == [True]
    assert log["error"] and "ptt-off rejected" in log["error"][0]


# === two-tone generator ===


class TestMakeTwoTone:
    """Tests for the _make_two_tone PCM generator."""

    SR = 48_000

    def test_length_matches_duration(self) -> None:
        samples = _make_two_tone(self.SR, 5.0)
        assert len(samples) == self.SR * 5

    def test_dtype_is_int16(self) -> None:
        samples = _make_two_tone(self.SR, 1.0)
        assert samples.dtype == np.dtype("int16")

    def test_no_clipping(self) -> None:
        """Peak of the two-tone sum must never hit the int16 rails."""
        samples = _make_two_tone(self.SR, 5.0)
        assert int(np.abs(samples).max()) < 32767

    def test_peak_near_minus1_dbfs(self) -> None:
        """Peak amplitude should be close to 10^(-1/20) of full scale.

        The theoretical maximum of two equal-amplitude sines with the
        chosen scale factor is exactly 32767 * 10^(-1/20) ≈ 29204.
        Due to sampling, the observed peak may be slightly below that.
        We verify it's within a 15% tolerance band: the signal is
        non-trivial and not clipped.
        """
        samples = _make_two_tone(self.SR, 5.0)
        target = 32767 * (10 ** (-1.0 / 20.0))  # ≈ 29204
        peak = int(np.abs(samples).max())
        assert peak > target * 0.70, (
            f"Peak {peak} is too low — signal may be missing"
        )
        assert peak < 32767, "Signal is clipping"

    def test_short_duration(self) -> None:
        """Generator must work for sub-second durations."""
        samples = _make_two_tone(self.SR, 0.1)
        assert len(samples) == int(self.SR * 0.1)
        assert samples.dtype == np.dtype("int16")


# === transmit_test_tone slot ===


def test_transmit_test_tone_emits_started_then_complete(qapp) -> None:
    """transmit_test_tone follows the same signal contract as transmit."""
    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit_test_tone()

    assert log["started"] == [True]
    assert log["complete"] == [True]
    assert log["aborted"] == []
    assert log["error"] == []


def test_transmit_test_tone_keys_and_unkeys_rig(qapp) -> None:
    rig = MagicMock(spec=["set_ptt", "open", "close"])
    worker = TxWorker(rig=rig, ptt_delay_s=0)

    worker.transmit_test_tone()

    assert [c.args for c in rig.set_ptt.call_args_list] == [(True,), (False,)]


def test_transmit_test_tone_ptt_failure_does_not_play(qapp) -> None:
    rig = MagicMock(spec=["set_ptt"])
    rig.set_ptt.side_effect = RigConnectionError("no radio")
    worker = TxWorker(rig=rig, ptt_delay_s=0)
    log = _record_signals(worker)

    worker.transmit_test_tone()

    assert log["error"] and "no radio" in log["error"][0]
    assert log["started"] == []
    assert log["complete"] == []


# === CW station ID ===


def test_cw_id_appended_to_sstv_samples(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    fake_samples: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With CW ID enabled the played buffer is SSTV + gap + CW samples."""
    cw_fake = np.zeros(500, dtype=np.int16)
    monkeypatch.setattr(
        "open_sstv.ui.workers.make_cw", MagicMock(return_value=cw_fake)
    )

    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0, sample_rate=48_000)
    worker.set_cw_id(True, "W0AEZ", wpm=20, tone_hz=800)
    worker.transmit(gradient_image, Mode.ROBOT_36)

    play = patch_encode_and_playback["play"]
    play.assert_called_once()
    args, _ = play.call_args
    sent = args[0]
    gap_n = int(_CW_GAP_S * 48_000)
    assert len(sent) == len(fake_samples) + gap_n + len(cw_fake)


def test_cw_id_skipped_when_callsign_empty(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    fake_samples: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty callsign → only SSTV plays, make_cw is never called."""
    make_cw_mock = MagicMock(return_value=np.zeros(200, dtype=np.int16))
    monkeypatch.setattr("open_sstv.ui.workers.make_cw", make_cw_mock)

    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0, sample_rate=48_000)
    worker.set_cw_id(True, "", wpm=20, tone_hz=800)  # enabled but no callsign
    worker.transmit(gradient_image, Mode.ROBOT_36)

    make_cw_mock.assert_not_called()
    args, _ = patch_encode_and_playback["play"].call_args
    assert len(args[0]) == len(fake_samples)


def test_cw_id_disabled_skips_cw(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    fake_samples: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CW ID disabled → just SSTV, even if callsign is set."""
    make_cw_mock = MagicMock(return_value=np.zeros(200, dtype=np.int16))
    monkeypatch.setattr("open_sstv.ui.workers.make_cw", make_cw_mock)

    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0, sample_rate=48_000)
    worker.set_cw_id(False, "W0AEZ", wpm=20, tone_hz=800)
    worker.transmit(gradient_image, Mode.ROBOT_36)

    make_cw_mock.assert_not_called()
    args, _ = patch_encode_and_playback["play"].call_args
    assert len(args[0]) == len(fake_samples)


def test_test_tone_does_not_append_cw(
    qapp,
    patch_encode_and_playback: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transmit_test_tone never calls make_cw, even with CW ID enabled."""
    make_cw_mock = MagicMock(return_value=np.zeros(200, dtype=np.int16))
    monkeypatch.setattr("open_sstv.ui.workers.make_cw", make_cw_mock)

    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0)
    worker.set_cw_id(True, "W0AEZ", wpm=20, tone_hz=800)
    worker.transmit_test_tone()

    make_cw_mock.assert_not_called()


def test_cw_id_output_gain_applied_to_cw(
    qapp,
    gradient_image: Image.Image,
    patch_encode_and_playback: dict[str, MagicMock],
    fake_samples: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output gain is applied to the CW tail, just like SSTV samples."""
    # Return a non-zero CW buffer so we can detect gain scaling.
    cw_fake = np.full(100, 10000, dtype=np.int16)
    monkeypatch.setattr(
        "open_sstv.ui.workers.make_cw", MagicMock(return_value=cw_fake)
    )

    worker = TxWorker(rig=ManualRig(), ptt_delay_s=0, sample_rate=48_000)
    worker.set_output_gain(0.5)
    worker.set_cw_id(True, "W0AEZ", wpm=20, tone_hz=800)
    worker.transmit(gradient_image, Mode.ROBOT_36)

    args, _ = patch_encode_and_playback["play"].call_args
    sent = args[0]
    gap_n = int(_CW_GAP_S * 48_000)
    cw_portion = sent[len(fake_samples) + gap_n:]
    # All CW samples should be ~5000 (10000 * 0.5).
    assert int(np.abs(cw_portion).max()) <= 5001


# === Watchdog covers every mode (OP-01 regression guard) ===


def test_watchdog_covers_every_mode_with_headroom() -> None:
    """_MAX_TX_DURATION_S must cover every supported mode with margin.

    Each transmission carries the mode body + VIS leader (~0.7 s) + PTT
    delay (up to 2 s) + CW station ID (up to ~15 s for a long callsign at
    15 WPM).  A 30 s slop comfortably covers all of that.  Without this
    test, adding a new long mode (Pasokon P7 at 406 s) silently broke
    when ``_MAX_TX_DURATION_S = 300`` in v0.1.21–v0.1.26.  Raising the
    watchdog to 600 s in v0.1.27 cleared every currently-shipping mode;
    this assertion pins the invariant for future modes too.
    """
    from open_sstv.core.modes import MODE_TABLE
    from open_sstv.ui.workers import _MAX_TX_DURATION_S

    longest = max(spec.total_duration_s for spec in MODE_TABLE.values())
    assert _MAX_TX_DURATION_S >= longest + 30, (
        f"Watchdog {_MAX_TX_DURATION_S:.0f}s < longest mode "
        f"{longest:.0f}s + 30s slop — will abort long-mode TX mid-flight."
    )
