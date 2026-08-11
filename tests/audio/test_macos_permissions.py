# SPDX-License-Identifier: GPL-3.0-or-later
"""macOS microphone (TCC) authorization probe — issue #35.

The real ``AVCaptureDevice`` call can't be asserted against in CI (its
answer depends on the machine's TCC database, and on Linux/Windows the
frameworks don't exist at all), so these cover the contract that matters:
the probe always returns one of the documented constants, never raises,
and a *denied* result stops capture with an actionable message instead of
reporting "Capturing…" while decoding silence.
"""
from __future__ import annotations

import sys

import pytest

from open_sstv.audio import macos_permissions as perms

_VALID = {perms.AUTHORIZED, perms.DENIED, perms.NOT_DETERMINED, perms.UNKNOWN}


class TestProbe:
    def test_returns_a_documented_status(self) -> None:
        assert perms.microphone_authorization() in _VALID

    def test_never_raises_even_if_objc_is_broken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permissions *hint* must never be what stops capture."""
        import ctypes.util

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            ctypes.util, "find_library",
            lambda _name: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert perms.microphone_authorization() == perms.UNKNOWN

    def test_unknown_off_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert perms.microphone_authorization() == perms.UNKNOWN

    def test_missing_avfoundation_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes.util

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(ctypes.util, "find_library", lambda _name: None)
        assert perms.microphone_authorization() == perms.UNKNOWN

    def test_status_map_covers_apple_values(self) -> None:
        # AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied,
        # 3 authorized. Restricted and denied are the same to a user.
        assert perms._STATUS_MAP[0] == perms.NOT_DETERMINED
        assert perms._STATUS_MAP[1] == perms.DENIED
        assert perms._STATUS_MAP[2] == perms.DENIED
        assert perms._STATUS_MAP[3] == perms.AUTHORIZED

    def test_denied_message_names_the_settings_pane(self) -> None:
        # The app can't re-prompt once denied, so the message has to tell
        # the user exactly where to go.
        assert "System Settings" in perms.DENIED_MESSAGE
        assert "Microphone" in perms.DENIED_MESSAGE


@pytest.mark.gui
class TestCaptureRefusal:
    """InputStreamWorker must refuse rather than 'capture' silence."""

    def _worker(self):
        from open_sstv.audio.input_stream import InputStreamWorker

        return InputStreamWorker()

    def test_denied_refuses_and_reports(
        self, qapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "open_sstv.audio.input_stream.macos_permissions."
            "microphone_authorization",
            lambda: perms.DENIED,
        )
        worker = self._worker()
        errors: list[str] = []
        stopped: list[int] = []
        started: list[int] = []
        worker.error.connect(errors.append)
        worker.stopped.connect(lambda: stopped.append(1))
        worker.started.connect(lambda: started.append(1))

        worker.start()

        assert started == [], "must not claim to be capturing when denied"
        assert stopped == [1], "UI needs the Start button back"
        assert errors and "System Settings" in errors[0]
        assert worker.is_running is False

    @pytest.mark.parametrize(
        "status", [perms.AUTHORIZED, perms.NOT_DETERMINED, perms.UNKNOWN]
    )
    def test_other_statuses_do_not_block_capture(
        self, qapp, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """Only an outright denial refuses; everything else proceeds so a
        misread status can never strand a working setup."""
        monkeypatch.setattr(
            "open_sstv.audio.input_stream.macos_permissions."
            "microphone_authorization",
            lambda: status,
        )
        opened: list[int] = []

        def _boom(*_a: object, **_k: object):
            opened.append(1)
            raise RuntimeError("no audio device in CI")

        monkeypatch.setattr(
            "open_sstv.audio.input_stream.sd.InputStream", _boom
        )
        worker = self._worker()
        worker.error.connect(lambda _m: None)
        worker.start()

        assert opened == [1], f"{status} must still attempt to open the stream"
