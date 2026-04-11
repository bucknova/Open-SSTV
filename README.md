# Open-SSTV

An open-source, cross-platform SSTV (Slow Scan Television) transceiver for amateur
radio. Receives and decodes SSTV images live off your radio, and encodes and
transmits images back, with optional Hamlib or direct serial PTT and frequency control.

**Status:** Alpha (v0.1.1). TX and RX paths work end-to-end. Robot 36, Martin M1,
and Scottie S1 are fully supported for both transmit and receive. Settings persist.
Rig control via rigctld or direct serial CAT is functional. Weak-signal decode is
usable down to roughly 0 dB SNR on Robot 36.

## Goals

- **Open source end-to-end**, GPL-3.0-or-later.
- **Cross-platform**: Linux x86_64 and macOS in v1; Raspberry Pi / ARM and Windows
  planned for v0.2.
- **Modern, intuitive UI** built on Qt 6 (PySide6).
- **Lightweight** enough to run on modest hardware. Pure Python + a small set of
  well-maintained scientific dependencies.
- **Real radio control** via Hamlib's `rigctld` TCP daemon or direct serial
  (Icom CI-V, Kenwood/Elecraft, Yaesu CAT, DTR/RTS PTT) — so any supported radio
  works out of the box without an external daemon.
- **Decoder written from scratch** because no maintained Python SSTV decoder exists
  on PyPI today. Algorithms mirror the well-known C reference `slowrx`.

## Features

### Transmit (TX)
- **Image editor** -- crop, rotate, flip, and add text overlays (callsign, labels)
  before transmitting. Crop is locked to the target mode's aspect ratio.
- **Correct Robot 36 encoding** -- custom line-pair encoder emits the canonical
  format that all real-world decoders (MMSSTV, SimpleSSTV, QSSTV, slowrx) expect.
  PySSTV's upstream Robot 36 produces a single-line format that most decoders cannot
  decode; Open-SSTV fixes this transparently.
- **PTT sequencing** -- keys the rig, waits for relay settle, plays SSTV audio,
  de-keys. Works with rigctld, direct serial, or manual (VOX).
- **TX progress bar** with elapsed/total time and percentage.
- **Stop button** -- abort mid-transmission; PTT is always de-keyed cleanly.

### Receive (RX)
- **Live decode** -- start capturing from any audio input, and decoded images appear
  in a scrollable gallery strip as they arrive.
- **Progressive decode** -- partial image preview updates during reception so you
  can see the image building line by line.
- **One-shot re-decode** -- after progressive decode completes, the full audio buffer
  is re-decoded in a single pass for best quality (better bandpass and sync grid).
- **Save images** -- save decoded images manually via Save button or Ctrl+S, with
  timestamped filenames to a configurable directory.
- **Slant correction** -- least-squares clock-drift compensation so images from
  slightly off-frequency TX stations don't skew.
- **Weak-signal robustness** -- bandpass prefilter, median-filter click rejection,
  and adaptive rolling-threshold sync detection. Usable decode down to ~0 dB SNR
  on Robot 36; partial decode at -5 dB.

### Radio Control
- **rigctld (Hamlib)** -- TCP client for `rigctld`, supporting PTT, frequency,
  mode, and S-meter. Auto-launch rigctld from the settings dialog.
- **Direct serial** -- connect to your rig without an external daemon:
  - **Icom CI-V** -- with preset picker for common models (IC-7300, IC-9700, etc.)
  - **Kenwood / Elecraft** -- standard Kenwood command protocol
  - **Yaesu CAT** -- Yaesu serial protocol
  - **PTT Only (DTR/RTS)** -- simple serial PTT via DTR or RTS line
- **Rig status bar** -- frequency, mode, and S-meter polled at 1 Hz when connected.
  Graceful disconnect: non-modal status bar message, auto-reconnect on next poll.

### Settings & Configuration
- **Audio device selection** -- separate input/output device pickers with
  input/output gain sliders. Device changes take effect immediately.
- **TOML-based config** -- all settings persist across sessions in a
  platform-appropriate config directory (`~/.config/sstv_app/` on Linux,
  `~/Library/Application Support/sstv_app/` on macOS).
- **Callsign** -- saved in settings, pre-populated in the image editor's text
  overlay tool for quick QSO card creation.

### CLI Tools
- `sstv-app-encode` -- encode an image to a WAV file without the GUI.
- `sstv-app-decode` -- decode a WAV file to an image without the GUI.
- Both work without Qt installed, for headless or scripted use.

## Supported Modes

