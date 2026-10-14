# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the v0.6.14 audit TX-safety findings in ``TxWorker``.

Same harness as ``test_tx_worker.py``: ``encode`` / ``play_blocking`` are
patched out and fake rigs record every PTT call, so each test runs in
milliseconds on the test thread.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from open_sstv.audio.devices import AudioDevice
from open_sstv.core.modes import Mode
from open_sstv.radio.base import ManualRig
from open_sstv.radio.exceptions import RigCommandError, RigConnectionError
from open_sstv.ui import workers
from open_sstv.ui.workers import TxWorker, _RigHealthMonitor

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, MagicMock]]:
    play = MagicMock()
    monkeypatch.setattr(workers, "encode", MagicMock(return_value=np.zeros(100, np.int16)))
    monkeypatch.setattr(workers.output_stream, "play_blocking", play)
    monkeypatch.setattr(workers.output_stream, "stop", MagicMock())
    monkeypatch.setattr(workers, "_UNKEY_RETRY_DELAY_S", 0.0)
    yield {"play": play}


@pytest.fixture
def image() -> Image.Image:
    return Image.new("RGB", (64, 64))


class PttRecorder:
    """Rig whose set_ptt calls are recorded; key/unkey behaviour scriptable."""

    name = "recorder"

    def __init__(self, key_exc: Exception | None = None) -> None:
        self.calls: list[bool] = []
        self.key_exc = key_exc

    def set_ptt(self, on: bool) -> None:
        self.calls.append(on)
        if on and self.key_exc is not None:
            raise self.key_exc

    def get_ptt(self) -> bool:
        return bool(self.calls and self.calls[-1])

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass


def _signals(w: TxWorker) -> dict[str, list]:
    log: dict[str, list] = {k: [] for k in ("started", "complete", "aborted", "error", "finished")}
    w.transmission_started.connect(lambda: log["started"].append(1))
    w.transmission_complete.connect(lambda: log["complete"].append(1))
    w.transmission_aborted.connect(lambda: log["aborted"].append(1))
    w.error.connect(lambda m: log["error"].append(m))
    w.transmission_finished.connect(lambda: log["finished"].append(1))
    return log


# --- T-H1 ---


@pytest.mark.parametrize(
    "exc", [RigConnectionError("T 1 timed out"), OSError("termios"), RuntimeError("??")]
)
def test_key_failure_still_sends_ptt_off(qapp, image, exc: Exception) -> None:
    """A failed/timed-out key may have keyed the rig anyway: PTT-off must
    always follow, and a non-RigError must not escape transmit()."""
    rig = PttRecorder(key_exc=exc)
    w = TxWorker(rig=rig, ptt_delay_s=0)
    log = _signals(w)
    w.transmit(image, Mode.ROBOT_36)
    assert rig.calls == [True, False]
    assert log["error"] and "Could not key rig" in log["error"][0]
    assert log["started"] == []
    assert log["finished"] == [1]


# --- T-M3 ---


def test_emergency_unkey_targets_the_keyed_rig_after_swap(qapp) -> None:
    real = PttRecorder()
    w = TxWorker(rig=real, ptt_delay_s=0)
    w._keyed_rig = real  # as _run_tx records it on keying
    w.set_rig(ManualRig())  # involuntary disconnect mid-TX
    w.emergency_unkey()
    assert real.calls == [False]


def test_keyed_rig_cleared_when_tx_ends(qapp, image) -> None:
    w = TxWorker(rig=PttRecorder(), ptt_delay_s=0)
    w.transmit(image, Mode.ROBOT_36)
    assert w._keyed_rig is None


# --- T-M2 ---


def test_watchdog_unkeys_directly_before_stopping(qapp) -> None:
    real = PttRecorder()
    w = TxWorker(rig=real, ptt_delay_s=0)
    w._keyed_rig = real
    order: list[str] = []
    real_set = real.set_ptt
    real.set_ptt = lambda on: (order.append(f"ptt{int(on)}"), real_set(on))  # type: ignore[method-assign]
    orig_stop = w.request_stop
    w.request_stop = lambda **kw: (order.append("stop"), orig_stop(**kw))  # type: ignore[method-assign]
    w._watchdog_fire(30.0, 0)
    assert order == ["ptt0", "stop"]


