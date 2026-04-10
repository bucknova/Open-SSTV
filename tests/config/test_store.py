# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the TOML config store round-trip."""
from __future__ import annotations

from pathlib import Path

from sstv_app.config.schema import AppConfig
from sstv_app.config.store import load_config, save_config


def test_round_trip_defaults(tmp_path: Path) -> None:
    """Save default config, reload it, assert equality."""
    cfg = AppConfig()
    p = tmp_path / "config.toml"
    save_config(cfg, path=p)
    loaded = load_config(path=p)
    assert loaded == cfg


def test_round_trip_custom_values(tmp_path: Path) -> None:
    cfg = AppConfig(
        audio_input_device="hw:1",
        audio_output_device="hw:0",
        sample_rate=44_100,
        default_tx_mode="robot_36",
        rigctld_host="10.0.0.5",
        rigctld_port=4533,
        rig_enabled=True,
        ptt_delay_s=0.5,
        callsign="W0AEZ",
        last_image_dir="/tmp/sstv",
        images_save_dir="/tmp/sstv/saved",
        auto_save=True,
    )
    p = tmp_path / "config.toml"
    save_config(cfg, path=p)
    loaded = load_config(path=p)
    assert loaded == cfg


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    loaded = load_config(path=tmp_path / "nonexistent.toml")
    assert loaded == AppConfig()


def test_load_ignores_unknown_keys(tmp_path: Path) -> None:
    """A TOML file with extra keys from a newer version must not crash."""
    p = tmp_path / "config.toml"
    p.write_text('callsign = "AB1CD"\nfuture_key = 42\n')
    loaded = load_config(path=p)
    assert loaded.callsign == "AB1CD"


def test_load_fills_missing_keys_with_defaults(tmp_path: Path) -> None:
    """A TOML file with only one key still populates the rest from defaults."""
    p = tmp_path / "config.toml"
    p.write_text('sample_rate = 44100\n')
    loaded = load_config(path=p)
    assert loaded.sample_rate == 44_100
    assert loaded.callsign == ""
    assert loaded.rig_enabled is False


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "config.toml"
    save_config(AppConfig(), path=deep)
    assert deep.is_file()
    loaded = load_config(path=deep)
    assert loaded == AppConfig()
