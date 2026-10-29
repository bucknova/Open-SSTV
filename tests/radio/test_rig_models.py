# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: E501  (verbatim rigctld -l output is wider than 100 columns)
"""Issue #90: the Radio model list — ``rigctld -l`` parsing and the fallback."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from open_sstv.radio import rig_models as rm

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script fake")

HAMLIB4_LIST = """\
 Rig #  Mfg                    Model                   Version         Status      Macro
     1  Hamlib                 Dummy                   20221128.0      Stable      RIG_MODEL_DUMMY
     2  Hamlib                 NET rigctl              20221111.0      Stable      RIG_MODEL_NETRIGCTL
  1027  Yaesu                  FT-450                  20221011.0      Stable      RIG_MODEL_FT450
  1046  Yaesu                  FT-450D                 20221024.0      Stable      RIG_MODEL_FT450D
  2029  Elecraft               K3                      20221226.10     Stable      RIG_MODEL_K3
  3073  Icom                   IC-7300                 20221218.0      Stable      RIG_MODEL_IC7300
"""

HAMLIB3_LIST = """\
 Rig #  Mfg                    Model                   Version         Status
     1  Hamlib                 Dummy                   0.5             Beta
     2  Hamlib                 NET rigctl              0.3             Beta
   127  Yaesu                  FT-450                  0.22.1          Beta
   373  Icom                   IC-7300                 0.7.1           Beta
