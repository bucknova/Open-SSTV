# SPDX-License-Identifier: GPL-3.0-or-later
"""Receive panel widget.

Composes the live waterfall (top), the in-progress decode preview that
fills row-by-row as ``Decoder.LineDecoded`` events arrive (middle), and the
``ImageGalleryWidget`` of recently decoded images (bottom). Listens to
``RxWorker`` signals exclusively — no direct DSP imports.

Phase 0 stub. Implemented in Phase 2 step 17 of the v1 plan.
"""
from __future__ import annotations
