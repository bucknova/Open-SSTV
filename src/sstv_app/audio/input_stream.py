# SPDX-License-Identifier: GPL-3.0-or-later
"""Thread-safe RX audio capture pipeline.

Wraps ``sounddevice.InputStream`` and bridges its PortAudio callback thread
to the rest of the app via a bounded ``queue.Queue`` plus a ``QObject`` that
emits Qt signals on its own ``QThread``. The PortAudio callback itself does
**only** ``q.put_nowait(indata.copy())`` — no DSP, no Qt signals — which is
the documented safe pattern for python-sounddevice.

Public API:
    class InputStreamWorker(QObject):
        chunk_ready = Signal(np.ndarray)   # float32 mono
        error = Signal(str)
        def start(self, device_index, sample_rate=48000, blocksize=1024): ...
        def stop(self): ...

Phase 0 stub. Implemented in Phase 2 step 16 of the v1 plan.
"""
from __future__ import annotations