def test_watchdog_does_not_cancel_queued_next_tx(qapp, image, _fast) -> None:
    """v0.6.14 sweep: the watchdog stops the overrunning TX only — a
    separately queued next transmission still runs."""
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    log = _signals(w)
    w.note_tx_requested()  # TX 1 dispatched and running
    assert w._begin_tx_slot()
    w.note_tx_requested()  # TX 2 queued behind it
    w._watchdog_fire(30.0, 0)
    assert w._stop_event.is_set()  # TX 1 is told to stop
    w._end_tx_slot()
    w.transmit(image, Mode.ROBOT_36)  # TX 2's queued slot
    assert True in rig.calls, "queued TX must still key after a watchdog stop"
    assert log["complete"] == [1]


def test_operator_stop_still_cancels_queued_tx(qapp, image, _fast) -> None:
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    w.note_tx_requested()
    assert w._begin_tx_slot()
    w.note_tx_requested()
    w.request_stop()
    w._end_tx_slot()
    w.transmit(image, Mode.ROBOT_36)
    assert True not in rig.calls


# --- T-L2 ---


def test_stop_before_queued_slot_runs_is_honoured(qapp, image, _fast) -> None:
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    log = _signals(w)
    w.note_tx_requested()  # GUI dispatched…
    w.request_stop()       # …then Stop before the queued slot ran
    w.transmit(image, Mode.ROBOT_36)
    assert rig.calls == []
    _fast["play"].assert_not_called()
    assert log["aborted"] == [1] and log["finished"] == [1]


def test_stop_does_not_cancel_a_later_request(qapp, image) -> None:
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    w.request_stop()        # stale stop, nothing queued
    w.note_tx_requested()
    w.transmit(image, Mode.ROBOT_36)
    assert rig.calls == [True, False]


def test_test_tone_honours_early_stop(qapp) -> None:
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    w.note_tx_requested()
    w.request_stop()
    w.transmit_test_tone()
    assert rig.calls == []


# --- T-M1 / UI-M7 terminal signal ---