"""

# Hamlib model IDs from include/hamlib/riglist.h (RIG_MAKE_MODEL(backend, n)
# = backend * 1000 + n; Dummy 0, Yaesu 1, Kenwood 2, Icom 3).
RIGLIST_H = {
    "Hamlib Dummy": 1,  # RIG_MODEL_DUMMY
    "Hamlib NET rigctl": 2,  # RIG_MODEL_NETRIGCTL
    "Yaesu FT-817": 1020,  # RIG_MODEL_FT817
    "Yaesu FT-857": 1022,  # RIG_MODEL_FT857
    "Yaesu FT-897": 1023,  # RIG_MODEL_FT897
    "Yaesu FT-450": 1027,  # RIG_MODEL_FT450
    "Yaesu FT-950": 1028,  # RIG_MODEL_FT950
    "Yaesu FT-991": 1035,  # RIG_MODEL_FT991
    "Yaesu FT-891": 1036,  # RIG_MODEL_FT891
    "Yaesu FTDX-101D": 1040,  # RIG_MODEL_FTDX101D
    "Yaesu FT-818": 1041,  # RIG_MODEL_FT818
    "Yaesu FTDX-10": 1042,  # RIG_MODEL_FTDX10
    "Yaesu FTDX-101MP": 1044,  # RIG_MODEL_FTDX101MP
    "Yaesu FT-450D": 1046,  # RIG_MODEL_FT450D
    "Yaesu FT-710": 1049,  # RIG_MODEL_FT710
    "Kenwood TS-2000": 2014,  # RIG_MODEL_TS2000
    "Kenwood TS-480": 2028,  # RIG_MODEL_TS480
    "Kenwood TS-590SG": 2037,  # RIG_MODEL_TS590SG
    "Kenwood TS-890S": 2041,  # RIG_MODEL_TS890S
    "Elecraft K3": 2029,  # RIG_MODEL_K3
    "Elecraft KX2": 2044,  # RIG_MODEL_KX2
    "Elecraft KX3": 2045,  # RIG_MODEL_KX3
    "Elecraft K4": 2047,  # RIG_MODEL_K4
    "Icom IC-7200": 3061,  # RIG_MODEL_IC7200
    "Icom IC-7100": 3070,  # RIG_MODEL_IC7100
    "Icom IC-7300": 3073,  # RIG_MODEL_IC7300
    "Icom IC-7610": 3078,  # RIG_MODEL_IC7610
    "Icom IC-9700": 3081,  # RIG_MODEL_IC9700
    "Icom IC-705": 3085,  # RIG_MODEL_IC705
}

# Which Hamlib backend (ID // 1000) each manufacturer's rigs live in.
BACKEND = {"Hamlib": 0, "Yaesu": 1, "Kenwood": 2, "Elecraft": 2, "Icom": 3}


@pytest.fixture(autouse=True)
def _clear_cache():
    rm.available_rig_models.cache_clear()
    yield
    rm.available_rig_models.cache_clear()


def _fake_rigctld(directory: Path, output: str) -> None:
    (directory / "list.txt").write_text(output)
    exe = directory / "rigctld"
    exe.write_text(f'#!/bin/sh\n/bin/cat "{directory / "list.txt"}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


# --- fallback list ---------------------------------------------------------


def test_fallback_ids_match_riglist_h() -> None:
    got = {f"{r.manufacturer} {r.model}": r.model_id for r in rm.FALLBACK_RIG_MODELS}
    assert got == RIGLIST_H


def test_fallback_ids_are_in_the_right_backend() -> None:
    """The original list had Yaesu, Kenwood and Icom backends shuffled
    ("IC-7300" = 1035 = the FT-991).  Catch that class of mistake directly."""
    for r in rm.FALLBACK_RIG_MODELS:
        assert r.model_id // 1000 == BACKEND[r.manufacturer], r


def test_fallback_ids_are_unique() -> None:
    ids = [r.model_id for r in rm.FALLBACK_RIG_MODELS]
    assert len(ids) == len(set(ids))


def test_fallback_has_the_issue_90_radio() -> None:
    assert any(r.model_id == 1046 and r.model == "FT-450D" for r in rm.FALLBACK_RIG_MODELS)


# --- parsing ---------------------------------------------------------------


def test_parse_hamlib4_list() -> None:
    models = rm.parse_rigctld_list(HAMLIB4_LIST)
    assert models == [
        rm.RigModel(1, "Hamlib", "Dummy"),
        rm.RigModel(2, "Hamlib", "NET rigctl"),
        rm.RigModel(1027, "Yaesu", "FT-450"),
        rm.RigModel(1046, "Yaesu", "FT-450D"),
        rm.RigModel(2029, "Elecraft", "K3"),
        rm.RigModel(3073, "Icom", "IC-7300"),
    ]


def test_parse_hamlib3_list_without_macro_column() -> None:
    models = rm.parse_rigctld_list(HAMLIB3_LIST)
    assert rm.RigModel(127, "Yaesu", "FT-450") in models
    assert rm.RigModel(2, "Hamlib", "NET rigctl") in models
    assert len(models) == 4


def test_parse_skips_noise() -> None:
    assert rm.parse_rigctld_list("rigctld: unknown option\n\n  garbage\n") == []


def test_label() -> None:
    assert rm.RigModel(1046, "Yaesu", "FT-450D").label == "Yaesu FT-450D (1046)"
    assert rm.NO_RIG.label == "None / Manual (0)"


# --- available_rig_models --------------------------------------------------


def test_no_rigctld_uses_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    models, from_hamlib = rm.available_rig_models()
    assert not from_hamlib
    assert models[0] == rm.NO_RIG
    assert set(models[1:]) == set(rm.FALLBACK_RIG_MODELS)


@posix_only
def test_installed_rigctld_list_is_used(tmp_path: Path, monkeypatch) -> None:
    # Pad with enough rows to pass the sanity threshold.
    extra = "".join(
        f"  {9000 + i}  Test                   Rig {i:<20}0.1             Alpha\n"
        for i in range(10)
    )
    _fake_rigctld(tmp_path, HAMLIB4_LIST + extra)
    monkeypatch.setenv("PATH", str(tmp_path))
    models, from_hamlib = rm.available_rig_models()
    assert from_hamlib
    assert models[0] == rm.NO_RIG
    assert rm.RigModel(1046, "Yaesu", "FT-450D") in models
    # Sorted by manufacturer for the combo.
    mfgs = [r.manufacturer.lower() for r in models[1:]]
    assert mfgs == sorted(mfgs)


@posix_only
def test_unparseable_output_falls_back(tmp_path: Path, monkeypatch) -> None:
    _fake_rigctld(tmp_path, "something unexpected\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    _, from_hamlib = rm.available_rig_models()
    assert not from_hamlib
