# Open-SSTV

**Created by Kevin (W0AEZ)** &nbsp;·&nbsp; Built with the assistance of Claude by Anthropic &nbsp;·&nbsp; [![Tests](https://github.com/bucknova/Open-SSTV/actions/workflows/test.yml/badge.svg)](https://github.com/bucknova/Open-SSTV/actions/workflows/test.yml) &nbsp;·&nbsp; [![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/w0aez)

An open-source, cross-platform SSTV (Slow Scan Television) transceiver for amateur
radio. Receives and decodes SSTV images live off your radio, and encodes and
transmits images back, with optional Hamlib, direct serial, or TCI (ExpertSDR2 /
SunSDR2 / AetherSDR) rig control.

**Status: Beta (v0.6.7) — ready for user testing and feedback.** TX and RX paths work
end-to-end across all 22 supported modes, with a built-in QSO logbook (v0.4), an image
gallery (v0.5), and opt-in remote control from a phone or laptop browser (v0.6). Rig
control via rigctld or direct serial CAT is functional. Weak-signal decode is usable
down to roughly 0 dB SNR on Robot 36.

Open-SSTV is looking for testers — on-air reports, audio captures of problem decodes,
and UI feedback all welcome. Please
[file an issue](https://github.com/bucknova/Open-SSTV/issues) with your OS, Python
version, radio / interface, and any terminal output. **For bug reports**, open
Settings → Diagnostics → *Export Diagnostics…* and attach the generated zip — it
contains the recent log file, system info, and your redacted config, which is
usually enough for a fix without follow-up questions. See
[Testing focus areas](#testing-focus-areas) below for the spots we'd most like eyes on.

See [CHANGELOG.md](CHANGELOG.md) for the full release history. &nbsp;|&nbsp;
[User Guide](SSTV_App_User_Guide.md)

## Goals

- **Open source end-to-end**, GPL-3.0-or-later.
- **Cross-platform**: Linux x86_64 and arm64, macOS (Apple Silicon; Intel via `pipx`), and Windows
  (experimental — prebuilt binaries available, real-hardware validation ongoing).
  Raspberry Pi 4/5 on-hardware testing planned.
- **Modern, intuitive UI** built on Qt 6 (PySide6).
- **Lightweight** enough to run on modest hardware. Pure Python + a small set of
  well-maintained scientific dependencies.
- **Real radio control** via Hamlib's `rigctld` TCP daemon, direct serial
  (Icom CI-V, Kenwood/Elecraft, Yaesu CAT, DTR/RTS PTT), or TCI WebSocket
  (ExpertSDR2 / ExpertSDR3 / AetherSDR / SunSDR2) — so any supported radio
  works out of the box without an external daemon.
- **Decoder written from scratch** because no maintained Python SSTV decoder exists
  on PyPI today. Algorithms mirror the well-known C reference `slowrx`.

## Features

### Transmit (TX)
- **22 SSTV modes** -- Robot 36, Martin M1/M2/M3/M4, Scottie S1/S2/DX/S3/S4, PD-50/90/120/160/180/240/290,
  Wraase SC2-120/180, and Pasokon P3/P5/P7. See the Supported Modes table below.
- **Image editor** -- crop, rotate, flip, and add text overlays (callsign, labels)
  before transmitting. Crop is locked to the target mode's aspect ratio. Text
  overlays support both named position presets (Top/Center/Bottom × Left/Center/Right)
  and pixel-precise X/Y spin boxes for fine placement.
- **QSO templates** -- one-click text overlays for common QSO phases (CQ, Exchange,
  73). Placeholder variables (`{mycall}`, `{theircall}`, `{rst}`, `{date}`, `{time}`)
  auto-fill from settings or prompt only for what's needed. Custom templates can be
  created, edited, and saved. Re-clicking a template auto-clears the previous text;
  a dedicated Clear Text button restores the clean image.
- **Layered template compositor (v0.3)** -- richer per-mode templates built from
  photo, text, rectangle, gradient, RX-image, and station-image layers, composited
  at TX time onto the mode's native frame size. Live template gallery on the TX
  panel and a non-modal three-panel editor (Layers / live preview / Properties)
  with MMSSTV-style and named tokens (`%c` / `{callsign}`, `%o` / `{tocall}`, etc.).
- **+RX Image slot** in the template editor (v0.3.1) for one-click reply layouts —
  composites the most-recently-received image into your outgoing TX frame. Default
  geometry sits in the bottom-right at 30%×25% with a small inset; resize and
  reposition like any other layer.
- **8 bundled fonts** (v0.3.1) -- DejaVu Sans Bold, Inter Bold, Press Start 2P,
  Orbitron Bold, Oswald Bold, Exo 2 Bold, Bebas Neue, Share Tech Mono. Orbitron,
  Oswald, and Exo 2 ship as Google variable fonts and snap to their Bold weight
  axis automatically when the family name carries Bold intent. Drop-in
  user-supplied `.ttf` / `.otf` files in `{user_config_dir}/open_sstv/fonts/`
  are still picked up automatically.
- **Rainbow gradient text mode** (v0.3.1) -- per-text-layer Solid/Rainbow toggle.
  Rainbow paints a smooth HSV hue sweep through the glyph mask while preserving
  uniform stroke colour and the layer's alpha; horizontal text sweeps left-to-
  right, stacked text top-to-bottom.
- **Auto center-crop on TX** (v0.3.2) -- before template compositing, the source
  photo is center-cropped to the mode's native aspect ratio and resized to the
  mode's frame size. Banner and overlay placement stays predictable for any input
  aspect (phone portrait, 4:3, 16:9, etc.) without distorting the photo. The
  original image is never modified; the manual image editor remains available
  for finer control.
- **Correct Robot 36 encoding** -- custom line-pair encoder emits the canonical
  format that all real-world decoders (MMSSTV, SimpleSSTV, QSSTV, slowrx) expect.
  PySSTV's upstream Robot 36 produces a single-line format that most decoders cannot
  decode; Open-SSTV fixes this transparently.
- **PTT sequencing** -- keys the rig, waits for a configurable relay-settle delay
  (0–2 s, default 200 ms), plays SSTV audio, then de-keys. Works with rigctld,
  direct serial CAT, DTR/RTS, or manual (VOX).
- **CW station ID** -- optional Morse code callsign appended to every SSTV
  transmission (keyed under the same PTT). Satisfies the Part 97 identification
  requirement automatically. 15–30 WPM, 400–1200 Hz sidetone, 5 ms ramps to
  suppress key clicks. Test Tone is exempt (it's a calibration signal, not a
  communication). On by default; skipped silently with a warning if the callsign
  field is empty.
- **Test Tone** -- a 700 Hz + 1900 Hz two-tone signal at −1 dBFS peak for 5 s,
  triggered from the Radio panel or the Audio Settings tab. Used for ALC
  calibration; the TX output gain slider remains live during playback so the
  operator can tune without stopping the tone.
- **TX output gain** -- 0–100% slider with an optional "Enable overdrive"
  checkbox that expands the ceiling to 200% for setups where ALC won't move at
  100%. Most USB-audio rigs (IC-7300, FT-991A, etc.) sit in the 10–15% range
  with overdrive off.
- **TX banner** -- optional identification strip stamped across the top of every
  transmitted image. Shows your callsign flush-left and "Open-SSTV v{version}"
  flush-right. Three size presets (Small 24 px / Medium 32 px / Large 40 px)
  with matching font sizes, live preview in Settings, plus a "Preview on image…"
  button that shows the banner composited against a real photo before committing
  to TX. The source image is gently shrunk to fit below the strip so user content
  is never overwritten; output dimensions match the SSTV mode exactly.
  Configurable background and text colours; off by default.
- **Per-transmission TX watchdog** -- two-stage timer bounds PTT exposure on a
  stuck encoder or hung audio driver. Stage 1 (encode) is a fixed 30 s budget;
  stage 2 (playback) is computed from the actual encoded sample count plus PTT
  delay with a 20 % margin and a 30 s floor, so a stuck Robot 36 aborts in under
  a minute while Pasokon P7 still gets its ~500 s.
- **Rig-swap lockout** -- rig connect/disconnect controls are disabled for the full
  duration of a transmission so a mid-TX backend change cannot corrupt PTT state.
- **TX progress bar** with elapsed/total time (at the active sample rate) and percentage.
- **Stop button** -- abort mid-transmission; PTT is always de-keyed cleanly.
- **Export to Audio (v0.3.10)** -- write a WAV file of the current TX panel
  composite (template + photo + QSO overlays + TX banner) without keying the
  radio. Uses exactly the same composited image Transmit would emit, so the
  WAV matches what would have gone over the air. Encode runs off the GUI
  thread; same job as `open-sstv-encode` but accessible without dropping to
  a shell. Disabled while live TX is in flight to prevent a mid-TX race.

### Receive (RX)
- **Live decode** -- start capturing from any audio input, and decoded images appear
  in a scrollable gallery strip as they arrive.
- **Per-line incremental decoder** (default since v0.1.24) -- each scan line is
  decoded as soon as its sync pulse arrives rather than reprocessing the full
  growing audio buffer on every flush. O(1) work per line instead of O(N²) total,
  so the decoder stays ahead of real-time on Pi-class hardware even on the longest
  modes (Pasokon P7, Scottie DX). Covers all 22 modes including Robot 36's auto-
  detected per-line and line-pair wire formats. The legacy batch decoder is still
  available via a Settings toggle as a diagnostic fallback.
- **Progressive image preview** -- the partial image updates line-by-line during
  reception so you can see it build in real time.
- **Optional final slant correction** -- when enabled, a single-pass re-decode
  runs on the completed buffer with a global least-squares timing fit to
  compensate for TX/RX sound-card clock drift. Off by default: on weak or noisy
  signals the polyfit has no outlier rejection and can worsen the image. Robot 36
  is explicitly skipped (different color pipeline between the incremental and
  batch paths).
- **Weak-signal mode** -- optional relaxation of the VIS detection thresholds
  (leader presence 35 % → 20 %, start-bit minimum 17 ms → 15 ms) for signals
  audible in the noise that aren't triggering decode. False-positive VIS
  detections are handled gracefully (silent IDLE reset, no user-visible error).
- **Weak-signal robustness** -- bandpass prefilter, median-filter click rejection,
  and adaptive rolling-threshold sync detection. Usable decode down to ~0 dB SNR
  on Robot 36; partial decode at −5 dB.
- **RX-during-TX gate** -- the decoder is paused for the duration of every
  transmission, so the radio's own audio loopback never feeds back into the
  receive path. A 50 ms gate-off delay drains trailing RF before decode
  resumes; decoder state is reset between sessions to avoid stale residue.
- **Image gallery** -- horizontal thumbnail strip of the 20 most-recent decodes,
  newest first, with context-menu Save-As / Copy-to-Clipboard actions. Images
  are persisted to a per-session temp directory to release PIL buffers from
  memory immediately after the thumbnail is rendered (in-memory fallback if
  temp-dir creation fails).
- **Auto-save** -- optionally save every completed decode automatically to a
  configurable directory, with timestamped filenames
  (`sstv_<mode>_YYYYMMDD_HHMMSS.png`).
- **Save images** -- manual Save button, Ctrl+S shortcut, gallery double-click,
  or right-click → Save As…
- **FFT waterfall (v0.3.5)** -- floating spectrogram window (View → Waterfall)
  showing the 0–4 kHz SSTV audio band as a scrolling FFT, with distinct cool
  and warm palettes for RX and TX traffic and dotted reference lines at the
  1200 / 1500 / 1900 / 2300 Hz SSTV tones. Hidden (not destroyed) when
  toggled off, so scroll history is preserved across openings; visibility
  persists across app restarts.
- **RX audio recording (v0.3.6)** -- opt-in lossless capture of the raw
  received audio alongside each decoded image. Settings → Audio → "RX Audio
  Recording" toggles the feature and picks WAV (stdlib) or FLAC (~40 %
  smaller, requires the optional `[flac]` install extra). Lets operators
  re-decode marginal signals later via `open-sstv-decode` or a different
  decoder. Lossy formats are deliberately excluded because compression
  artefacts degrade re-decode quality.
- **Decode Audio (v0.3.10)** -- offline-decode a `.wav` or `.flac` file
  from the RX panel; the result lands in the gallery exactly like a live
  decode. Pairs naturally with v0.3.6 RX audio recording for re-running
  marginal signals through the current decoder. Always enabled, including
  during live capture (results interleave in the same gallery).

### Logbook & QSO Logging (v0.4)
- **Built-in QSO logbook** -- every transmission and reception can be captured
  with its image, mode, frequency, UTC time, callsign, RSV report, and notes.
  Storage is a single schema-versioned SQLite file in the platform data dir;
  rows reference your image files, never copies of them.
- **Capture dialog** -- opens pre-filled at TX/RX completion (TX drafts pull
  ToCall/RST/Name/Note straight from the QSO bar); Esc dismisses without
  writing anything. A config option logs silently instead.
- **Party-line aware** -- SSTV calling frequencies carry everyone's QSOs, so an
  *RX capture* setting controls when receptions prompt: always, only while
  you're in a QSO (ToCall filled), or never -- and any decoded image can be
  logged after the fact from the gallery (right-click → Log QSO…).
- **Logbook window** (Tools → Logbook…, Cmd/Ctrl+L) -- filterable table,
  image preview, edit/delete, manual entry with Save & New.
- **ADIF 3.1.5 import/export** -- interop with HRD, N1MM+, LoTW (via TQSL),
  eQSL, Club Log, and QRZ.com. Exports stamp your station identity per
  record; imports dedupe on (callsign, time, mode). See
  [docs/logbook.md](docs/logbook.md) for the full guide.
- **External Log over UDP (v0.6.7)** -- an **[External Log]** button next to
  **[Logbook…]** on the TX panel's QSO bar broadcasts the current contact to
  companion logging software (QLog, JTAlert, GridTracker, Log4OM, N1MM…) the
  moment you're done working it, in raw-ADIF or WSJT-X's own framed
  protocol -- independent of the local logbook database.

### Remote Access (v0.6)
- **Control from a browser** -- an embedded, **opt-in** web server (off by
  default; loopback + token) lets a paired phone or laptop watch RX live,
  browse the gallery and logbook, and — behind an explicit gate and a
  connected CAT rig — compose and transmit. Enable it in Settings → Remote
  and pair by scanning the QR.
- **Compose & transmit** -- take a photo (camera or upload), crop and frame
  it to the selected mode, add a station template, and send. Safety is
  layered: off by default, a single-writer control lease, per-transmit
  confirmation, and a dead-man's-switch that unkeys the rig if the browser
  goes silent.

### Image Gallery (v0.5)
- **Built-in image browser** -- browse received (and opt-in transmitted)
  images in a thumbnail grid, opened via Tools → Gallery… (Cmd/Ctrl+G).
  The gallery joins your image folder to the logbook: a logged image shows
  its contact's callsign / mode / frequency / time and a one-click **→ QSO**
  jump; unlogged images still appear, dated and mode-tagged from the filename.
- **Filter & group** -- by callsign, mode, or date range, sorted by date,
  callsign, or mode, with lazy on-demand thumbnails cached to disk so the
  grid stays smooth into the tens of thousands of images.
- **Operations** -- re-send an image to the TX panel, export a copy, or
  delete the file (a linked logbook contact is kept, its image link cleared).
  See [docs/gallery.md](docs/gallery.md) for the full guide.

### Radio Control
- **rigctld (Hamlib)** -- TCP client for `rigctld`, supporting PTT, frequency,
  mode, and S-meter. Auto-launch rigctld from the settings dialog.
- **Direct serial** -- connect to your rig without an external daemon:
  - **Icom CI-V** -- with preset picker for common models (IC-7300, IC-9700, etc.)
  - **Kenwood / Elecraft** -- standard Kenwood command protocol
  - **Yaesu CAT** -- FT-991/991A, FT-891, FT-710, FTDX10, FTDX101, FT-950,
    and FT-450/450D. The frequency command's digit width differs between
    the FT-450/450D (8 digits) and the newer rigs (9, zero-padded); it's
    auto-detected from the radio's own response, no per-model setting needed.
  - **PTT Only (DTR/RTS)** -- simple serial PTT via DTR or RTS line
- **TCI (v0.3.5)** -- WebSocket-based control for the Expert Electronics
  SunSDR2 family (ExpertSDR2 / ExpertSDR3) and the AetherSDR. A single
  `ws://host:port` connection (default `127.0.0.1:40001`) carries both CAT
  control and binary PCM audio, so rig control and RX/TX audio share one
  transport with no virtual audio cables required.
- **FlexRadio direct** -- control a 6000-series Flex over the SmartSDR TCP
  API with no `rigctld` and no virtual serial port: enter the radio's IP,
  pick a slice, and PTT / frequency / mode work directly. Audio still comes
  from your sound device (e.g. DAX). The S-meter is not available over this
  path (Flex streams meters as VITA-49/UDP) and reads as 0.
- **Band Plan (v0.3.6)** -- one-click "Band Plan" popup button on the radio
  panel tunes the connected rig to any standard SSTV calling frequency.
  Twelve entries covering HF (80/40/20/17/15/10 m), VHF (2 m), and UHF
  (70 cm) with the correct mode (LSB below 10 MHz, USB above, FM on
  VHF/UHF). The 20 m 14.230 MHz USB primary is shown in bold. Button is
  disabled when no rig is connected or TX is in progress. A rejected
  frequency/mode change (VFO lock, band-edge, an unsupported CAT command)
  is now surfaced as a status-bar message instead of failing silently.
- **SSTV mode policy (Direct Serial only)** -- Settings → Radio → Direct
  Serial → "SSTV mode" controls what the Band Plan button sends for the
  mode half of a tune, mirroring WSJT-X's rig Mode setting: **Voice**
  (default; sends the band-plan entry's plain USB/LSB/FM, unchanged from
  before), **Data/Pkt** (asks for the protocol's data-mode variant instead
  -- e.g. Yaesu `DATA-U`/`DATA-L` -- so SSTV doesn't land on plain USB with
  the speech processor still engaged; currently mapped for Yaesu CAT only,
  other protocols fall back to Voice), or **Don't change mode** (frequency
  only, for operators who already have their data mode set up manually).
- **Configurable baud rate** -- 4800, 9600, 19200, 38400, 57600, or 115200 baud.
- **Rig status bar** -- frequency, mode, and S-meter polled at 1 Hz when connected.
  Graceful disconnect: non-modal status bar message, auto-reconnect on next poll.

### Settings & Configuration
- **Audio device selection** -- separate input/output device pickers. RX input
  gain slider is 0–200 %; TX output gain slider is 0–100 % by default, expandable
  to 200 % with an "Enable overdrive" checkbox for setups where ALC won't move at
  100 %. Device changes take effect immediately; a saved-but-missing device
  surfaces a status-bar notice on startup rather than silently falling back.
- **Cross-platform serial port enumeration** -- uses `serial.tools.list_ports` for
  reliable port detection on Linux, macOS, and Windows. Port list is cached for
  5 s so repeated Settings opens don't re-enumerate USB hardware.
- **TOML-based config** -- all settings persist across sessions in a
  platform-appropriate config directory (`~/.config/open_sstv/` on Linux,
  `~/Library/Application Support/open_sstv/` on macOS,
  `%APPDATA%\open_sstv\` on Windows).
- **Resilient config loading** -- malformed or missing config and template files
  fall back to built-in defaults instead of crashing. Legacy key names
  (e.g. pre-v0.1.24 `experimental_incremental_decode`) are migrated automatically.
- **RX no-progress watchdog timeout (v0.3.8)** -- configurable via Settings →
  Receive (5–300 s, default 5 s). Bounds how long a decode can sit without
  emitting new lines before being aborted; higher values are useful for long
  QSB fades on weak signals, where `walk_sync_grid`'s sync-bridge can resume
  once audio returns. Out-of-range values in hand-edited TOML are clamped on
  load.
- **Callsign** -- saved in settings, pre-populated in the image editor's text
  overlay tool for quick QSO card creation, and used by both CW station ID and
  the TX banner.
- **Default TX mode** -- pre-select your preferred mode so it is ready each session.

### CLI Tools
- `open-sstv` -- launch the Qt desktop application.
- `open-sstv-encode` -- encode an image to an SSTV WAV file without the GUI.
- `open-sstv-decode` -- decode an SSTV WAV file back into an image without the GUI.
- Both CLI tools work without Qt installed, for headless or scripted use (Raspberry
  Pi, CI pipelines, batch processing).

## Supported Modes

All 22 modes support both TX (encode) and RX (decode).

| Mode | Resolution | Duration | Color System |
|------|-----------|----------|--------------|
| Robot 36 | 320×240 | ~36 s | YCbCr |
| Martin M1 | 320×256 | ~114 s | RGB |
| Martin M2 | 160×256 | ~57 s | RGB |
| Martin M3 | 320×128 | ~57 s | RGB |
| Martin M4 | 160×128 | ~29 s | RGB |
| Scottie S1 | 320×256 | ~110 s | RGB |
| Scottie S2 | 160×256 | ~71 s | RGB |
| Scottie DX | 320×256 | ~269 s | RGB |
| Scottie S3 | 320×128 | ~55 s | RGB |
| Scottie S4 | 160×128 | ~36 s | RGB |
| PD-50 | 320×256 | ~50 s | YCbCr |
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

**Not yet implemented** (need custom YCbCr 4:2:2 encoders not in PySSTV): Robot 8,
Robot 12, Robot 24, Robot 72. Planned for a future release.

## Screenshots

![Open-SSTV main window](docs/screenshots/main-window.png)

*Main window — TX panel with the v0.3 template gallery filtered by role, QSO state widget, mode selector, and the live RX panel waiting for a signal*

![Logbook window](docs/screenshots/logbook.png)

*Logbook window (v0.4) — filterable QSO table, image preview with full contact details, and one-click ADIF import/export*

| | |
|---|---|
| ![Template editor](docs/screenshots/template-editor.png) | ![Image editor](docs/screenshots/image-editor.png) |
| *Template editor — three-panel form (Layers / Live preview / Properties) with sample QSO state for token previews* | *Image editor — crop, rotate, flip, and text overlays before encode* |
| ![Audio settings](docs/screenshots/settings-audio.png) | ![Radio settings](docs/screenshots/settings-radio.png) |
| *Audio tab — device, gain sliders, weak-signal mode, incremental decode* | *Radio tab — Direct Serial / Icom CI-V, PTT delay, CW Station ID* |
| ![Images settings](docs/screenshots/settings-images.png) | ![About dialog](docs/screenshots/about-dialog.png) |
| *Images tab — auto-save, TX banner with live preview, update-check opt-in* | *About dialog — v0.3, 22 modes, GPL-3.0-or-later* |

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
       direct serial ──────►┤
       TCI WebSocket ──────►┘
```

The DSP `core/` is a pure-Python package with no UI, audio, or socket
dependencies -- it's unit-testable in headless CI and can be driven from a
different front-end (TUI, web, CLI) without modification.

## Install (prebuilt binary)

Prebuilt binaries for Windows, macOS (Apple Silicon), and Linux (x86_64 and arm64)
are attached to every [GitHub Release](https://github.com/bucknova/Open-SSTV/releases/latest).
No Python install required — just download, unzip, and run.

### macOS (Apple Silicon)

As of v0.3.21 the macOS release ships as a real `.app` bundle (`Open-SSTV.app`),
not a folder of files. Because it's ad-hoc signed (not yet Apple-notarized —
planned), macOS Gatekeeper will refuse to launch it until you clear the
quarantine flag that Safari/Chrome/Finder stamps on every downloaded file.
You only need to do this once per download.

```bash
cd ~/Downloads
unzip -o open-sstv-macos-arm64.zip
xattr -cr Open-SSTV.app        # strip the quarantine flag from the whole bundle
open Open-SSTV.app             # or just double-click in Finder
```

After the one-time `xattr -cr`, double-clicking `Open-SSTV.app` in Finder
launches it like any other Mac app — no Terminal step needed.

**If the launch fails** with `library load disallowed by system policy`, the
quarantine flag wasn't fully stripped. Re-run `xattr -cr Open-SSTV.app` (note:
the *whole bundle* must be the target, not just a file inside it).

Notarized builds are on the roadmap; when that lands, even the `xattr -cr`
step goes away.

### macOS (Intel) — no prebuilt binary

GitHub Actions retired the `macos-13` (Intel) runner pool, which is the
last x86_64 macOS image they hosted; Apple-Silicon-only is now the only
practical CI option for macOS releases. Universal2 is not viable for us
either: PySide6 ships per-architecture wheels rather than a Universal2
fat binary, so a `target_arch="universal2"` PyInstaller build fails at
collect time.

If you're on an Intel Mac, install from source instead:

```bash
python3 -m pip install --user pipx
pipx install open-sstv          # or: pipx install "open-sstv[flac]"
open-sstv
```

`pipx` puts Open-SSTV in an isolated venv on PATH and pulls the
appropriate x86_64 wheels from PyPI for every native dependency. This is
the same install path the development setup below uses, just without the
clone.

### Linux (x86_64 or arm64)

Two formats are published per architecture:

- **`.AppImage`** — single-file, self-contained. `chmod +x open-sstv-*.AppImage && ./open-sstv-*.AppImage`.
- **`.zip`** — unpacked onedir bundle. `unzip open-sstv-linux-*.zip && ./open-sstv/open-sstv`.

### Windows

Download `open-sstv-windows.zip`, unzip, and double-click `open-sstv.exe`.
Windows SmartScreen may show a "Windows protected your PC" dialog on first launch
because the binary is unsigned; click **More info → Run anyway**.

## Install (development)

### Linux and macOS (supported)

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

### Windows (experimental — untested)

> ⚠️ **Open-SSTV has not been tested on Windows.** Every runtime dependency
> (PySide6, numpy, scipy, sounddevice, PySSTV, Pillow, pyserial) publishes
> Windows wheels, so the app *should* install and run, but no one has yet
> driven a real radio from Open-SSTV on Windows. Please treat the instructions
> below as a call for testers rather than a supported install, and
> [file issues](https://github.com/bucknova/Open-SSTV/issues) with your
> findings — good or bad. Full validated Windows support is on the
> [v0.3 roadmap](#post-beta--v03).

Prerequisites: Python 3.11+ from [python.org](https://www.python.org/downloads/)
(tick "Add Python to PATH" during install) and a working Git for Windows.

From a `cmd.exe` or PowerShell prompt:

```powershell
git clone https://github.com/bucknova/Open-SSTV.git
cd Open-SSTV
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

For rigctld-based radio control, download Hamlib for Windows from the
[official releases](https://github.com/Hamlib/Hamlib/releases) (pick a
`hamlib-w64-*.zip`), unzip somewhere permanent, and either add its `bin\`
folder to `PATH` or launch `rigctld.exe` manually before starting Open-SSTV.
Direct serial rig control (Icom CI-V, Kenwood, Yaesu, DTR/RTS PTT) does
**not** require Hamlib.

Known Windows caveats (expected, not yet verified on hardware):

- **Serial ports** appear as `COM3`, `COM4`, etc. — no `/dev/...` paths.
  `serial.tools.list_ports` enumerates them natively, so the Settings
  dialog's port picker should populate automatically.
- **Audio devices** — use the MME or WASAPI host API. ASIO is not exposed
  by `sounddevice` out of the box. If your USB interface doesn't appear,
  open it once in the Windows Sound control panel to register the endpoint.
- **Config directory** — settings persist to `%APPDATA%\open_sstv\`
  (via `platformdirs`), not the Linux/macOS paths mentioned elsewhere in
  this README.
- **`rigctld` auto-launch** from the Settings dialog assumes `rigctld` is
  on `PATH`; if not, launch it manually and connect Open-SSTV to
  `127.0.0.1:4532` instead.

## Run

```bash
open-sstv                                              # Qt desktop app
open-sstv-encode in.png --mode martin_m1 -o out.wav   # CLI encoder
open-sstv-decode in.wav -o out.png                    # CLI decoder
```

## Testing focus areas

If you're kicking the tyres on the v0.6 beta, these are the surfaces we'd most
like eyes on. File an [issue](https://github.com/bucknova/Open-SSTV/issues)
with what you tried and what happened.

- **Remote station** (v0.6) — the newest and least-proven path, and the one
  that can key your transmitter. Pair a phone, watch a live decode, then try
  composing and sending **into a dummy load first**. Confirm the
  dead-man's-switch: start a transmission, then close the tab or walk out of
  Wi-Fi range, and check the rig unkeys. Reports on phones/browsers we
  haven't tried are especially useful.
- **Logbook and gallery** (v0.4 / v0.5) — capture a QSO from a decode, export
  ADIF into your usual logger, and check the gallery↔logbook cross-links land
  on the right contact.

- **Weak-signal RX**. Decode quality on fading / noisy signals; the weak-signal
  mode toggle (Settings → Audio → Receive); false-positive VIS detections (expected
  to reset silently — report if they don't).
- **RX decoder watchdog** (v0.1.36). Intentionally interrupt a transmission
  mid-image — does the partial image land in the gallery? Does the decoder return
  to IDLE cleanly for the next VIS?
- **TX output level calibration**. Test Tone button, output-gain slider, overdrive
  toggle. Does ALC move predictably? Any odd interactions with your rig's USB MOD
  Level or the OS system volume?
- **CW station ID**. At 15 / 20 / 30 WPM, with your real callsign. Audible and
  legible to a human copying by ear?
- **TX preview outline** (v0.1.37). Load an image, walk through the mode dropdown —
  does the green/amber match indicator track what you'd expect?
- **Rig control edge cases**. Mid-session USB unplug; rigctld daemon crash; Icom
  CI-V addresses other than the default 0x94; Kenwood/Yaesu protocol quirks.
- **TCI rigs** (v0.3.5). If you have an ExpertSDR2/3, AetherSDR, or another
  TCI-speaking SDR, on-air reports are especially valuable — this path is
  newly added and has only been validated against one AetherSDR setup.
  Confirm TCI connect, RX audio routing, full SSTV TX, and CW ID over TCI.
- **FFT waterfall** (v0.3.5). Toggle View → Waterfall and confirm RX traffic
  paints a cool palette and TX audio paints a warm palette during a
  transmission; report any visible scrolling stalls or palette glitches.
- **macOS privacy prompts**. If you see Music / iCloud / unexpected access
  requests on launch, note which ones and when — we have a hunch this is PortAudio
  device enumeration but haven't nailed it yet.
- **Windows**. Now in real use, and the CI matrix builds and tests on Windows,
  but it sees far less on-air mileage than macOS and Linux — audio device
  quirks (MME vs WASAPI) and rig-control paths are where problems have
  surfaced so far. Reports very welcome.

## Roadmap

### Next
- **Remaining SSTV modes** -- Robot 8/12/24/72 (4 modes needing custom YCbCr 4:2:2
  encoders not yet in PySSTV).
- **Raspberry Pi 4/5 validation** -- Linux arm64 binaries already ship; on-hardware testing still pending.
- **Digital VOX** -- auto-detect incoming SSTV and start decoding without manual
  capture start.
- **Drag-and-drop** image loading in the TX panel.

### Future
- **Expanded template library** -- a full set of premade QSO templates inspired by MMSSTV and other popular SSTV clients (signal reports, contest exchanges, ragchew layouts).
- **Expanded font support** -- more typeface options for text overlays, including styles common in amateur radio use.
- **Advanced text layout** -- multi-column overlays, alignment controls, and background fill options for finer control over callsign and caption placement.
- FSKID transmission (CW ID already shipping — see Features → Transmit).
- LoTW (TQSL), eQSL, and QRZ.com direct upload from the logbook (ADIF
  export covers them today — see Features → Logbook).
- PSK Reporter / DX cluster spotting.
- Installer packaging (.deb, .dmg, Flatpak).
- PyPI publish.
- Plugin/macro system.
- Internationalization.

### Considered and deferred
- **Optional image post-processing** — non-destructive "clean up image" action in
  the gallery (median filter for salt-and-pepper noise). Deferred: simple filters
  trade detail for smoother noise; more advanced denoisers (NLM, neural) bring
  significant dependency weight for mixed returns. May revisit if a concrete use
  case emerges.

## Author

Open-SSTV is developed by Kevin (W0AEZ).

## License

GPL-3.0-or-later. See [LICENSE](./LICENSE).
