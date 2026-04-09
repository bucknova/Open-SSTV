# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio I/O layer for sstv-app.

Wraps ``sounddevice`` (PortAudio) for cross-platform input and output. The
PortAudio callback runs on its own thread, so this package is responsible for
bridging that thread safely to the rest of the app via ``queue.Queue`` plus
Qt signals — the DSP ``core/`` package is forbidden from talking to audio I/O
directly so it stays headless-testable.
"""
