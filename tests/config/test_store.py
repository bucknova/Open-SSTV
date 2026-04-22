# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the TOML config store round-trip."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.config.store import load_config, save_config


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
        ptt_delay_s=0.5,
        callsign="W0AEZ",
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


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "config.toml"
    save_config(AppConfig(), path=deep)
    assert deep.is_file()
    loaded = load_config(path=deep)
    assert loaded == AppConfig()


# === v0.2.8: auto-save filename template ===


def test_round_trip_autosave_filename_fields(tmp_path: Path) -> None:
    """The three v0.2.8 auto-save fields must survive a TOML round-trip.

    Catches the specific regression mode where a new AppConfig field is
    added but not threaded through ``save_config`` / ``load_config``,
    causing the user's saved template to silently revert to the
    default on every relaunch.
    """
    cfg = AppConfig(
        auto_save=True,
        autosave_tx=True,
        autosave_filename_pattern="%c_%d_%t_%m",
        autosave_file_format="jpg",
    )
    p = tmp_path / "config.toml"
    save_config(cfg, path=p)
    loaded = load_config(path=p)
    assert loaded.autosave_tx is True
    assert loaded.autosave_filename_pattern == "%c_%d_%t_%m"
    assert loaded.autosave_file_format == "jpg"
    assert loaded == cfg


def test_load_autosave_defaults_when_missing(tmp_path: Path) -> None:
    """Older configs (pre-v0.2.8) lack these keys. Loading must apply
    sensible defaults rather than crash."""
    p = tmp_path / "config.toml"
    p.write_text('callsign = "W0AEZ"\n')
    loaded = load_config(path=p)
    assert loaded.autosave_tx is False
    assert loaded.autosave_filename_pattern == "%d_%t_%m"
    assert loaded.autosave_file_format == "png"


def test_autosave_file_format_normalised_on_load(tmp_path: Path) -> None:
    """Hand-edited TOML with 'JPEG' or '.PNG' must still produce a
    valid filename extension — ``AppConfig.__post_init__`` normalises
    it to lowercase and maps ``jpeg`` → ``jpg``."""
    p = tmp_path / "config.toml"
    p.write_text('autosave_file_format = "JPEG"\n')
    loaded = load_config(path=p)
    assert loaded.autosave_file_format == "jpg"

    p.write_text('autosave_file_format = "bmp"\n')
    loaded = load_config(path=p)
    # Unknown format falls back to PNG — never leaves the user with a
    # config that produces unopenable files.
    assert loaded.autosave_file_format == "png"


# === OP2-06: narrow except in load_config ===


def test_load_corrupt_toml_returns_defaults(tmp_path: Path) -> None:
    """Genuine TOML parse error → fall back to defaults (not a crash)."""
    p = tmp_path / "config.toml"
    p.write_bytes(b"[[[ not valid toml")
    loaded = load_config(path=p)
    assert loaded == AppConfig()


@pytest.mark.skipif(sys.platform == "win32", reason="chmod read-only unreliable on Windows")
def test_load_permission_error_propagates(tmp_path: Path) -> None:
    """PermissionError must NOT be swallowed — it surfaces so the operator
    knows their config directory has a permission problem (OP2-06)."""
    p = tmp_path / "config.toml"
    save_config(AppConfig(), path=p)
    p.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            load_config(path=p)
    finally:
        p.chmod(0o644)


# === OP2-07: atomic config write ===


def test_save_config_is_atomic(tmp_path: Path) -> None:
    """save_config must leave no .tmp artefact on success (OP2-07)."""
    p = tmp_path / "config.toml"
    save_config(AppConfig(), path=p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), ".tmp file must be removed after successful save"


def test_save_config_no_tmp_on_ioerror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If os.replace fails, the .tmp file must be cleaned up (OP2-07)."""
    import os
    import open_sstv.config.store as store_module

    original_replace = os.replace

    def _fail_replace(src: str, dst: str) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(store_module.os, "replace", _fail_replace)
    p = tmp_path / "config.toml"
    with pytest.raises(OSError, match="simulated disk full"):
        save_config(AppConfig(), path=p)

    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), ".tmp must be cleaned up after a failed os.replace"
