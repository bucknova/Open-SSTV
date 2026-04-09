# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings schema (plain dataclasses, no Pydantic).

Fields:
    audio_input_device   : str | None
    audio_output_device  : str | None
    sample_rate          : int            (default 48000)
    default_tx_mode      : str            (default "martin_m1")
    rigctld_host         : str            (default "127.0.0.1")
    rigctld_port         : int            (default 4532)
    rig_enabled          : bool           (default False)
    ptt_delay_s          : float          (default 0.2)
    callsign             : str            (default "")
    last_image_dir       : str            (default user pictures dir)
    images_save_dir      : str            (default user pictures dir)

Phase 0 stub. Implemented in Phase 3 step 18 of the v1 plan.
"""
from __future__ import annotations
