# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Composes the RX panel, TX panel, radio panel, settings dialog, and the
toolbar / status bar. Owns the worker ``QThread`` instances and wires their
signals into the relevant widgets.

Layout (see docs/architecture.md for the ASCII sketch):

    [ Menu: File / Radio / Help ]
    [ Toolbar: input device | output device | rig LED | callsign ]
    +---------------------------+----------------------------+
    | RX panel                  | TX panel                   |
    |   waterfall + in-progress |   image preview + transmit |
    |   decoded image gallery   |                            |
    +---------------------------+----------------------------+
    [ Status bar: rig freq | mode | S-meter | RX SNR ]

Phase 0 stub. Implemented across Phase 1 step 9 (TX-only) and Phase 2 step 17
(RX wired in) of the v1 plan.
"""
from __future__ import annotations
