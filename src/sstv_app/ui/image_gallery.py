# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoded image gallery widget.

A ``QListView`` in ``IconMode`` backed by a tiny ``QStandardItemModel``
showing the last N decoded SSTV frames as thumbnails. Double-clicking opens
a save dialog (or auto-saves to ``config.images_save_dir`` if configured).

Phase 0 stub. Implemented in Phase 2 step 17 of the v1 plan.
"""
from __future__ import annotations
