# SPDX-License-Identifier: GPL-3.0-or-later
"""Modal settings dialog.

Edits an ``AppConfig`` instance from ``sstv_app.config.schema``. On accept,
calls ``sstv_app.config.store.save_config`` to persist the changes back to
the platformdirs config path. Lays out fields by section: Audio, Radio,
Images, About.

Phase 0 stub. Implemented in Phase 3 step 18 of the v1 plan.
"""
from __future__ import annotations
