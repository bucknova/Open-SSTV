# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``sstv_app.audio.devices``.

We mock out ``sounddevice.query_devices`` and ``sounddevice.query_hostapis``
so this suite runs in headless CI without a real PortAudio backend. Tests
that actually open the speakers live in ``test_output_stream.py`` and are
gated by the ``audio`` marker (see ``pyproject.toml``).
"""
from __future__ import annotations

from unittest.mock import patch

from sstv_app.audio import devices


# Two-device fixture: one input-only mic, one output-only speaker, plus
# a virtual aggregate device with no channels (which the listings should
# silently drop).
_FAKE_HOSTAPIS = [{"name": "Test API"}]
_FAKE_DEVICES = [
    {
        "index": 0,
        "name": "Mic",
        "hostapi": 0,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "index": 1,
        "name": "Speaker",
        "hostapi": 0,
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "index": 2,
        "name": "Aggregate",
        "hostapi": 0,
        "max_input_channels": 0,
        "max_output_channels": 0,
        "default_samplerate": 0.0,
    },
]


def _patch_sd(default_device=(0, 1)):
    return patch.multiple(
        "sstv_app.audio.devices.sd",
        query_devices=lambda: list(_FAKE_DEVICES),
        query_hostapis=lambda: list(_FAKE_HOSTAPIS),
        default=type("D", (), {"device": default_device})(),
    )


def test_list_input_devices_filters_to_inputs() -> None:
    with _patch_sd():
        inputs = devices.list_input_devices()
    assert [d.name for d in inputs] == ["Mic"]
    assert inputs[0].is_input
    assert not inputs[0].is_output


def test_list_output_devices_filters_to_outputs() -> None:
    with _patch_sd():
        outputs = devices.list_output_devices()
    assert [d.name for d in outputs] == ["Speaker"]
    assert outputs[0].is_output
    assert not outputs[0].is_input


def test_aggregate_device_with_no_channels_is_excluded() -> None:
    with _patch_sd():
        all_listed = devices.list_input_devices() + devices.list_output_devices()
    names = {d.name for d in all_listed}
    assert "Aggregate" not in names


def test_default_input_device_resolves() -> None:
    with _patch_sd():
        d = devices.default_input_device()
    assert d is not None
    assert d.name == "Mic"
    assert d.host_api == "Test API"


def test_default_output_device_resolves() -> None:
    with _patch_sd():
        d = devices.default_output_device()
    assert d is not None
    assert d.name == "Speaker"


def test_default_input_returns_none_when_unset() -> None:
    """PortAudio reports ``-1`` when no default input exists (e.g. a
    headless box with no mic). We treat that as no default rather than
    crashing on a missing index lookup."""
    with _patch_sd(default_device=(-1, 1)):
        assert devices.default_input_device() is None


def test_default_output_returns_none_when_unset() -> None:
    with _patch_sd(default_device=(0, -1)):
        assert devices.default_output_device() is None


def test_audio_device_fields_round_trip() -> None:
    with _patch_sd():
        speaker = devices.list_output_devices()[0]
    assert speaker.index == 1
    assert speaker.name == "Speaker"
    assert speaker.host_api == "Test API"
    assert speaker.max_input_channels == 0
    assert speaker.max_output_channels == 2
    assert speaker.default_sample_rate == 48000.0
