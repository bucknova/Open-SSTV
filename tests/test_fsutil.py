# SPDX-License-Identifier: GPL-3.0-or-later
"""``fsutil.atomic_write_bytes``: atomic *and* durable.

The config, template and auto-save writers used to do "write .tmp, then
os.replace" with no fsync, which is atomic but not durable.  A power cut
just after a save can leave an empty file, and an empty config loads as
defaults (2026-10 stability audit).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from open_sstv import fsutil
from open_sstv.fsutil import atomic_write_bytes


def test_writes_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "config.toml"
    atomic_write_bytes(target, b"one")
    atomic_write_bytes(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["config.toml"], "temp file left behind"


def test_data_is_fsynced_before_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(fsutil.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(fsutil.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])

    atomic_write_bytes(tmp_path / "f", b"data")

    assert "replace" in events
    assert events.index("fsync") < events.index("replace"), (
        f"data must reach the disk before the rename; got {events}"
    )


def test_failure_leaves_the_old_file_and_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"good")

    def boom(_a: object, _b: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"good"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_concurrent_writers_never_interleave(tmp_path: Path) -> None:
    """The template writer had no lock and a fixed .tmp name.  Each write
    must now land whole."""
    target = tmp_path / "template.toml"
    payloads = [bytes([i]) * 200_000 for i in range(8)]
    threads = [threading.Thread(target=atomic_write_bytes, args=(target, p)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert target.read_bytes() in payloads, "file is a mix of two writes"
    assert [p.name for p in tmp_path.iterdir()] == ["template.toml"]
