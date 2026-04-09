# SPDX-License-Identifier: GPL-3.0-or-later
"""Load and save the user config from a TOML file in the platformdirs path.

Public API:
    load_config() -> AppConfig
    save_config(cfg: AppConfig) -> None
    config_path() -> pathlib.Path

Loads with stdlib ``tomllib`` and writes with ``tomli_w``. Missing keys fall
back to dataclass defaults; unknown keys are ignored (forwards-compatible).

Phase 0 stub. Implemented in Phase 3 step 18 of the v1 plan.
"""
from __future__ import annotations
