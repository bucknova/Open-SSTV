# sstv-app

An open-source, cross-platform SSTV (Slow Scan Television) transceiver for amateur
radio. Receives and decodes SSTV images live off your radio, and encodes and
transmits images back, with optional Hamlib-based PTT and frequency control.

**Status:** Pre-alpha. Active development. Nothing is wired up yet.

## Goals

- **Open source end-to-end**, GPL-3.0-or-later.
- **Cross-platform**: Linux x86_64 and macOS in v1; Raspberry Pi / ARM and Windows
  planned for v0.2.
- **Modern, intuitive UI** built on Qt 6 (PySide6).
- **Lightweight** enough to run on modest hardware. Pure Python + a small set of
  well-maintained scientific dependencies.
- **Real radio control** via Hamlib's `rigctld` TCP daemon — no fragile SWIG
  bindings — so any of the hundreds of radios Hamlib supports works out of the box.
- **Decoder written from scratch** because no maintained Python SSTV decoder exists
  on PyPI today. Algorithms mirror the well-known C reference `slowrx`.

## v1 mode coverage

- Robot 36
- Martin M1
- Scottie S1

These three cover the large majority of SSTV QSOs on the air today. Additional
modes (PD90, Martin M2, Scottie S2/DX, more Robot variants, Wraase) are planned
for v0.2+.

## Architecture (one-liner)

```
PySSTV ──► encoder facade ──┐
                            ├─► audio output ──► (radio TX via PTT from rigctld)
                            │
       UI (Qt 6 / PySide6)──┤
                            │
       audio input ────────►├─► Decoder (FM demod → VIS → sync → per-mode decode → slant)
                            │       (pure NumPy/SciPy, no UI/IO deps)
       rigctld TCP ────────►┘
```

The DSP `core/` is a pure-Python package with no UI, audio, or socket
dependencies — it's unit-testable in headless CI and can be driven from a
different front-end (TUI, web, CLI) without modification.

## Install (development)

```bash
git clone <repo-url> sstv-app
cd sstv-app
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

You will also need Hamlib's `rigctld` for radio control:

- **macOS:** `brew install hamlib`
- **Debian/Ubuntu:** `sudo apt install libhamlib-utils`

## Run

```bash
sstv-app                                 # Qt desktop app
sstv-app-encode in.png --mode martin_m1 -o out.wav   # CLI encoder
sstv-app-decode in.wav -o out.png        # CLI decoder
```

## License

GPL-3.0-or-later. See [LICENSE](./LICENSE).
