# SPDX-License-Identifier: GPL-3.0-or-later
"""TCP client for Hamlib's ``rigctld`` daemon.

The wire protocol is one command per line, ``\\n``-terminated. Set commands
respond with ``RPRT 0\\n`` on success or ``RPRT -n\\n`` on error. Get commands
respond with one value per line. We implement a small subset for v1:

    F <hz>           set freq         RPRT 0
    f                get freq         <hz>
    M <mode> <pb>    set mode         RPRT 0
    m                get mode         <mode>\\n<pb>
    T 1 / T 0        set PTT          RPRT 0
    t                get PTT          0/1
    l STRENGTH       get S-meter      <int dB>
    \\dump_state     handshake        multiline

Connection lifecycle: lazy connect on first command, idempotent ``open()``,
``threading.Lock`` around ``_send_recv`` so the UI poll thread and TX worker
can't interleave bytes, one automatic reconnect on broken pipe before raising
``RigConnectionError``, 2-second per-command timeout.

Phase 0 stub. Implemented in Phase 1 step 8 of the v1 plan.
"""
from __future__ import annotations
