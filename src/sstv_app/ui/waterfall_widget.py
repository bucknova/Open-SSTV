# SPDX-License-Identifier: GPL-3.0-or-later
"""Live FFT waterfall widget.

A ``QGraphicsView`` containing a custom ``QGraphicsItem`` that scrolls a
256-tall RGB bitmap downward each time a new FFT magnitude column arrives
on the ``waterfall_chunk`` signal. The FFT itself is computed in
``RxWorker`` (off the GUI thread); this widget only renders.

Default vertical scale 0–4000 Hz, horizontal scroll roughly one column per
50 ms.

Phase 0 stub. Implemented in Phase 2 step 17 of the v1 plan.
"""
from __future__ import annotations
