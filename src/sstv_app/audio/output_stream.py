# SPDX-License-Identifier: GPL-3.0-or-later
"""TX audio playback.

For v1 we don't need streaming output — TX is "render the whole image to a
buffer, then play it" — so the implementation is a thin wrapper around
``sounddevice.play(...)`` plus ``sounddevice.wait()`` running on a worker
thread so the GUI thread never blocks. A ``stop()`` helper calls
``sounddevice.stop()`` to interrupt the blocking ``wait()`` for user-cancelled
transmissions.

Public API:
    play(samples, sample_rate, device_index, on_finished_callback) -> None
    stop() -> None

Phase 0 stub. Implemented in Phase 1 step 6 of the v1 plan.
"""
from __future__ import annotations