@pytest.mark.parametrize("fail", ["none", "encode", "key", "play"])
def test_transmission_finished_fires_exactly_once(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    rig = PttRecorder(key_exc=RigConnectionError("x") if fail == "key" else None)
    if fail == "encode":
        monkeypatch.setattr(workers, "encode", MagicMock(side_effect=ValueError("bad")))
    if fail == "play":
        _fast["play"].side_effect = RuntimeError("gone")
    w = TxWorker(rig=rig, ptt_delay_s=0)
    log = _signals(w)
    idle_at_finish: list[bool] = []
    w.transmission_finished.connect(lambda: idle_at_finish.append(w.wait_for_idle(0)))
    w.transmit(image, Mode.ROBOT_36)
    assert log["finished"] == [1]
    assert idle_at_finish == [True]


# --- T-M4 ---


class _FlakyRig:
    name = "flaky"

    def __init__(self, script: list[object]) -> None:
        self.script = script

    def get_ptt(self) -> bool:
        item = self.script.pop(0) if self.script else True
        if isinstance(item, Exception):
            raise item
        return bool(item)


def _run_monitor(rig: object, monkeypatch: pytest.MonkeyPatch, seconds: float) -> _RigHealthMonitor:
    monkeypatch.setattr(workers, "_RIG_HEALTH_INTERVAL_S", 0.01)
    m = _RigHealthMonitor(rig)  # type: ignore[arg-type]
    m.start()
    time.sleep(seconds)
    m.stop()
    return m


def test_health_single_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _FlakyRig([RigConnectionError("timeout"), True, RigConnectionError("t"), True])
    assert _run_monitor(rig, monkeypatch, 0.2).failure is None


def test_health_rprt_rejection_is_not_link_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _FlakyRig([RigCommandError("RPRT -9", command="t", rprt=-9)] * 10)
    assert _run_monitor(rig, monkeypatch, 0.3).failure is None


def test_health_consecutive_failures_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _FlakyRig([OSError("unplugged")] * 10)
    m = _run_monitor(rig, monkeypatch, 0.3)
    assert isinstance(m.failure, OSError)


class _AlwaysRaisingRig:
    name = "always"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.pings = 0

    def get_ptt(self) -> bool:
        self.pings += 1
        raise self.exc


@pytest.mark.parametrize("code", [-5, -6, -8, -13, -14])
def test_health_link_down_rprt_trips_after_threshold(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """v0.6.14 sweep: rigctld that lost the radio answers RPRT -5/-6."""
    rig = _AlwaysRaisingRig(RigCommandError(f"RPRT {code}", command="t", rprt=code))
    m = _run_monitor(rig, monkeypatch, 0.3)
    assert isinstance(m.failure, RigCommandError)
    assert rig.pings == workers._RIG_HEALTH_FAIL_THRESHOLD


@pytest.mark.parametrize("code", [-1, -4, -9, -11, 0x50000016])
def test_health_alive_rejection_codes_not_strikes(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    rig = _AlwaysRaisingRig(RigCommandError("refused", command="t", rprt=code))
    assert _run_monitor(rig, monkeypatch, 0.2).failure is None


# --- T-M5 / T-L4 ---


def _dev(index: int, api: str = "Core Audio", name: str = "USB Audio CODEC") -> AudioDevice:
    return AudioDevice(index, name, api, 0, 2, 48000.0)


def test_output_device_re_resolved_by_name_after_device_loss(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale, fresh = _dev(3), _dev(7)
    monkeypatch.setattr(workers, "find_output_device_by_name", lambda n, host_api=None: fresh)
    monkeypatch.setattr(workers, "list_output_devices", lambda: [fresh])
    w = TxWorker(rig=PttRecorder(), output_device=stale, ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.mark_output_device_stale()
    w.transmit(image, Mode.ROBOT_36)
    assert _fast["play"].call_args.kwargs["device"] is fresh


def test_output_device_not_requeried_without_device_change(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6.14 review: no PortAudio/pactl query on every key-up."""
    dev = _dev(3)
    lookup = MagicMock(return_value=_dev(7))
    monkeypatch.setattr(workers, "find_output_device_by_name", lookup)
    w = TxWorker(rig=PttRecorder(), output_device=dev, ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.transmit(image, Mode.ROBOT_36)
    lookup.assert_not_called()
    assert _fast["play"].call_args.kwargs["device"] is dev


def test_failed_enumeration_keeps_current_device(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6.14 review: PortAudio enumeration failing (devices returns [])
    is not 'the device is gone' — transmit on the current device."""
    dev = _dev(3)
    monkeypatch.setattr(workers, "list_output_devices", lambda: [])
    monkeypatch.setattr(workers, "find_output_device_by_name", lambda n, host_api=None: None)
    monkeypatch.setattr(workers, "find_pipewire_sink_by_name", lambda n: None)
    rig = PttRecorder()
    w = TxWorker(rig=rig, output_device=dev, ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.mark_output_device_stale()
    w.transmit(image, Mode.ROBOT_36)
    assert _fast["play"].call_args.kwargs["device"] is dev
    assert rig.calls and rig.calls[0] is True


def test_missing_output_device_refuses_before_keying(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "find_output_device_by_name", lambda n, host_api=None: None)
    monkeypatch.setattr(workers, "find_pipewire_sink_by_name", lambda n: None)
    monkeypatch.setattr(workers, "list_output_devices", lambda: [_dev(5, name="Other")])
    rig = PttRecorder()
    w = TxWorker(rig=rig, output_device=_dev(3), ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.mark_output_device_stale()
    log = _signals(w)
    w.transmit(image, Mode.ROBOT_36)
    assert rig.calls == [], "must not key when the TX audio device is gone"
    _fast["play"].assert_not_called()
    assert log["error"] and "not available" in log["error"][0]


def test_reresolution_prefers_previous_host_api(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    from open_sstv.audio import devices

    mme, wasapi = _dev(1, "MME"), _dev(9, "Windows WASAPI")
    monkeypatch.setattr(devices, "list_output_devices", lambda: [mme, wasapi])
    assert devices.find_output_device_by_name("USB Audio CODEC") is mme
    assert devices.find_output_device_by_name("USB Audio CODEC", host_api="Windows WASAPI") is wasapi
    assert devices.find_output_device_by_name("USB Audio CODEC", host_api="ASIO") is mme
    assert devices.find_output_device_by_name("nope", host_api="MME") is None


def test_portaudio_generation_bump_forces_reresolve(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6.14 sweep: a PortAudio reset by any path (e.g. RX start after a
    replug while RX was stopped) re-resolves without the GUI stale flag."""
    from open_sstv.audio import devices

    stale, fresh = _dev(3), _dev(7)
    lookup = MagicMock(return_value=fresh)
    monkeypatch.setattr(workers, "find_output_device_by_name", lookup)
    monkeypatch.setattr(workers, "list_output_devices", lambda: [fresh])
    w = TxWorker(rig=PttRecorder(), output_device=stale, ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.transmit(image, Mode.ROBOT_36)
    lookup.assert_not_called()
    devices.bump_portaudio_generation()
    w.transmit(image, Mode.ROBOT_36)
    assert _fast["play"].call_args.kwargs["device"] is fresh
    # ...and only once per generation.
    w.transmit(image, Mode.ROBOT_36)
    assert lookup.call_count == 1


def test_pa_reset_bumps_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from open_sstv.audio import devices, input_stream

    monkeypatch.setattr(input_stream.sd, "_terminate", lambda: None, raising=False)
    monkeypatch.setattr(input_stream.sd, "_initialize", lambda: None, raising=False)
    before = devices.portaudio_generation()
    input_stream.InputStreamWorker._pa_reset(MagicMock())
    assert devices.portaudio_generation() == before + 1


def test_device_query_holds_tx_state_lock(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enumeration can't overlap _pa_reset's sd._terminate()."""
    held: list[bool] = []

    def _list() -> list[AudioDevice]:
        held.append(workers.output_stream._tx_state_lock.locked())
        return [_dev(7)]

    monkeypatch.setattr(workers, "list_output_devices", _list)
    monkeypatch.setattr(workers, "find_output_device_by_name", lambda n, host_api=None: _dev(7))
    w = TxWorker(rig=PttRecorder(), output_device=_dev(3), ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC")
    w.mark_output_device_stale()
    w.transmit(image, Mode.ROBOT_36)
    assert held == [True]
    assert not workers.output_stream._tx_state_lock.locked()


def test_configured_host_api_preferred_when_missing_at_launch(
    qapp, image, _fast, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.6.14 sweep: device missing at launch, configured WASAPI ->
    resolves to the WASAPI twin, not MME (enumerated first)."""
    from open_sstv.audio import devices

    mme, wasapi = _dev(1, "MME"), _dev(9, "Windows WASAPI")
    monkeypatch.setattr(devices, "list_output_devices", lambda: [mme, wasapi])
    monkeypatch.setattr(workers, "list_output_devices", lambda: [mme, wasapi])
    monkeypatch.setattr(workers, "find_output_device_by_name", devices.find_output_device_by_name)
    w = TxWorker(rig=PttRecorder(), output_device=None, ptt_delay_s=0)
    w.set_output_device_name("USB Audio CODEC", host_api="Windows WASAPI")
    w.transmit(image, Mode.ROBOT_36)
    assert _fast["play"].call_args.kwargs["device"] is wasapi


def test_no_name_keeps_given_device(qapp, image, _fast) -> None:
    dev = _dev(3)
    w = TxWorker(rig=ManualRig(), output_device=dev, ptt_delay_s=0)
    w.transmit(image, Mode.ROBOT_36)
    assert _fast["play"].call_args.kwargs["device"] is dev


def test_tci_reconnect_in_unkey_retry_resubscribes_rx_audio(qapp) -> None:
    """T-L8: a reconnect during the unkey retry starts a fresh TCI session
    without the RX ``audio_start`` — RX went silent until restart."""

    class _Conn:
        def __init__(self) -> None:
            self.subscribed = True
            self.sent: list[str] = []

        def is_rx_audio_subscribed(self) -> bool:
            return self.subscribed

        def mark_rx_audio_subscribed(self, v: bool) -> None:
            self.subscribed = v

        def send(self, cmd: str) -> None:
            self.sent.append(cmd)

    class _TciLike(PttRecorder):
        def __init__(self) -> None:
            super().__init__()
            self.connection = _Conn()
            self.fail_unkeys = 1

        def set_ptt(self, on: bool) -> None:
            self.calls.append(on)
            if not on and self.fail_unkeys:
                self.fail_unkeys -= 1
                raise RigConnectionError("socket dead")

        def close(self) -> None:
            self.connection.subscribed = False  # disconnect() resets it

    rig = _TciLike()
    w = TxWorker(rig=rig, ptt_delay_s=0)
    w._unkey_with_retry(rig)
    assert rig.calls == [False, False]
    # v0.6.14 sweep: the full RX setup TciInputStreamWorker.start sends,
    # not a bare audio_start.
    from open_sstv.radio.tci import RX_AUDIO_SETUP_COMMANDS

    assert rig.connection.sent == list(RX_AUDIO_SETUP_COMMANDS)
    assert RX_AUDIO_SETUP_COMMANDS[-1] == "audio_start:0;"
    assert rig.connection.subscribed


def test_emergency_unkey_from_another_thread_while_keyed(qapp, image, _fast) -> None:
    """Sanity: emergency_unkey during playback reaches the keyed rig."""
    rig = PttRecorder()
    w = TxWorker(rig=rig, ptt_delay_s=0)

    def _play(*_a, **_k):
        t = threading.Thread(target=w.emergency_unkey)
        t.start()
        t.join()

    _fast["play"].side_effect = _play
    w.transmit(image, Mode.ROBOT_36)
    assert rig.calls == [True, False, False]
