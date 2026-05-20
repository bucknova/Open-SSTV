# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression test for the v0.3.9 → v0.3.10 offline-worker GC race.

In v0.3.9 the offline encode/decode workers were created as local
variables inside slot functions and driven via
``QMetaObject.invokeMethod`` + ``Q_ARG``.  Two compounding bugs were
present:

1. **GC race**: PySide6 signal connections hold only a *weak*
   reference to the receiver QObject, so a local-variable worker
   was garbage-collected the moment the slot returned — before the
   queued ``encode()`` invocation could fire.

2. **Q_ARG marshalling**: PySide6 6.11 cannot marshal arbitrary
   Python objects (``PIL.Image``, ``Mode`` StrEnum) across a queued
   ``invokeMethod`` call — ``Q_ARG(object, …)`` fails meta-type
   lookup, ``Q_ARG("PyObject", …)`` produces ``PyObjectWrapper``
   which doesn't match the slot's declared ``PyObject`` param.

v0.3.10 switched to the ``__init__(args) → thread.started → run``
pattern (matching ``_RigConnectWorker``) which sidesteps both issues:
args live on the worker before thread.start, so no cross-thread
marshalling is needed, and the worker is held as an instance
attribute so it survives until ``thread.finished``.

This test exercises that exact launch shape under ``pytest-qt`` —
worker held as instance attribute, ``thread.started → worker.run``,
real cross-thread dispatch.  A regression that reintroduces either
the GC race or the Q_ARG marshalling pattern causes this test to
time out (the worker dies before its slot runs, or Q_ARG fails
silently and the slot is never invoked).
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QObject, QThread

from open_sstv.core.modes import Mode
from open_sstv.ui.offline_workers import OfflineDecodeWorker, OfflineEncodeWorker

pytestmark = pytest.mark.gui


class _OfflineEncodeHost(QObject):
    """Mirrors MainWindow's instance-attribute ownership of the worker.

    Holding the worker on ``self._worker`` (not as a local) is what
    prevents the GC race.  Constructor args + ``thread.started → run``
    is what prevents the Q_ARG marshalling failure.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: OfflineEncodeWorker | None = None

    def start(
        self, image: Image.Image, mode: Mode, fs: int, out_path: str
    ) -> OfflineEncodeWorker:
        thread = QThread(self)
        worker = OfflineEncodeWorker(image, mode, fs, out_path)
        self._thread = thread
        self._worker = worker
        worker.finished.connect(thread.quit)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.start()
        return worker

    def shutdown(self) -> None:
        """Block until the thread exits — required before the host is
        destroyed, otherwise ``~QThread()`` aborts the process."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
            self._worker = None


class _OfflineDecodeHost(QObject):
    """Same instance-attribute + constructor-args pattern, decode side."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: OfflineDecodeWorker | None = None

    def start(self, path: str) -> OfflineDecodeWorker:
        thread = QThread(self)
        worker = OfflineDecodeWorker(path)
        self._thread = thread
        self._worker = worker
        worker.finished.connect(thread.quit)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.start()
        return worker

    def shutdown(self) -> None:
        """Block until the thread exits — required before the host is
        destroyed, otherwise ``~QThread()`` aborts the process."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
            self._worker = None


def test_encode_completes_via_thread_started_dispatch(
    qtbot, tmp_path: Path
) -> None:
    """Regression for the v0.3.9 worker-GC + Q_ARG bug.

    If the worker is GC'd before thread.started fires, no signal ever
    arrives.  If Q_ARG marshalling is reintroduced, the slot is never
    called.  Either way ``waitSignal`` times out.  With v0.3.10's
    constructor-args + instance-attr pattern, the encode runs to
    completion and the WAV is written to disk.
    """
    host = _OfflineEncodeHost()
    img = Image.new("RGB", (320, 240), color=(64, 192, 128))
    out_path = tmp_path / "queued.wav"
    worker = host.start(img, Mode.ROBOT_36, 48_000, str(out_path))

    try:
        # 60 s timeout: Robot 36 encode is ~36 s on a modest CPU.
        with qtbot.waitSignal(
            worker.encode_complete, timeout=60_000
        ) as blocker:
            pass

        assert blocker.signal_triggered
        path, duration_s, mode = blocker.args
        assert path == str(out_path)
        assert mode == Mode.ROBOT_36
        assert 30 < duration_s < 40
        assert out_path.exists()
        # Confirm format: 16-bit PCM mono at 48 kHz.
        with wave.open(str(out_path)) as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 48_000
    finally:
        host.shutdown()


def test_decode_completes_via_thread_started_dispatch(
    qtbot, tmp_path: Path
) -> None:
    """Same regression check, decode side.

    Encodes a Robot 36 WAV synchronously (so we have real audio), then
    decodes it via the constructor-args + thread.started pattern.
    """
    # Synchronous encode for the input — not exercising the cross-thread
    # path yet, just preparing test data.
    from open_sstv.core.encoder import encode

    img = Image.new("RGB", (320, 240), color=(20, 220, 90))
    samples = encode(img, Mode.ROBOT_36, sample_rate=48_000)
    wav_path = tmp_path / "to_decode.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(samples.tobytes())

    host = _OfflineDecodeHost()
    worker = host.start(str(wav_path))

    try:
        with qtbot.waitSignal(
            worker.image_complete, timeout=60_000
        ) as blocker:
            pass

        assert blocker.signal_triggered
        decoded_img, mode, vis_code = blocker.args
        assert mode == Mode.ROBOT_36
        assert vis_code == 0x08
        assert decoded_img.size == (320, 240)
    finally:
        host.shutdown()
