# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt 6 (PySide6) desktop UI for sstv-app.

Plain QtWidgets — no QML — composed into a single ``MainWindow`` with an RX
panel (waterfall + in-progress decode + decoded image gallery), a TX panel
(image preview + mode picker + transmit/stop), and a radio panel
(rigctld connection state, frequency, S-meter).

Threading model: long-running tasks (RX decoder loop, TX playback) live on
``QThread`` workers in ``workers.py`` and communicate with the GUI thread
via Qt signals only. The DSP ``core/`` package never imports anything from
``ui/`` — the dependency arrow is one-way.
"""
