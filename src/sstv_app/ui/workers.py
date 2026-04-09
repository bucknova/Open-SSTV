# SPDX-License-Identifier: GPL-3.0-or-later
"""``QThread`` workers for long-running RX and TX tasks.

The DSP loop and the audio playback both block, so they live on dedicated
``QThread`` instances and communicate with the GUI thread exclusively via
Qt signals (queued connections, which Qt makes thread-safe automatically).
We deliberately avoid asyncio/qasync — no concurrent socket fan-out, so a
worker-thread-per-task model is the right fit.

Workers (Phase 0 stub):

    class RxWorker(QObject):
        image_started   = Signal(str)             # mode name
        image_complete  = Signal(QImage, str)     # decoded image, mode name
        waterfall_chunk = Signal(np.ndarray)      # FFT magnitude column
        snr_updated     = Signal(float)
        error           = Signal(str)

    class TxWorker(QObject):
        transmission_started   = Signal()
        transmission_progress  = Signal(float)    # 0.0 .. 1.0
        transmission_complete  = Signal()
        transmission_aborted   = Signal()
        error                  = Signal(str)

Implemented in Phase 1 step 9 (TxWorker) and Phase 2 step 17 (RxWorker) of
the v1 plan.
"""
from __future__ import annotations
