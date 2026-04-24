# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.ui.update_checker``."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from open_sstv.ui.update_checker import UpdateCheckerWorker, _parse_version


# === _parse_version ===


def test_parse_version_with_v_prefix() -> None:
    assert _parse_version("v0.2.15") == (0, 2, 15)


def test_parse_version_without_v_prefix() -> None:
    assert _parse_version("1.0.0") == (1, 0, 0)


def test_parse_version_comparison_newer() -> None:
    assert _parse_version("v0.2.16") > _parse_version("v0.2.15")


def test_parse_version_comparison_same() -> None:
    assert _parse_version("v0.2.15") == _parse_version("0.2.15")


def test_parse_version_non_numeric_segment() -> None:
    assert _parse_version("v1.0.0-beta") == (1, 0, 0)


# === UpdateCheckerWorker ===


def _mock_response(tag: str, url: str = "https://github.com/bucknova/Open-SSTV/releases/tag/v9") -> MagicMock:
    body = json.dumps({"tag_name": tag, "html_url": url}).encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    return mock


def test_update_available_emitted_when_newer(qtbot) -> None:
    worker = UpdateCheckerWorker()
    received: list[tuple[str, str]] = []
    worker.update_available.connect(lambda v, u: received.append((v, u)))

    release_url = "https://github.com/bucknova/Open-SSTV/releases/tag/v9.9.9"
    with patch("open_sstv.ui.update_checker.urllib.request.urlopen",
               return_value=_mock_response("v9.9.9", release_url)):
        worker.check()

    assert len(received) == 1
    assert received[0][0] == "9.9.9"
    assert received[0][1] == release_url


def test_update_available_not_emitted_when_same(qtbot) -> None:
    from open_sstv import __version__

    worker = UpdateCheckerWorker()
    received: list = []
    worker.update_available.connect(lambda v, u: received.append((v, u)))

    with patch("open_sstv.ui.update_checker.urllib.request.urlopen",
               return_value=_mock_response(f"v{__version__}")):
        worker.check()

    assert received == []


def test_update_available_not_emitted_when_older(qtbot) -> None:
    worker = UpdateCheckerWorker()
    received: list = []
    worker.update_available.connect(lambda v, u: received.append((v, u)))

    with patch("open_sstv.ui.update_checker.urllib.request.urlopen",
               return_value=_mock_response("v0.0.1")):
        worker.check()

    assert received == []


def test_check_complete_emitted_on_success(qtbot) -> None:
    worker = UpdateCheckerWorker()
    completed: list[bool] = []
    worker.check_complete.connect(lambda: completed.append(True))

    with patch("open_sstv.ui.update_checker.urllib.request.urlopen",
               return_value=_mock_response("v0.0.1")):
        worker.check()

    assert completed == [True]


def test_network_failure_is_silent(qtbot) -> None:
    worker = UpdateCheckerWorker()
    received: list = []
    completed: list[bool] = []
    worker.update_available.connect(lambda v, u: received.append((v, u)))
    worker.check_complete.connect(lambda: completed.append(True))

    with patch("open_sstv.ui.update_checker.urllib.request.urlopen",
               side_effect=OSError("connection refused")):
        worker.check()  # must not raise

    assert received == []
    assert completed == [True]  # check_complete still fires


# === FirstLaunchDialog.check_updates_enabled ===


pytestmark_gui = pytest.mark.gui


@pytest.mark.gui
def test_check_updates_enabled_default(qtbot) -> None:
    from open_sstv.ui.first_launch_dialog import FirstLaunchDialog

    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)
    assert dlg.check_updates_enabled() is True


@pytest.mark.gui
def test_check_updates_can_be_disabled(qtbot) -> None:
    from open_sstv.ui.first_launch_dialog import FirstLaunchDialog

    dlg = FirstLaunchDialog()
    qtbot.addWidget(dlg)
    dlg._check_updates.setChecked(False)
    assert dlg.check_updates_enabled() is False
