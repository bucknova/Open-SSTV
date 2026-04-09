# SPDX-License-Identifier: GPL-3.0-or-later
"""Abstract ``Rig`` Protocol and the no-op ``ManualRig`` fallback backend.

Defines the surface every rig backend must implement (open/close, get/set
freq, get/set mode, get/set PTT, get_strength, ping, name). ``ManualRig`` is
the zero-config default for users without Hamlib — every method is a no-op
and the user is expected to be on VOX or hand-keyed PTT.

Phase 0 stub. Implemented in Phase 1 step 8 of the v1 plan.
"""
from __future__ import annotations
