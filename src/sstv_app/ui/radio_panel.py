# SPDX-License-Identifier: GPL-3.0-or-later
"""Radio (rigctld) connection panel widget.

Host:port editor, Connect/Disconnect button, current frequency label,
current mode label, S-meter bar. Polls the rig at 1 Hz on a ``QTimer``;
all polling goes through ``RigctldClient`` which serializes its socket
access with a ``threading.Lock``.

Phase 0 stub. Implemented across Phase 1 step 9 and Phase 3 step 20 of the
v1 plan.
"""
from __future__ import annotations
