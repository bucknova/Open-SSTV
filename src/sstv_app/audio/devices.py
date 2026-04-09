# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio device enumeration.

Wraps ``sounddevice.query_devices()`` and returns tidy ``AudioDevice``
dataclasses (name, index, supported sample rates, channel count, host API)
that the UI can populate combo boxes with.

Public API:
    list_input_devices()  -> list[AudioDevice]
    list_output_devices() -> list[AudioDevice]

Phase 0 stub. Implemented in Phase 1 step 6 of the v1 plan.
"""
from __future__ import annotations