| Mode | Resolution | Duration | TX | RX |
|------|-----------|----------|----|----|
| Robot 36 | 320x240 | ~36s | Yes | Yes |
| Martin M1 | 320x256 | ~114s | Yes | Yes |
| Scottie S1 | 320x256 | ~110s | Yes | Yes |

These three cover the large majority of SSTV QSOs on the air today.

## Architecture

```
PySSTV ──► encoder facade ──┐
   (Robot 36 uses custom    ├─► audio output ──► (radio TX via PTT)
    line-pair encoder)      │
                            │
       UI (Qt 6 / PySide6)──┤
                            │
       audio input ────────►├─► Decoder (FM demod -> VIS -> sync -> per-mode decode -> slant)
                            │       (pure NumPy/SciPy, no UI/IO deps)
       rigctld TCP ────────►│
       direct serial ──────►┘
```

The DSP `core/` is a pure-Python package with no UI, audio, or socket
dependencies -- it's unit-testable in headless CI and can be driven from a
different front-end (TUI, web, CLI) without modification.

## Install (development)

```bash
git clone https://github.com/bucknova/Open-SSTV.git
cd Open-SSTV
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

You will also need Hamlib's `rigctld` for rigctld-based radio control (not
required for direct serial or manual PTT):

- **macOS:** `brew install hamlib`
- **Debian/Ubuntu:** `sudo apt install libhamlib-utils`

## Run

```bash
sstv-app                                             # Qt desktop app
sstv-app-encode in.png --mode martin_m1 -o out.wav   # CLI encoder
sstv-app-decode in.wav -o out.png                    # CLI decoder
```

## Changelog

### v0.1.1

- **Renamed** the app from "Open SSTV" to "Open-SSTV" across all user-facing text.
- **Fixed Robot 36 TX encoding** -- replaced PySSTV's single-line encoder with a
  custom line-pair encoder (`Robot36LinePair`) that emits the canonical format with
  two sync pulses per super-line (300ms, 120 pairs). Transmitted images now decode
  correctly in MMSSTV, SimpleSSTV (iOS), QSSTV, and other real-world receivers.
- **Fixed audio output device selection** -- the saved output device from settings
  was never applied to the TX worker; audio always played through the system default.
  Now resolves the saved device name at startup and after settings changes.
- **Fixed Robot 36 green artifact on RX** -- Cb/Cr planes were initialized to 0
  instead of 128 (the YCbCr neutral midpoint), producing a green fringe on the
  right edge of decoded images. Added chroma-aware pixel sampling with frequency
  floor and right-edge guard.
- **Fixed image editor crash** -- `QGraphicsScene.clear()` destroyed the C++ crop
  rect object while Python still held a reference, causing RuntimeError on
  subsequent access. Now saves geometry before clear.
- **Fixed Mode enum unwrapping** -- Qt signals unwrap `StrEnum` to plain `str`,
  causing `AttributeError` on `.value` access in image save handlers.
- **Added direct serial rig control** -- Icom CI-V, Kenwood/Elecraft, Yaesu CAT,
  and PTT-only via DTR/RTS. Settings dialog has protocol picker, CI-V address
  presets, and a Test Connection button.
- **Added image editor** -- crop (with aspect ratio lock), rotate, flip, and text
  overlays with configurable font size, color, and position.
- **Improved UI resize behavior** -- image editor toolbar split into two rows,
  buttons use proper size policies so text doesn't crop at small window sizes.

### v0.1.0

- Initial release. TX and RX end-to-end for Robot 36, Martin M1, Scottie S1.
- Settings dialog, TOML config persistence, rigctld integration.
- CLI encode/decode tools. Slant correction. Weak-signal robustness.

## Roadmap

### v0.2 (planned)
- **Additional modes** -- PD90, Martin M2, Scottie S2, Scottie DX, Robot 72,
  Wraase SC2-120/180.
- **Raspberry Pi / ARM support** -- tested on Pi 4/5.
- **Windows support**.
- **Waterfall display** -- live FFT spectrogram in the RX panel.
- **Digital VOX** -- auto-detect incoming SSTV and start decoding without manual
  capture start.
- **Drag-and-drop** image loading in the TX panel.

### Future
- FSKID / CW ID transmission.
- ADIF QSO logging.
- PSK Reporter / DX cluster spotting.
- Installer packaging (.deb, .dmg, Flatpak).
- PyPI publish.
- Plugin/macro system.
- Internationalization.

## License

GPL-3.0-or-later. See [LICENSE](./LICENSE).
