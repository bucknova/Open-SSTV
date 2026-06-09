# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the TOML config store round-trip."""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# v0.3.4 — operator-info fields (operator_name / grid_square / qth)
# ---------------------------------------------------------------------------


def test_operator_info_fields_default_to_empty_string() -> None:
    """Fresh AppConfig must have empty defaults for the new fields so
    pre-v0.3.4 configs roundtrip without acquiring values they didn't
    have before."""
    cfg = AppConfig()
    assert cfg.operator_name == ""
    assert cfg.grid_square == ""
    assert cfg.qth == ""


def test_operator_info_fields_round_trip(tmp_path: Path) -> None:
    """Set the three new fields, save, reload, assert equality."""
    cfg = AppConfig(
        callsign="W0AEZ",
        operator_name="Kevin",
        grid_square="EM29",
        qth="Kansas City, MO",
    )
    p = tmp_path / "config.toml"
    save_config(cfg, path=p)
    loaded = load_config(path=p)
    assert loaded.operator_name == "Kevin"
    assert loaded.grid_square == "EM29"
    assert loaded.qth == "Kansas City, MO"


def test_pre_v0_3_4_config_loads_without_operator_info(tmp_path: Path) -> None:
    """A TOML file written before v0.3.4 (no operator_name / grid_square /
    qth keys) must load with the new fields at their empty defaults
    rather than crashing or reporting them as missing."""
    p = tmp_path / "config.toml"
    p.write_text('callsign = "W0AEZ"\nfirst_launch_seen = true\n')
    loaded = load_config(path=p)
    assert loaded.callsign == "W0AEZ"
    assert loaded.operator_name == ""
    assert loaded.grid_square == ""
    assert loaded.qth == ""


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


def test_load_corrupt_toml_backs_up_file(tmp_path: Path) -> None:
    """M2 (v0.3 audit): the corrupt file is preserved as a ``.corrupt``
    sibling (it holds the user's callsign/devices and is usually one
    typo away from recoverable) and the event is queryable so the GUI
    can warn the user."""
    from open_sstv.config.store import last_corrupt_backup

    p = tmp_path / "config.toml"
    p.write_bytes(b"[[[ not valid toml")
    loaded = load_config(path=p)

    backup = p.with_suffix(p.suffix + ".corrupt")
    assert loaded == AppConfig()
    assert not p.exists()  # corpse moved aside, not left to re-trip
    assert backup.exists()
    assert backup.read_bytes() == b"[[[ not valid toml"
    assert last_corrupt_backup() == backup


def test_clean_load_clears_corrupt_marker(tmp_path: Path) -> None:
    """The corrupt-backup marker is a process-global; a *clean* load must
    reset it so a prior corrupt load can't leak into an unrelated later
    one.  (Regression: the marker leaking across calls made the GUI pop
    a stale "settings were corrupt" dialog — and hung the headless test
    suite on the modal — after any earlier corrupt load in the session.)
    """
    from open_sstv.config.store import last_corrupt_backup

    # First, trip the marker with a corrupt file.
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"[[[ not valid toml")
    load_config(path=bad)
    assert last_corrupt_backup() is not None

    # Then a clean load (here: a non-existent path → defaults) must
    # clear it.
    good = tmp_path / "good.toml"
    load_config(path=good)
    assert last_corrupt_backup() is None


def test_load_wrong_typed_value_backs_up_and_defaults(tmp_path: Path) -> None:
    """M1/M2: a value that parses as TOML but blows up AppConfig
    construction (string where int is expected) is treated like
    corruption — defaults + backup, never a startup crash."""
    p = tmp_path / "config.toml"
    p.write_text('rigctld_port = "not-a-port"\n', encoding="utf-8")
    loaded = load_config(path=p)
    assert loaded == AppConfig()
    assert p.with_suffix(p.suffix + ".corrupt").exists()


