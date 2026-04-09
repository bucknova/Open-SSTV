# Architecture

Frozen snapshot of the v1 design that the codebase implements. For the full
implementation plan and milestone breakdown see
`/Users/kevinrowley/.claude/plans/memoized-cuddling-dragon.md`.

## High level

```
PySSTV ──► encoder facade ──┐
                            ├─► audio output ──► (radio TX via PTT from rigctld)
                            │
       UI (Qt 6 / PySide6)──┤
                            │
       audio input ────────►├─► Decoder (FM demod → VIS → sync → per-mode → slant)
                            │       (pure NumPy/SciPy, no UI/IO deps)
       rigctld TCP ────────►┘
```

## Package layout

- `sstv_app/core/` — pure DSP. **Forbidden** from importing `ui/`, `audio/`,
  `radio/`, or `config/`. NumPy in, NumPy out. Headless-testable.
- `sstv_app/audio/` — `sounddevice` wrapper, the only place that touches
  PortAudio. Bridges PortAudio's callback thread to the rest of the app via a
  `queue.Queue` plus a `QObject` that emits Qt signals on its own `QThread`.
- `sstv_app/radio/` — abstract `Rig` Protocol with two backends: `ManualRig`
  (no-op for VOX users) and `RigctldClient` (TCP client for Hamlib's daemon).
- `sstv_app/config/` — TOML persistence in the platformdirs config path.
- `sstv_app/ui/` — Qt 6 widgets and `QThread` workers.
- `sstv_app/cli/` — no-Qt entry points for headless smoke tests.

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

## UI threading model

`QThread` workers + Qt signals/slots. **Not** asyncio/qasync. Two long-running
operations (RX decoder loop, TX playback) live on dedicated worker threads
and communicate with the GUI thread via queued signals only.

## Main window layout (target)

```
[ Menu: File / Radio / Help ]
[ Toolbar: input device | output device | rig status LED | callsign ]
+--------------------------+--------------------------+
| RX panel                 | TX panel                 |
|   Waterfall (live FFT)   |   Image preview (drop)   |
|   In-progress decode     |   Mode picker            |
|   Decoded image gallery  |   [Transmit] [Stop]      |
|                          |   Progress bar           |
+--------------------------+--------------------------+
[ Status bar: rig freq | rig mode | S-meter | RX SNR ]
```
