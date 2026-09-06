# rigctld TCP protocol — the subset we use

`rigctld` is the network daemon shipped with Hamlib. It exposes hundreds of
supported radios over a tiny line-oriented TCP protocol, which is why we
prefer it over the SWIG Python bindings (no native build, no version drift,
process isolation if it crashes).

Reference: [`rigctld(1)` man page](https://hamlib.sourceforge.net/html/rigctld.1.html).

## Wire format

- One command per line, terminated with `\n`.
- Set commands respond with `RPRT 0\n` on success or `RPRT -<n>\n` on error.
- Get commands respond with one value per line.
- Default port: **4532**.

## Commands we implement in v1

| Method on `RigctldClient`     | Sent (TX)              | Received (RX)        |
|-------------------------------|------------------------|----------------------|
| `set_freq(hz: int)`           | `F <hz>\n`             | `RPRT 0\n`           |
| `get_freq() -> int`           | `f\n`                  | `<hz>\n`             |
| `set_mode(mode, passband_hz)` | `M <mode> <pb>\n`      | `RPRT 0\n`           |
| `get_mode() -> (str, int)`    | `m\n`                  | `<mode>\n<pb>\n`     |
| `set_ptt(on: bool)`           | `T 1\n` / `T 0\n`      | `RPRT 0\n`           |
| `get_ptt() -> bool`           | `t\n`                  | `0\n` / `1\n`        |
| `get_strength() -> int|None`  | `l STRENGTH\n`         | `<int dB>\n`         |
| `ping() -> bool`              | `\dump_state\n`        | multiline; we just check the connection survived |

## Connection lifecycle

- **Lazy connect.** Construction does *not* open a socket — that way the UI
  can hold a `RigctldClient` instance even if the daemon isn't running yet.
- **Idempotent `open()`.** Opens the TCP socket, sends `\dump_state\n`, and
  reads the response. On success caches `_connected = True`.
- **`close()`.** Flushes and closes the socket.
- **Locking.** A `threading.Lock` wraps every `_send_recv` call so the UI
  poll thread (1 Hz S-meter polling) and the TX worker thread (issuing PTT)
  can never interleave bytes on the wire.
- **Reconnect on `BrokenPipeError` / `ConnectionResetError`.** One automatic
  retry, then surface as `RigConnectionError` to the UI as a non-modal status
  bar message — never a modal dialog. A flaky CAT connection must not crash
  the app or interrupt RX.
- **Timeout.** 2 seconds per command. Timeout is treated like a broken pipe
  (reconnect once, then surface).

## Band Plan tuning and the "SSTV mode" setting

A Band Plan tune sends `F <hz>` and (unless the mode family already
matches) `M <mode> <pb>`. The `<mode>` is resolved from the Settings →
Radio → rigctld **SSTV mode** dropdown via
`radio.band_plan.resolve_tune_mode(..., RIGCTLD_PROTOCOL, policy)`:

| Setting            | `<mode>` sent            |
|--------------------|--------------------------|
| Don't change mode  | *(no `M` command)*       |
| Voice (USB/LSB)    | `USB` / `LSB` / `FM`     |
| Data/Pkt           | `PKTUSB` / `PKTLSB` (FM unchanged) |

`PKTUSB` / `PKTLSB` are Hamlib's backend-independent data-mode tokens. A
rig backend without a data mode answers `M PKTUSB` with a non-zero
`RPRT`, which surfaces as a "tune failed" status message.

## Testing without a real radio

Use Hamlib's dummy rig: `rigctld -m 1`. It speaks the full TCP protocol and
returns plausible values, so all of `tests/radio/test_rigctld_client.py` can
run in CI on a Linux box that has `libhamlib-utils` installed.

If `rigctld` isn't installed, the test suite falls back to
`tests/radio/fake_rigctld.py`, a tiny asyncio TCP server that hard-codes
responses to the half-dozen commands above. Tests are parameterized over
both backends.