def test_schema_validates_hand_edited_fields() -> None:
    """M1 (v0.3 audit): out-of-range / unknown hand-edited values are
    clamped or reset in ``__post_init__`` instead of failing later at
    connect/render time with no pointer back to the config."""
    cfg = AppConfig(
        rigctld_port=0,
        tci_port=99_999,
        rig_baud_rate=12_345,
        audio_input_gain=1000.0,
        rig_connection_mode="tcp",
        default_tx_mode="not_a_mode",
        rig_serial_protocol="Morse by hand",
        rig_ptt_line="XYZ",
        tx_banner_size="enormous",
    )
    assert cfg.rigctld_port == 1
    assert cfg.tci_port == 65535
    assert cfg.rig_baud_rate == 9600
    assert cfg.audio_input_gain == 2.0
    assert cfg.rig_connection_mode == "manual"
    assert cfg.default_tx_mode == "martin_m1"
    assert cfg.rig_serial_protocol == "PTT Only (DTR/RTS)"
    assert cfg.rig_ptt_line == "DTR"
    assert cfg.tx_banner_size == "small"


def test_schema_accepts_valid_values_unchanged() -> None:
    """Validation must not touch in-range values."""
    cfg = AppConfig(
        rigctld_port=4533,
        rig_baud_rate=38_400,
        audio_input_gain=1.5,
        rig_connection_mode="serial",
        default_tx_mode="scottie_s1",
        rig_ptt_line="rts",  # case-normalised, not rejected
        tx_banner_size="large",
    )
    assert cfg.rigctld_port == 4533
    assert cfg.rig_baud_rate == 38_400
    assert cfg.audio_input_gain == 1.5
    assert cfg.rig_connection_mode == "serial"
    assert cfg.default_tx_mode == "scottie_s1"
    assert cfg.rig_ptt_line == "RTS"
    assert cfg.tx_banner_size == "large"


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
    import open_sstv.config.store as store_module

    def _fail_replace(src: str, dst: str) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(store_module.os, "replace", _fail_replace)
    p = tmp_path / "config.toml"
    with pytest.raises(OSError, match="simulated disk full"):
        save_config(AppConfig(), path=p)

    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), ".tmp must be cleaned up after a failed os.replace"


# === M6: concurrent save_config is serialized via threading.Lock ===


def test_save_config_concurrent_calls_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads calling save_config in parallel must not interleave
    inside the critical section.

    Without the lock, two threads could both write to ``config.toml.tmp``
    and ``os.replace`` it, leaving one writer's bytes inside the other's
    half-finished file or producing a stray ``.tmp`` sibling.  With the
    lock, the two calls serialize: at most one thread is inside the
    write block at any instant, and the on-disk file matches one of the
    two configs exactly (not a hybrid).
    """
    import threading
    import time

    import open_sstv.config.store as store_module

    p = tmp_path / "config.toml"
    cfg_a = AppConfig(callsign="W0AAA")
    cfg_b = AppConfig(callsign="K1BBB")

    # Track inside-critical-section concurrency by wrapping tomli_w.dump
    # with a "I'm in" / "I'm out" pair guarded by a tiny sleep so the
    # window is wide enough that two unsynchronised threads would
    # observably overlap.
    inside_count = 0
    max_concurrent = 0
    overlap_lock = threading.Lock()

    real_dump = store_module.tomli_w.dump

    def _instrumented_dump(data, f):
        nonlocal inside_count, max_concurrent
        with overlap_lock:
            inside_count += 1
            max_concurrent = max(max_concurrent, inside_count)
        try:
            time.sleep(0.05)  # widen the race window
            real_dump(data, f)
        finally:
            with overlap_lock:
                inside_count -= 1

    monkeypatch.setattr(store_module.tomli_w, "dump", _instrumented_dump)

    threads = [
        threading.Thread(target=save_config, args=(cfg_a,), kwargs={"path": p}),
        threading.Thread(target=save_config, args=(cfg_b,), kwargs={"path": p}),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_concurrent == 1, (
        f"save_config critical sections overlapped (peak concurrency = "
        f"{max_concurrent}); the threading.Lock is not protecting the writer."
    )

    # Whichever thread won the race, the resulting file must be a clean
    # one of the two configs — never a torn hybrid.
    loaded = load_config(path=p)
    assert loaded.callsign in {"W0AAA", "K1BBB"}

    # And no orphan .tmp sibling left behind.
    assert not p.with_suffix(p.suffix + ".tmp").exists()
