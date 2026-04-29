# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for ``open_sstv.audio.devices``.

We mock out ``sounddevice.query_devices`` and ``sounddevice.query_hostapis``
so this suite runs in headless CI without a real PortAudio backend. Tests
that actually open the speakers live in ``test_output_stream.py`` and are
gated by the ``audio`` marker (see ``pyproject.toml``).
"""
from __future__ import annotations

from unittest.mock import patch

from open_sstv.audio import devices


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
        "open_sstv.audio.devices.sd",
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


# ---------------------------------------------------------------------------
# v0.3.3 — JACK host-API filter on Linux
# ---------------------------------------------------------------------------
#
# JACK's PortAudio host API enumerates the same physical sound card as the
# underlying ALSA hardware, producing confusing duplicate entries in the
# device picker.  On Linux only, we drop everything whose host API name
# matches "jack" (case-insensitive).  Other platforms are unaffected.

_JACK_HOSTAPIS = [
    {"name": "ALSA"},
    {"name": "JACK Audio Connection Kit"},
]
_JACK_DEVICES = [
    # Two ALSA devices.
    {
        "index": 0,
        "name": "USB Codec (ALSA)",
        "hostapi": 0,
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "index": 1,
        "name": "Built-in Audio (ALSA)",
        "hostapi": 0,
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    # Two JACK devices that mirror the same hardware.
    {
        "index": 2,
        "name": "system",
        "hostapi": 1,
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
    {
        "index": 3,
        "name": "alsa_pcm",
        "hostapi": 1,
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
]


def _patch_sd_with_jack(default_device=(0, 1)):
    return patch.multiple(
        "open_sstv.audio.devices.sd",
        query_devices=lambda: list(_JACK_DEVICES),
        query_hostapis=lambda: list(_JACK_HOSTAPIS),
        default=type("D", (), {"device": default_device})(),
    )


def _patch_platform(system: str):
    return patch("open_sstv.audio.devices.platform.system", return_value=system)


class TestJackFilterOnLinux:
    """JACK devices must be hidden from the picker on Linux so users
    don't see the same hardware twice."""

    def test_inputs_drop_jack_devices(self) -> None:
        with _patch_sd_with_jack(), _patch_platform("Linux"):
            inputs = devices.list_input_devices()
        names = [d.name for d in inputs]
        assert "system" not in names
        assert "alsa_pcm" not in names
        # ALSA-side devices are kept.
        assert "USB Codec (ALSA)" in names
        assert "Built-in Audio (ALSA)" in names

    def test_outputs_drop_jack_devices(self) -> None:
        with _patch_sd_with_jack(), _patch_platform("Linux"):
            outputs = devices.list_output_devices()
        names = [d.name for d in outputs]
        assert "system" not in names
        assert "alsa_pcm" not in names
        assert "USB Codec (ALSA)" in names
        assert "Built-in Audio (ALSA)" in names

    def test_case_insensitive_jack_match(self) -> None:
        """A host API named e.g. 'jack' or 'Jack Audio' must also be filtered."""
        hostapis_lowercase = [{"name": "ALSA"}, {"name": "jack"}]
        devs = [
            {
                "index": 0,
                "name": "ALSA Device",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
            {
                "index": 1,
                "name": "JACK Device",
                "hostapi": 1,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        with patch.multiple(
            "open_sstv.audio.devices.sd",
            query_devices=lambda: list(devs),
            query_hostapis=lambda: list(hostapis_lowercase),
            default=type("D", (), {"device": (0, 0)})(),
        ), _patch_platform("Linux"):
            inputs = devices.list_input_devices()
        assert "JACK Device" not in [d.name for d in inputs]
        assert "ALSA Device" in [d.name for d in inputs]


class TestJackFilterIsLinuxOnly:
    """On macOS and Windows the JACK filter is inactive even if (somehow)
    a JACK host API is reported."""

    def test_macos_does_not_filter_jack(self) -> None:
        with _patch_sd_with_jack(), _patch_platform("Darwin"):
            inputs = devices.list_input_devices()
        names = [d.name for d in inputs]
        # All four devices visible on macOS.
        assert "system" in names
        assert "alsa_pcm" in names
        assert "USB Codec (ALSA)" in names

    def test_windows_does_not_filter_jack(self) -> None:
        with _patch_sd_with_jack(), _patch_platform("Windows"):
            outputs = devices.list_output_devices()
        names = [d.name for d in outputs]
        assert "system" in names
        assert "alsa_pcm" in names


class TestJackFilterNoOpWhenNoJackHostApi:
    """A Linux box without any JACK install should not have devices
    silently dropped — the filter is a no-op when no host API name
    contains 'jack'."""

    def test_linux_alsa_only_passes_through(self) -> None:
        alsa_only = [{"name": "ALSA"}]
        devs = [
            {
                "index": 0,
                "name": "USB Codec",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        with patch.multiple(
            "open_sstv.audio.devices.sd",
            query_devices=lambda: list(devs),
            query_hostapis=lambda: list(alsa_only),
            default=type("D", (), {"device": (0, 0)})(),
        ), _patch_platform("Linux"):
            inputs = devices.list_input_devices()
        assert [d.name for d in inputs] == ["USB Codec"]
