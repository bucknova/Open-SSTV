# Architecture

Orientation for someone reading the codebase for the first time: what the
packages are, which way the dependencies point, and how the threads fit
together. Per-feature design notes live alongside the features
(`docs/design/v0.3_templates.md`, `design/remote/architecture.md`).

## High level

```
PySSTV ──► encoder facade ──┐
                            ├─► audio output ──► radio TX (PTT via the Rig backend)
                            │
       UI (Qt 6 / PySide6)──┤
                            │
       audio input ────────►├─► Decoder (FM demod → VIS → sync → per-mode → slant)
                            │       (pure NumPy/SciPy, no UI/IO deps)
       rig control ────────►┘
```

## Package layout

All under `src/open_sstv/`:

- `core/` — pure DSP: encode, decode, demod, VIS, sync, slant, CW, banner.
  **Forbidden** from importing `ui/`, `audio/`, `radio/`, or `config/`.
  NumPy in, NumPy out. Headless-testable.
- `audio/` — the only place that touches PortAudio (via `sounddevice`),
  plus WAV/FLAC file IO and the TCI audio stream. Bridges PortAudio's
  callback thread to the app through a queue and a `QObject` that emits Qt
  signals from its own `QThread`.
- `radio/` — the `Rig` Protocol and its backends: `ManualRig` (no-op, for
  VOX), `RigctldClient` (Hamlib's daemon over TCP), the direct-serial rigs
  (Icom CI-V, Kenwood/Elecraft, Yaesu, and DTR/RTS PTT-only), `TciRig`
  (ExpertSDR/SunSDR), and `FlexRig` (FlexRadio SmartSDR TCP). Plus the band
  plan.
- `templates/` — the v0.3 template system: model, TOML IO, token
  substitution, font handling, and the Qt-free renderer that composites a
  transmitted image.
- `logbook/` — QSO model, SQLite store, ADIF import/export, and the
  capture-flow coordinator.
- `gallery/` — filesystem scanner, the logbook join, and the disk-backed
  thumbnail cache.
- `remote/` — the embedded web server: read model, event hub (SSE), the
  transmit control plane, the server-side compositor, and the served page.
  Qt-free so it can be tested headless.
- `config/` — TOML persistence in the platformdirs config path.
- `ui/` — Qt 6 widgets and `QThread` workers.
- `cli/` — no-Qt entry points (`open-sstv-encode` / `open-sstv-decode`).
- `assets/` — bundled fonts, icons, and starter templates.

## Dependency rule

Dependency arrows point downward. `core/` is at the bottom; `ui/` is at the
top. No back-edges:

```
ui   ─┐
audio ─┼─► config
radio ─┘     │
             ▼
           core
```

The feature packages (`templates/`, `logbook/`, `gallery/`, `remote/`) sit
between `config/` and `ui/`: they may use `core/` and `config/`, and `ui/`
drives them, but none of them import `ui/`. That's what keeps them testable
without a display — the remote server and the template renderer both run
headless.

## UI threading model

`QThread` workers + Qt signals/slots. **Not** asyncio/qasync. The
long-running operations (RX decode loop, TX playback, rig polling) live on
dedicated worker threads and talk to the GUI thread through queued signals
only.

Two threads are deliberately independent of the Qt event loop, because they
must keep working even if the GUI stalls: the remote server's HTTP threads,
and the control plane's safety tick that drives the transmit
dead-man's-switch.

## Main window layout

```
[ Menu: File / View / Tools / Help ]
[ Radio panel: rig connect | freq | mode | S-meter | band plan ]
+--------------------------+--------------------------+
| TX panel                 | RX panel                 |
|   Image preview (drop)   |   In-progress decode     |
|   Template gallery       |   Decoded image strip    |
|   Mode picker            |   Status line            |
|   [Transmit] [Stop]      |                          |
+--------------------------+--------------------------+
[ Status bar: messages | remote-on indicator ]
```

Detached windows: Logbook (Ctrl/Cmd+L), Gallery (Ctrl/Cmd+G), the template
and image editors, and the FFT waterfall.
