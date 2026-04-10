# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings schema (plain dataclasses, no Pydantic).

``AppConfig`` holds every user-facing setting. The TOML store loads it
from disk (filling missing keys with the dataclass defaults) and writes
it back when the user clicks "Save" in the settings dialog.

The config is intentionally a *value object* with no Qt dependency — it
can be constructed and round-tripped in headless tests without importing
PySide6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import platformdirs


def _default_images_dir() -> str:
    """XDG-correct pictures directory, e.g. ``~/Pictures/sstv_app``."""
    base = Path(platformdirs.user_pictures_dir())
    return str(base / "sstv_app")


@dataclass
class AppConfig:
    """Top-level application config, one field per user-facing setting.

    Every field has a sensible default so ``AppConfig()`` is valid on a
    fresh install with no TOML file on disk.
    """

    # --- Audio ---
    audio_input_device: str | None = None
    audio_output_device: str | None = None
    sample_rate: int = 48_000

    # --- TX ---
    default_tx_mode: str = "martin_m1"

    # --- Radio (rigctld) ---
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    rig_enabled: bool = False
    ptt_delay_s: float = 0.2

    # --- Identity ---
    callsign: str = ""

    # --- Directories ---
    last_image_dir: str = field(default_factory=_default_images_dir)
    images_save_dir: str = field(default_factory=_default_images_dir)
    auto_save: bool = False


__all__ = ["AppConfig"]
