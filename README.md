# Open-SSTV

An open-source, cross-platform SSTV (Slow Scan Television) transceiver for amateur
radio. Receives and decodes SSTV images live off your radio, and encodes and
transmits images back, with optional Hamlib or direct serial PTT and frequency control.

**Status:** Alpha (v0.1.10). TX and RX paths work end-to-end across all 17 supported
modes. Rig control via rigctld or direct serial CAT is functional. Weak-signal decode
is usable down to roughly 0 dB SNR on Robot 36.

See [CHANGELOG.md](CHANGELOG.md) for the full release history. &nbsp;|&nbsp;
[User Guide](SSTV_App_User_Guide.md)

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
- **17 SSTV modes** -- Robot 36, Martin M1/M2, Scottie S1/S2/DX, PD-90/120/160/180/240/290,
  Wraase SC2-120/180, and Pasokon P3/P5/P7. See the Supported Modes table below.
- **Image editor** -- crop, rotate, flip, and add text overlays (callsign, labels)
  before transmitting. Crop is locked to the target mode's aspect ratio.
- **QSO templates** -- one-click text overlays for common QSO phases (CQ, Exchange,
  73). Placeholder variables (`{mycall}`, `{theircall}`, `{rst}`, `{date}`, `{time}`)
  auto-fill from settings or prompt only for what's needed. Custom templates can be
  created, edited, and saved. Re-clicking a template auto-clears the previous text;
  a dedicated Clear Text button restores the clean image.
- **Correct Robot 36 encoding** -- custom line-pair encoder emits the canonical
  format that all real-world decoders (MMSSTV, SimpleSSTV, QSSTV, slowrx) expect.
  PySSTV's upstream Robot 36 produces a single-line format that most decoders cannot
  decode; Open-SSTV fixes this transparently.
- **PTT sequencing** -- keys the rig, waits for a configurable relay-settle delay
  (0–2 s, default 200 ms), plays SSTV audio, then de-keys. Works with rigctld,
  direct serial CAT, DTR/RTS, or manual (VOX).
- **TX watchdog** -- a 300-second hard-limit timer forces PTT off and aborts playback
  if an encode + playback cycle exceeds the limit, preventing accidental extended
  transmissions.
- **Rig-swap lockout** -- rig connect/disconnect controls are disabled for the full
  duration of a transmission so a mid-TX backend change cannot corrupt PTT state.
- **TX progress bar** with elapsed/total time and percentage.
- **Stop button** -- abort mid-transmission; PTT is always de-keyed cleanly.

### Receive (RX)
- **Live decode** -- start capturing from any audio input, and decoded images appear
  in a scrollable gallery strip as they arrive.
- **Progressive decode** -- partial image preview updates during reception so you
  can see the image building line by line.
- **One-shot re-decode** -- after progressive decode completes, the full audio buffer
  is re-decoded in a single pass for best quality (better bandpass and sync grid).
- **Auto-save** -- optionally save every completed decode automatically to a
  configurable directory, with timestamped filenames.
- **Save images** -- save decoded images manually via Save button or Ctrl+S.
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
- **Configurable baud rate** -- 4800, 9600, 19200, 38400, 57600, or 115200 baud.
- **Rig status bar** -- frequency, mode, and S-meter polled at 1 Hz when connected.
  Graceful disconnect: non-modal status bar message, auto-reconnect on next poll.

### Settings & Configuration
- **Audio device selection** -- separate input/output device pickers with
  input/output gain sliders (0–200%). Device changes take effect immediately.
- **Cross-platform serial port enumeration** -- uses `serial.tools.list_ports` for
  reliable port detection on Linux, macOS, and Windows.
- **TOML-based config** -- all settings persist across sessions in a
  platform-appropriate config directory (`~/.config/sstv_app/` on Linux,
  `~/Library/Application Support/sstv_app/` on macOS).
- **Resilient config loading** -- malformed or missing config and template files
  fall back to built-in defaults instead of crashing.
- **Callsign** -- saved in settings, pre-populated in the image editor's text
  overlay tool for quick QSO card creation.
- **Default TX mode** -- pre-select your preferred mode so it is ready each session.

### CLI Tools
- `open-sstv` -- launch the Qt desktop application.
- `open-sstv-encode` -- encode an image to an SSTV WAV file without the GUI.
- `open-sstv-decode` -- decode an SSTV WAV file back into an image without the GUI.
- Both CLI tools work without Qt installed, for headless or scripted use (Raspberry
  Pi, CI pipelines, batch processing).

## Supported Modes

All 17 modes support both TX (encode) and RX (decode).

| Mode | Resolution | Duration | Color System |
|------|-----------|----------|--------------|
| Robot 36 | 320×240 | ~36 s | YCbCr |
| Martin M1 | 320×256 | ~114 s | RGB |
| Martin M2 | 160×256 | ~57 s | RGB |
| Scottie S1 | 320×256 | ~110 s | RGB |
| Scottie S2 | 160×256 | ~71 s | RGB |
| Scottie DX | 320×256 | ~269 s | RGB |
| PD-90 | 320×256 | ~90 s | YCbCr |
| PD-120 | 640×496 | ~126 s | YCbCr |
| PD-160 | 512×400 | ~161 s | YCbCr |
| PD-180 | 640×496 | ~188 s | YCbCr |
| PD-240 | 640×496 | ~248 s | YCbCr |
| PD-290 | 800×616 | ~289 s | YCbCr |
| Wraase SC2-120 | 320×256 | ~122 s | RGB |
| Wraase SC2-180 | 320×256 | ~183 s | RGB |
| Pasokon P3 | 640×496 | ~203 s | RGB |
| Pasokon P5 | 640×496 | ~304 s | RGB |
| Pasokon P7 | 640×496 | ~406 s | RGB |

**Not yet implemented** (no PySSTV encoder class; custom encoder needed): Robot 8,
Robot 12, Robot 24, Robot 72, Martin M3, Martin M4, Scottie S3, Scottie S4, PD-50.
These are planned for a future release.

## Screenshots

![Open-SSTV main window](docs/screenshots/main-window.png)

*Main window: radio status toolbar (top), Transmit panel (left), Receive panel (right)*

| | |
|---|---|
| ![Audio settings](docs/screenshots/settings-audio.png) | ![Radio settings](docs/screenshots/settings-radio.png) |
| *Audio tab — device and gain settings* | *Radio tab — rig control, protocol, and PTT* |
| ![Images settings](docs/screenshots/settings-images.png) | ![QSO templates](docs/screenshots/qso-templates.png) |
| *Images tab — default TX mode and auto-save* | *QSO templates — one-click callsign and exchange overlays* |

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
open-sstv                                              # Qt desktop app
open-sstv-encode in.png --mode martin_m1 -o out.wav   # CLI encoder
open-sstv-decode in.wav -o out.png                    # CLI decoder
```

## Roadmap

### v0.2 (planned)
- **Remaining SSTV modes** -- Robot 8/12/24/72, Martin M3/M4, Scottie S3/S4, PD-50
  (9 modes needing custom encoders not yet in PySSTV).
- **Waterfall display** -- live FFT spectrogram in the RX panel
  (scope: [docs/waterfall_scope.md](docs/waterfall_scope.md)).
- **Raspberry Pi / ARM support** -- tested on Pi 4/5.
- **Windows support**.
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
