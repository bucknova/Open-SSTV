# SPDX-License-Identifier: GPL-3.0-or-later
"""Exception hierarchy for the radio control layer.

    RigError                 — base class
      RigConnectionError     — socket/transport-level failure (rigctld dead, etc.)
      RigCommandError        — rigctld returned a non-zero RPRT code

The UI catches these and shows a non-modal status bar message. A flaky CAT
connection must never crash the app or interrupt RX.

Phase 0 stub. Implemented in Phase 1 step 8 of the v1 plan.
"""
from __future__ import annotations
