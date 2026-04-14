# SPDX-License-Identifier: GPL-3.0-or-later
"""Audio device enumeration.

Wraps ``sounddevice.query_devices()`` so the rest of the app deals in tidy
``AudioDevice`` dataclasses instead of PortAudio's raw dict-of-strings. The
UI populates input/output combo boxes from these lists; the TX worker takes
the selected device's ``index`` and hands it to ``sounddevice.play``.

PortAudio reports the same physical card twice — once as an input-only
device (channels in > 0, channels out = 0) and once as an output-only
device — so the input/output split here mirrors how the user thinks about
their sound card. We also drop "aggregate" / virtual devices that have
zero channels in *and* out, since they show up on macOS but can't be used
for either capture or playback.

Public API:
    list_input_devices()  -> list[AudioDevice]
    list_output_devices() -> list[AudioDevice]
    default_input_device()  -> AudioDevice | None
    default_output_device() -> AudioDevice | None

This is the only module that imports ``sounddevice`` from outside
``audio/``; everything else (TX worker, RX worker, UI) consumes the
``AudioDevice`` dataclass and never touches PortAudio directly.
"""
from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """One PortAudio input or output device.

    ``index`` is the value to pass to ``sounddevice.play(..., device=)``
    or ``sounddevice.InputStream(device=)``. ``host_api`` is the friendly
    name (``Core Audio``, ``ALSA``, ``MME``, …) the UI shows next to the
    device name so users can disambiguate when the same card appears under
    multiple host APIs (common on Windows: WASAPI vs MME vs DirectSound).
    """

    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0


def _build(raw: dict, host_api_names: list[str]) -> AudioDevice:
    return AudioDevice(
        index=int(raw["index"]),
        name=str(raw["name"]),
        host_api=host_api_names[int(raw["hostapi"])],
        max_input_channels=int(raw["max_input_channels"]),
        max_output_channels=int(raw["max_output_channels"]),
        default_sample_rate=float(raw["default_samplerate"]),
    )


def _all_devices() -> list[AudioDevice]:
    """Snapshot every device PortAudio can see, regardless of direction.

    Called by both the input and output listings; cheap enough that we
    don't bother caching across calls (the UI re-queries on every settings
    dialog open so users see hot-plugged devices).
    """
    host_apis = sd.query_hostapis()
    host_api_names = [str(h["name"]) for h in host_apis]
    devices = sd.query_devices()
    out: list[AudioDevice] = []
    for pa_index, raw in enumerate(devices):
        # PortAudio yields each entry as a dict on this codepath; the
        # ``index`` field isn't always present (some host APIs omit it),
        # so we patch it in from the PortAudio enumeration position.
        # Using len(out) would be wrong when earlier devices were skipped.
        if "index" not in raw:
            raw = {**raw, "index": pa_index}
        out.append(_build(dict(raw), host_api_names))
    return out


def list_input_devices() -> list[AudioDevice]:
    """All devices with at least one input channel, in PortAudio order."""
    return [d for d in _all_devices() if d.is_input]


def list_output_devices() -> list[AudioDevice]:
    """All devices with at least one output channel, in PortAudio order."""
    return [d for d in _all_devices() if d.is_output]


def default_input_device() -> AudioDevice | None:
    """The system default input, or ``None`` if PortAudio doesn't have one.

    On a fresh macOS install with no microphone connected ``sd.default.device[0]``
    can be ``-1`` — we treat that as "no default" rather than crashing.
    """
    return _default_for_direction(direction=0)


def default_output_device() -> AudioDevice | None:
    return _default_for_direction(direction=1)


def _default_for_direction(direction: int) -> AudioDevice | None:
    default = sd.default.device
    # ``sd.default.device`` is a 2-tuple ``(input_index, output_index)``;
    # negative or unset entries mean "no default for this direction".
    try:
        idx = int(default[direction])
    except (TypeError, IndexError, ValueError):
        return None
    if idx < 0:
        return None
    for dev in _all_devices():
        if dev.index == idx:
            return dev
    return None


def find_output_device_by_name(name: str | None) -> AudioDevice | None:
    """Look up an output device by its saved name string.

    Returns the first output device whose ``name`` matches, or ``None``
    if no match is found (e.g. the device was unplugged since the config
    was saved). Used by ``MainWindow`` to resolve the config's
    ``audio_output_device`` (a string) into an ``AudioDevice`` with a
    usable PortAudio ``index``.
    """
    if not name:
        return None
    for dev in list_output_devices():
        if dev.name == name:
            return dev
    return None


def find_input_device_by_name(name: str | None) -> AudioDevice | None:
    """Look up an input device by its saved name string."""
    if not name:
        return None
    for dev in list_input_devices():
        if dev.name == name:
            return dev
    return None


__all__ = [
    "AudioDevice",
    "default_input_device",
    "default_output_device",
    "find_input_device_by_name",
    "find_output_device_by_name",
    "list_input_devices",
    "list_output_devices",
]
