# SPDX-License-Identifier: GPL-3.0-or-later
"""Issue #90: serial-line settings passed to an auto-launched rigctld.

The FT-450D's Hamlib backend defaults to RTS/CTS hardware handshake; with
only ``-r``/``-s`` on the command line there was no way to turn it off.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from open_sstv.config.schema import AppConfig
from open_sstv.config.store import load_config, save_config
from open_sstv.radio import rigctld_launcher as rl


def test_defaults_pass_nothing() -> None:
    """Default config must leave the command line exactly as before #90."""
    assert rl.serial_conf_from(AppConfig()) == {}
    assert rl.build_rigctld_argv(
        "/x/rigctld", 1046, 4532, "/dev/ttyUSB0", 38400, rl.serial_conf_from(AppConfig())
    ) == ["/x/rigctld", "-m", "1046", "-t", "4532", "-r", "/dev/ttyUSB0", "-s", "38400"]


def test_settings_become_one_set_conf_flag() -> None:
    cfg = AppConfig(
        rig_serial_handshake="None",
        rig_data_bits=8,
        rig_stop_bits=2,
        rig_serial_parity="None",
        rig_rts_state="OFF",
        rig_dtr_state="ON",
    )
    argv = rl.build_rigctld_argv(
        "/x/rigctld", 1046, 4532, "/dev/ttyUSB0", 38400, rl.serial_conf_from(cfg)
    )
    assert argv[-2:] == [
        "-C",
        "serial_handshake=None,data_bits=8,stop_bits=2,serial_parity=None,"
        "rts_state=OFF,dtr_state=ON",
    ]


def test_only_changed_settings_are_passed() -> None:
    conf = rl.serial_conf_from(AppConfig(rig_serial_handshake="None"))
    assert conf == {"serial_handshake": "None"}


def test_rts_dropped_under_hardware_handshake() -> None:
    """Hamlib refuses rts_state when it owns RTS for RTS/CTS flow control."""
    conf = rl.serial_conf_from(
        AppConfig(rig_serial_handshake="Hardware", rig_rts_state="ON", rig_dtr_state="OFF")
    )
    assert conf == {"serial_handshake": "Hardware", "dtr_state": "OFF"}


def test_unknown_values_are_ignored_by_serial_conf_from() -> None:
    source = SimpleNamespace(rig_serial_handshake="-v", rig_data_bits=9)
    assert rl.serial_conf_from(source) == {}


@pytest.mark.parametrize(
    "conf",
    [
        {"serial_handshake": "None,-vvvvv"},
        {"serial_handshake": "--help"},
        {"rig_pathname": "/dev/ttyS0"},
        {"data_bits": "9"},
    ],
)
def test_argv_rejects_unknown_conf(conf: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        rl.build_rigctld_argv("/x/rigctld", 1046, 4532, "/dev/ttyUSB0", 38400, conf)


def test_schema_resets_bad_values_to_default() -> None:
    cfg = AppConfig(
        rig_serial_handshake="RTSCTS",
        rig_data_bits=9,
        rig_stop_bits=3,
        rig_serial_parity="odd",
        rig_rts_state="high",
        rig_dtr_state="-C",
    )
    for attr, choices in rl.SERIAL_CONF_CHOICES.items():
        assert getattr(cfg, attr) == choices[0], attr


def test_round_trip_through_store(tmp_path: Path) -> None:
    cfg = AppConfig(rig_serial_handshake="None", rig_stop_bits=2, rig_rts_state="ON")
    p = tmp_path / "config.toml"
    save_config(cfg, path=p)
    assert load_config(path=p) == cfg


def test_launch_passes_conf_to_popen(monkeypatch) -> None:
    seen: list[list[str]] = []

    class _Proc:
        stderr = None

    def _popen(cmd, **_kw):
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr(rl, "find_rigctld_executable", lambda: "/x/rigctld")
    monkeypatch.setattr(rl.subprocess, "Popen", _popen)
    rl.launch_rigctld(1046, 4532, "/dev/ttyUSB0", 38400, {"serial_handshake": "None"})
    assert seen[0][-2:] == ["-C", "serial_handshake=None"]
