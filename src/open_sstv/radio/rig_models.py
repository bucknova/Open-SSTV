# SPDX-License-Identifier: GPL-3.0-or-later
"""Hamlib radio model list for the Settings "Radio model" combo.

Issue #90: the combo used to be a hand-typed list of 24 radios, so any rig
not on it (the reporter's FT-450D) simply wasn't there — and 21 of those 24
IDs were wrong, with the Yaesu / Kenwood / Icom backend numbers shuffled
(the "IC-7300" entry launched rigctld as an FT-991).  Now the list comes
from the installed Hamlib itself via ``rigctld -l``, which always matches
the user's version and covers every rig it supports.  When rigctld isn't
installed (or its output can't be parsed) we fall back to
:data:`FALLBACK_RIG_MODELS`, checked against Hamlib's ``riglist.h``.

Qt-free, so it is unit-testable without a window.
"""
from __future__ import annotations

import functools
import logging
import re
import subprocess
import sys
from typing import NamedTuple

from open_sstv.radio.rigctld_launcher import find_rigctld_executable

_log = logging.getLogger(__name__)


class RigModel(NamedTuple):
    model_id: int
    manufacturer: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.model} ({self.model_id})"


#: Always first in the combo: no rigctld auto-launch.
NO_RIG = RigModel(0, "None", "/ Manual")

#: Used when rigctld isn't installed.  Hamlib model IDs are
#: ``backend * 1000 + n`` (``RIG_MAKE_MODEL`` in include/hamlib/riglist.h):
#: Dummy 0, Yaesu 1, Kenwood 2, Icom 3.  Elecraft rigs live in the Kenwood
#: backend.  tests/radio/test_rig_models.py pins these against riglist.h.
FALLBACK_RIG_MODELS: tuple[RigModel, ...] = (
    RigModel(1, "Hamlib", "Dummy"),
    RigModel(2, "Hamlib", "NET rigctl"),
    RigModel(1020, "Yaesu", "FT-817"),
    RigModel(1022, "Yaesu", "FT-857"),
    RigModel(1023, "Yaesu", "FT-897"),
    RigModel(1027, "Yaesu", "FT-450"),
    RigModel(1028, "Yaesu", "FT-950"),
    RigModel(1035, "Yaesu", "FT-991"),
    RigModel(1036, "Yaesu", "FT-891"),
    RigModel(1040, "Yaesu", "FTDX-101D"),
    RigModel(1041, "Yaesu", "FT-818"),
    RigModel(1042, "Yaesu", "FTDX-10"),
    RigModel(1044, "Yaesu", "FTDX-101MP"),
    RigModel(1046, "Yaesu", "FT-450D"),
    RigModel(1049, "Yaesu", "FT-710"),
    RigModel(2014, "Kenwood", "TS-2000"),
    RigModel(2028, "Kenwood", "TS-480"),
    RigModel(2037, "Kenwood", "TS-590SG"),
    RigModel(2041, "Kenwood", "TS-890S"),
    RigModel(2029, "Elecraft", "K3"),
    RigModel(2044, "Elecraft", "KX2"),
    RigModel(2045, "Elecraft", "KX3"),
    RigModel(2047, "Elecraft", "K4"),
    RigModel(3061, "Icom", "IC-7200"),
    RigModel(3070, "Icom", "IC-7100"),
    RigModel(3073, "Icom", "IC-7300"),
    RigModel(3078, "Icom", "IC-7610"),
    RigModel(3081, "Icom", "IC-9700"),
    RigModel(3085, "Icom", "IC-705"),
)

# A ``rigctld -l`` row: right-aligned model number, then manufacturer,
# model and version columns separated by runs of 2+ spaces.  Single spaces
# occur inside names ("NET rigctl"), so they can't be the separator.
_ROW_RE = re.compile(r"^\s*(\d+)\s{2,}(\S.*?)\s{2,}(\S.*?)\s{2,}\S")

#: Fewer rows than this means the output wasn't the list we expected.
_MIN_PARSED_MODELS = 10

_LIST_TIMEOUT_S = 5.0


def parse_rigctld_list(text: str) -> list[RigModel]:
    """Parse ``rigctld -l`` output (Hamlib 3.x and 4.x formats).

    The header and any line that doesn't look like a model row are skipped.
    """
    models: list[RigModel] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m is None:
            continue
        model_id = int(m.group(1))
        if model_id <= 0:
            continue
        models.append(RigModel(model_id, m.group(2), m.group(3)))
    return models


def _run_rigctld_list() -> str | None:
    exe = find_rigctld_executable()
    if exe is None:
        return None
    # No console window flashing up on Windows every time Settings opens.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [exe, "-l"],
            capture_output=True,
            timeout=_LIST_TIMEOUT_S,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("rigctld -l failed: %s", exc)
        return None
    return result.stdout.decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=1)
def available_rig_models() -> tuple[tuple[RigModel, ...], bool]:
    """``(models, from_hamlib)`` for the Radio model combo.

    *models* starts with :data:`NO_RIG`, then is sorted by manufacturer and
    model.  *from_hamlib* is False when the static fallback was used.
    Cached for the life of the process — the installed Hamlib doesn't change
    under us, and Settings is reopened often.
    """
    output = _run_rigctld_list()
    parsed = parse_rigctld_list(output) if output else []
    from_hamlib = len(parsed) >= _MIN_PARSED_MODELS
    if output is not None and not from_hamlib:
        _log.warning(
            "rigctld -l gave %d parseable models; using the built-in list",
            len(parsed),
        )
    models = parsed if from_hamlib else list(FALLBACK_RIG_MODELS)
    models.sort(key=lambda r: (r.manufacturer.lower(), r.model.lower(), r.model_id))
    return (NO_RIG, *models), from_hamlib


__all__ = [
    "FALLBACK_RIG_MODELS",
    "NO_RIG",
    "RigModel",
    "available_rig_models",
    "parse_rigctld_list",
]
