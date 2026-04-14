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

import logging
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

_log = logging.getLogger(__name__)


def _default_images_dir() -> str:
    """XDG-correct pictures directory, e.g. ``~/Pictures/open_sstv``."""
    base = Path(platformdirs.user_pictures_dir())
    return str(base / "open_sstv")


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

    # --- Radio ---
    # Connection mode: "manual", "rigctld", "serial"
    rig_connection_mode: str = "manual"
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    ptt_delay_s: float = 0.2
    rig_model_id: int = 0
    rig_serial_port: str = ""
    rig_baud_rate: int = 9600
    auto_launch_rigctld: bool = False

    # --- Direct serial rig control ---
    # Protocol: "PTT Only (DTR/RTS)", "Icom CI-V", "Kenwood / Elecraft", "Yaesu CAT"
    rig_serial_protocol: str = "PTT Only (DTR/RTS)"
    rig_civ_address: int = 0x94
    rig_ptt_line: str = "DTR"

    # --- Audio gain ---
    audio_input_gain: float = 1.0
    audio_output_gain: float = 1.0
    # v0.1.13: overdrive unlocks the TX output gain slider ceiling from 100%
    # to 200%. Off by default — the typical USB-audio rig only needs ~10-15%.
    tx_output_overdrive: bool = False
    # v0.1.13: relaxes VIS leader presence (0.40 → 0.25) and start-bit
    # minimum duration (20 ms → 15 ms) for weak/fading signal conditions.
    rx_weak_signal_mode: bool = False

    # --- CW station ID ---
    # v0.1.14: appended after every SSTV TX (not test tone). Uses the
    # callsign field below. Skipped with a warning if callsign is empty.
    cw_id_enabled: bool = True
    cw_id_wpm: int = 20     # valid range 15–30
    cw_id_tone_hz: int = 800  # valid range 400–1200

    # --- Identity ---
    callsign: str = ""

    # --- Directories ---
    images_save_dir: str = field(default_factory=_default_images_dir)
    auto_save: bool = False

    def __post_init__(self) -> None:
        # v0.1.12: slider ceiling reverted from 500% to 200%.
        # Clamp any stored value so users who raised it to ≤500% on v0.1.11
        # don't get unexpected clipping on next open.
        if self.audio_output_gain > 2.0:
            self.audio_output_gain = 2.0
        # v0.1.13: default slider ceiling is now 100%.  If an existing config
        # has a value above 100% but overdrive was never persisted (missing
        # field, old config file), auto-enable overdrive so the user's
        # calibrated gain is preserved rather than being silently clamped on
        # the next Settings open.
        if self.audio_output_gain > 1.0 and not self.tx_output_overdrive:
            self.tx_output_overdrive = True
            _log.info(
                "AppConfig: audio_output_gain %.0f%% > 100%% — "
                "overdrive auto-enabled (migrated from pre-v0.1.13 config).",
                self.audio_output_gain * 100,
            )
        # v0.1.14: clamp CW fields to their valid ranges so hand-edited
        # TOML files can't push WPM or tone outside what the UI allows.
        self.cw_id_wpm = max(15, min(30, self.cw_id_wpm))
        self.cw_id_tone_hz = max(400, min(1200, self.cw_id_tone_hz))


__all__ = ["AppConfig"]
