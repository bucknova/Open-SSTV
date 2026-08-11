# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.audio.pipewire_route``.

All ``pactl`` subprocess calls are mocked so this suite runs in headless CI
without a live PipeWire session. Every public function must degrade to
"nothing to route" (``False`` / ``None`` / ``[]``) on any failure — that
contract is exercised explicitly throughout, since a routing failure must
fall back to Open-SSTV's existing "play on the system default" behaviour,
never abort a transmission.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from open_sstv.audio import pipewire_route as pw


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["pactl"], returncode=returncode, stdout=stdout, stderr=""
    )


def _patch_pipewire_active(active: bool):
    return patch(
        "open_sstv.audio.pipewire_route.is_pipewire_active", return_value=active
    )


# ---------------------------------------------------------------------------
# is_pipewire_active
# ---------------------------------------------------------------------------


class TestIsPipeWireActive:
    def test_true_when_socket_present(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "pipewire-0").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert pw.is_pipewire_active() is True

    def test_false_when_socket_absent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert pw.is_pipewire_active() is False

    def test_false_when_env_var_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert pw.is_pipewire_active() is False

    def test_never_raises_on_bad_env_value(self, monkeypatch) -> None:
        # os.environ itself rejects an embedded NUL at assignment time, so
        # inject the bad value by swapping in a plain dict rather than via
        # monkeypatch.setenv — exercises the defensive except clause.
        monkeypatch.setattr(pw.os, "environ", {"XDG_RUNTIME_DIR": "bad\x00path"})
        assert pw.is_pipewire_active() is False


# ---------------------------------------------------------------------------
# list_pipewire_sinks / find_pipewire_sink_by_name
# ---------------------------------------------------------------------------

_SINKS_JSON = json.dumps(
    [
        {"index": 64, "name": "alsa_output.usb-foo", "description": "(null)"},
        {"index": 127, "name": "Main_null", "description": "Main"},
        {"index": 175, "name": "Radio_null", "description": "Radio"},
    ]
)


class TestListPipeWireSinks:
    def test_returns_empty_when_pipewire_inactive(self) -> None:
        with _patch_pipewire_active(False):
            assert pw.list_pipewire_sinks() == []

    def test_parses_sinks(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_SINKS_JSON),
            ),
        ):
            sinks = pw.list_pipewire_sinks()
        assert sinks == [
            pw.PipeWireSink(id=64, name="alsa_output.usb-foo", description="alsa_output.usb-foo"),
            pw.PipeWireSink(id=127, name="Main_null", description="Main"),
            pw.PipeWireSink(id=175, name="Radio_null", description="Radio"),
        ]

    def test_null_description_falls_back_to_name(self) -> None:
        """pactl reports the literal string "(null)" (not JSON null) for a
        sink with no PipeWire description set — must not leak that literal
        text into the UI."""
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_SINKS_JSON),
            ),
        ):
            sinks = pw.list_pipewire_sinks()
        rubix = next(s for s in sinks if s.id == 64)
        assert rubix.description == "alsa_output.usb-foo"
        assert "(null)" not in rubix.description

    def test_pactl_missing_returns_empty(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                side_effect=FileNotFoundError(),
            ),
        ):
            assert pw.list_pipewire_sinks() == []

    def test_pactl_timeout_returns_empty(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="pactl", timeout=2.0),
            ),
        ):
            assert pw.list_pipewire_sinks() == []

    def test_malformed_json_returns_empty(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed("not json"),
            ),
        ):
            assert pw.list_pipewire_sinks() == []

    def test_nonzero_returncode_returns_empty(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_SINKS_JSON, returncode=1),
            ),
        ):
            assert pw.list_pipewire_sinks() == []

    def test_malformed_entry_is_skipped_not_fatal(self) -> None:
        bad = json.dumps([{"index": "not-an-int", "name": "x"}, {"index": 5, "name": "ok"}])
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(bad),
            ),
        ):
            sinks = pw.list_pipewire_sinks()
        assert [s.id for s in sinks] == [5]


