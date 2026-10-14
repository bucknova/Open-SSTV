# SPDX-License-Identifier: GPL-3.0-or-later
"""Abstract ``Rig`` Protocol and the no-op ``ManualRig`` fallback backend.

Defines the surface every rig backend must implement: open / close, get and
set frequency, get and set mode, get and set PTT, signal strength, ping,
and a friendly ``name``. Adding a new backend (CAT-direct, flrig XML-RPC,
USB-HID PTT, ...) is a matter of writing one new class that satisfies this
Protocol — no changes anywhere in ``ui/`` or ``audio/``.

``ManualRig`` is the zero-config default for users who don't run a control
daemon. Every method is a no-op (or returns a sentinel) and the user is
expected to be on VOX or hand-keyed PTT. The TX worker keys ``ManualRig``
exactly the same way it keys a real rig — the no-op just means the calls
return immediately and ``set_ptt`` does nothing on the wire.

The Protocol is ``runtime_checkable`` so the UI can ``isinstance``-check
when deciding whether to show rig controls. We deliberately don't make
``Rig`` an ABC: a Protocol lets users plug in third-party backends
without inheriting from us.
"""
from __future__ import annotations

import re
from enum import StrEnum, unique
from typing import Protocol, runtime_checkable

from open_sstv.radio.exceptions import RigCommandError

#: Allowlist for a rig mode token sent on a line-oriented wire protocol
#: (rigctld ``M <mode>``, TCI ``modulation:0,<mode>;``, Flex ``mode=<mode>``).
#: v0.6.14 audit (security info): modes only ever come from the static band
#: plan today, but a newline, ``;`` or space in one would inject a second
#: command into the stream, so reject anything that isn't a plain token.
_MODE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")


def validate_mode_token(mode: str) -> str:
    """Return *mode* stripped, or raise ``RigCommandError`` if unsafe.

    Accepts 1-16 characters of ``[A-Za-z0-9_-]`` starting with a letter or
    digit, which covers every Hamlib (``PKTUSB``), TCI (``DIGU``), Flex
    (``NFM``) and Yaesu/Icom-style (``DATA-U``, ``USB-D``) token we use.

    >>> validate_mode_token(" USB ")
    'USB'
    """
    token = (mode or "").strip()
    if not _MODE_TOKEN_RE.fullmatch(token):
        raise RigCommandError(f"refusing unsafe rig mode {mode!r}", command="set_mode")
    return token


@unique
class RigConnectionMode(StrEnum):
    """How the application talks to the radio.

    Values match the strings historically stored in ``AppConfig.rig_connection_mode``
    (``"manual"`` / ``"serial"`` / ``"rigctld"``) and emitted by the Settings
    dialog's connection-mode combo box, so ``StrEnum`` gives us a drop-in
    replacement for the bare-string dispatch without touching the TOML
    on-disk format.

    Introduced in v0.1.29 (OP-28): the three string literals used to live in
    three separate places — ``config/schema.py`` default, ``ui/settings_dialog.py``
    combo, and ``ui/main_window.py._on_rig_connect`` dispatch — and renaming
    any one without the others would silently break rig connection.
    """

    MANUAL = "manual"
    SERIAL = "serial"
    RIGCTLD = "rigctld"
    TCI = "tci"
    #: v0.6.1: FlexRadio 6000-series over the SmartSDR TCP API — direct,
    #: no rigctld and no virtual serial port in between.
    FLEX = "flex"


@runtime_checkable
class Rig(Protocol):
    """The interface every rig backend must implement.

    All methods may raise ``RigConnectionError`` or ``RigCommandError`` from
    ``open_sstv.radio.exceptions``. ``ManualRig`` and any other no-op backend
    must never raise.
    """

    name: str

    def open(self) -> None:
        """Establish the link to the rig (or no-op for manual)."""

    def close(self) -> None:
        """Tear down the link. Idempotent."""

    def get_freq(self) -> int:
        """Current operating frequency in Hz."""

    def set_freq(self, hz: int) -> None:
        """Tune the rig to ``hz`` Hz."""

    def get_mode(self) -> tuple[str, int]:
        """``(mode_name, passband_hz)`` tuple, e.g. ``("USB", 2400)``."""

    def set_mode(self, mode: str, passband_hz: int) -> None:
        """Set operating mode and passband."""

    def get_ptt(self) -> bool:
        """``True`` if the rig is currently transmitting."""

    def set_ptt(self, on: bool) -> None:
        """Key (``True``) or unkey (``False``) the transmitter."""

    def get_strength(self) -> int:
        """Signal strength in dB (signed; e.g. ``-73`` for S9)."""

    def ping(self) -> None:
        """Round-trip a cheap command to verify the link is alive."""


class ManualRig:
    """No-op rig backend for users without Hamlib.

    Every method is a no-op or returns a placeholder. The TX worker still
    calls ``set_ptt(True)`` / ``set_ptt(False)`` around playback, but with
    ``ManualRig`` those calls don't touch any hardware — the user keys the
    rig themselves (PTT footswitch, hand mic, or VOX).

    ``get_freq``, ``get_mode``, ``get_strength`` return zero/empty values so
    UI status widgets don't crash if they're polled while ``ManualRig`` is
    selected; the UI is expected to hide those readouts in manual mode but
    must not assume the calls always succeed.
    """

    name: str = "Manual (no rig control)"

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def get_freq(self) -> int:
        return 0

    def set_freq(self, hz: int) -> None:
        del hz

    def get_mode(self) -> tuple[str, int]:
        return ("", 0)

    def set_mode(self, mode: str, passband_hz: int) -> None:
        del mode, passband_hz

    def get_ptt(self) -> bool:
        return False

    def set_ptt(self, on: bool) -> None:
        del on

    def get_strength(self) -> int:
        return 0

    def ping(self) -> None:
        return None


__all__ = ["ManualRig", "Rig", "RigConnectionMode", "validate_mode_token"]
