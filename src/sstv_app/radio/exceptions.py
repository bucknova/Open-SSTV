# SPDX-License-Identifier: GPL-3.0-or-later
"""Exception hierarchy for the radio control layer.

::

    RigError                 — base class
      RigConnectionError     — socket / transport-level failure
                                (rigctld dead, refused, timed out, ...)
      RigCommandError        — rigctld answered, but with a non-zero RPRT code
                                or an unparseable response

The UI catches these and shows a non-modal status bar message. A flaky CAT
connection must never crash the app or interrupt RX, so callers always
have one base class (``RigError``) they can catch in a single ``except``.
"""
from __future__ import annotations


class RigError(Exception):
    """Base class for every error raised by the radio control layer."""


class RigConnectionError(RigError):
    """Could not reach the rig control daemon, or the link was dropped.

    Raised on connection refused, timeout, broken pipe, DNS failure, or
    any other socket-level failure. Also raised after the one-shot
    auto-reconnect inside ``RigctldClient`` fails for the second time.
    """


class RigCommandError(RigError):
    """The daemon answered, but rejected the command.

    Carries the original command string and the numeric ``RPRT`` code
    rigctld returned (or ``None`` if the response was unparseable).
    """

    def __init__(self, message: str, command: str | None = None, rprt: int | None = None) -> None:
        super().__init__(message)
        self.command = command
        self.rprt = rprt


__all__ = ["RigCommandError", "RigConnectionError", "RigError"]