class TestFindPipeWireSinkByName:
    def test_matches_by_description(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_SINKS_JSON),
            ),
        ):
            found = pw.find_pipewire_sink_by_name("Radio")
        assert found is not None
        assert found.name == "Radio_null"

    def test_no_match_returns_none(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_SINKS_JSON),
            ),
        ):
            assert pw.find_pipewire_sink_by_name("Nonexistent") is None

    def test_empty_name_returns_none_without_querying(self) -> None:
        with patch(
            "open_sstv.audio.pipewire_route.subprocess.run",
            side_effect=AssertionError("must not query pactl for an empty name"),
        ):
            assert pw.find_pipewire_sink_by_name(None) is None
            assert pw.find_pipewire_sink_by_name("") is None


# ---------------------------------------------------------------------------
# snapshot_sink_input_ids / route_active_stream_to_sink
# ---------------------------------------------------------------------------


def _sink_inputs_json(ids: list[int]) -> str:
    return json.dumps([{"index": i} for i in ids])


class TestSnapshotSinkInputIds:
    def test_returns_index_set(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_sink_inputs_json([1, 2, 3])),
            ),
        ):
            assert pw.snapshot_sink_input_ids() == {1, 2, 3}

    def test_empty_when_pipewire_inactive(self) -> None:
        with _patch_pipewire_active(False):
            assert pw.snapshot_sink_input_ids() == set()


class TestRouteActiveStreamToSink:
    _TARGET = pw.PipeWireSink(id=175, name="Radio_null", description="Radio")

    def test_finds_new_id_and_moves_it(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **_kw):
            calls.append(args)
            if "sink-inputs" in args:
                return _completed(_sink_inputs_json([1, 2, 42]))
            return _completed()

        with (
            _patch_pipewire_active(True),
            patch("open_sstv.audio.pipewire_route.subprocess.run", side_effect=fake_run),
        ):
            ok = pw.route_active_stream_to_sink(self._TARGET, before_ids={1, 2})

        assert ok is True
        move_call = next(c for c in calls if "move-sink-input" in c)
        assert move_call == ["pactl", "move-sink-input", "42", "175"]

    def test_no_new_id_within_timeout_returns_false(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                return_value=_completed(_sink_inputs_json([1, 2])),
            ),
        ):
            ok = pw.route_active_stream_to_sink(
                self._TARGET, before_ids={1, 2}, timeout_s=0.05, poll_interval_s=0.01
            )
        assert ok is False

    def test_move_failure_returns_false(self) -> None:
        def fake_run(args, **_kw):
            if "sink-inputs" in args:
                return _completed(_sink_inputs_json([1, 2, 42]))
            return _completed(returncode=1)

        with (
            _patch_pipewire_active(True),
            patch("open_sstv.audio.pipewire_route.subprocess.run", side_effect=fake_run),
        ):
            ok = pw.route_active_stream_to_sink(self._TARGET, before_ids={1, 2})
        assert ok is False

    def test_pactl_missing_returns_false_not_raise(self) -> None:
        with (
            _patch_pipewire_active(True),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                side_effect=FileNotFoundError(),
            ),
        ):
            ok = pw.route_active_stream_to_sink(
                self._TARGET, before_ids=set(), timeout_s=0.05, poll_interval_s=0.01
            )
        assert ok is False

    def test_multiple_new_ids_picks_lowest_deterministically(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **_kw):
            calls.append(args)
            if "sink-inputs" in args:
                return _completed(_sink_inputs_json([1, 2, 50, 7]))
            return _completed()

        with (
            _patch_pipewire_active(True),
            patch("open_sstv.audio.pipewire_route.subprocess.run", side_effect=fake_run),
        ):
            ok = pw.route_active_stream_to_sink(self._TARGET, before_ids={1, 2})

        assert ok is True
        move_call = next(c for c in calls if "move-sink-input" in c)
        # New ids are {50, 7} -- lowest (7) wins, deterministic regardless
        # of dict/set iteration order.
        assert move_call == ["pactl", "move-sink-input", "7", "175"]

    def test_inactive_pipewire_returns_false_immediately(self) -> None:
        with (
            _patch_pipewire_active(False),
            patch(
                "open_sstv.audio.pipewire_route.subprocess.run",
                side_effect=AssertionError("must not call pactl when PipeWire is inactive"),
            ),
        ):
            assert pw.route_active_stream_to_sink(self._TARGET, before_ids=set()) is False
