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

from typing import Protocol, runtime_checkable


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


__all__ = ["ManualRig", "Rig"]
