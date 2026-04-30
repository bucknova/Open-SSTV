# Hamlib Direct Integration — Research and Deferred Plan

> **Status:** Deferred as of 2026-04-30. Architecture is fully specified; implementation has not started. Pending a PTT-type design decision and the time investment to land three PRs cleanly.

This document captures the v0.3.x research, design iterations, and known gotchas for adding a third radio backend — direct Hamlib library access via the `Hamlib` Python bindings — alongside the existing **rigctld TCP** and **direct serial** backends. It exists so a future contributor (or a future Claude session) can resume without re-doing the exploration.

---

## 1. Why we're deferring

The v0.3.x line shipped two of the three radio backends originally planned:

* Direct serial (Icom CI-V, Kenwood, Yaesu, PTT-only DTR/RTS) — built.
* rigctld TCP client — built, including auto-launch and lazy reconnect.
* **Hamlib direct (this document)** — researched, architected through four plan revisions (v1 → v4), but not implemented.

Two reasons for the deferral:

1. **Open design decision around PTT type.** Hamlib's `set_ptt()` only sets the keyed/unkeyed *state*; the *mechanism* (CAT command vs. DTR line vs. RTS line vs. handshake-bit) is configured separately via `set_conf("ptt_type", …)` before `open()`. Per-model defaults exist in the Hamlib database, but a meaningful chunk of users override them. We have not decided whether the UI exposes a PTT-type selector or whether Hamlib direct is CAT-only with DTR/RTS users routed back to the direct-serial backend. See § 5.

2. **Time investment.** Even with the v4 plan locked, the scope is one new backend module, one fake module, two new test files (~60 cases), settings-dialog changes, and main-window dispatch wiring. Three PRs. That's bigger than the small fixes we've been bundling into each v0.3.x point release, and rigctld already gives users a working path on every supported platform.

Coming back to this is a clean pickup — nothing in the deferred plan rots.

---

## 2. What already exists in the codebase

The radio layer is already swappable-by-design. Adding Hamlib direct is a parallel addition; nothing existing changes structurally.

### Direct serial backends — `src/open_sstv/radio/serial_rig.py`

| Class | Protocol | Methods covered |
|---|---|---|
| `SerialPttRig` | DTR/RTS line keying only (no CAT) | PTT only |
| `IcomCIVRig` | CI-V (IC-7300, IC-9700, etc.) | freq, mode, PTT, S-meter |
| `KenwoodRig` | Kenwood/Elecraft text protocol | freq, mode, PTT, S-meter |
| `YaesuRig` | Yaesu modern CAT (FT-991, FT-710, FTDX10, …) | freq, mode, PTT, S-meter |

A factory at `serial_rig.py:841` (`create_serial_rig(protocol, port, baud, …)`) dispatches the four protocols by string name from settings. Each backend uses `pyserial` and a `threading.Lock` to serialize I/O.

### rigctld TCP client — `src/open_sstv/radio/rigctld.py`

`RigctldClient(host, port, timeout_s)` — synchronous client with lazy connect, one-shot auto-reconnect on transport failure, and threading-lock-serialized `_send_recv` to keep concurrent callers from byte-interleaving on the wire. The settings dialog also offers an "Auto-launch rigctld" path that spawns the daemon as a child process tied to the dialog lifecycle.

### Protocol — `src/open_sstv/radio/base.py:48`

```python
class Rig(Protocol):
    name: str
    def open(self) -> None: ...
    def close(self) -> None: ...
    def get_freq(self) -> int: ...                          # Hz
    def set_freq(self, hz: int) -> None: ...
    def get_mode(self) -> tuple[str, int]: ...              # (mode, passband_hz)
    def set_mode(self, mode: str, passband_hz: int) -> None: ...
    def get_ptt(self) -> bool: ...
    def set_ptt(self, on: bool) -> None: ...
    def get_strength(self) -> int: ...                      # dB, signed
    def ping(self) -> None: ...
```

`runtime_checkable` Protocol — backends implement structurally, no ABC inheritance, no registration. Adding a Hamlib backend is a single new file that satisfies these ten methods.

### `RigConnectionMode` StrEnum — `src/open_sstv/radio/base.py`

```python
class RigConnectionMode(StrEnum):
    MANUAL  = "manual"
    SERIAL  = "serial"
    RIGCTLD = "rigctld"
    # HAMLIB_DIRECT = "hamlib_direct"  ← v0.3.x deferred addition
```

The `MainWindow._on_rig_connect()` dispatch switches on this enum to call `_connect_serial()` or `_connect_rigctld()`. A future `_connect_hamlib_direct()` arm slots in identically.

### Existing tests

* `tests/radio/test_serial_rig.py` — comprehensive coverage of the four serial backends (echo stripping, PTT, mode/freq/S-meter, BCD round-trips, OSError wrapping).
* `tests/radio/test_rigctld_client.py` — lazy connect, one-shot reconnect, command serialization under threading, `is_safe_rigctld_arg` injection guard, oversized-response rejection.
* `tests/radio/fake_rigctld.py` — TCP server fixture mocking the daemon.

---

## 3. Hamlib API — factual findings

### 3.1 Python bindings structure

The official `Hamlib` Python module is **SWIG-generated** and ships with Hamlib itself when built `--with-python-binding`. It is *not* installable from PyPI. Distribution paths:

* **Linux** — `apt install python3-libhamlib2` (Debian/Ubuntu) or distro equivalent.
* **Windows** — install a Hamlib build that includes the Python bindings.
* **macOS** — Homebrew's Hamlib does not bundle Python bindings; the user has to build from source. *(This drove the platform-gating decision in § 4.)*

Import shape:

```python
import Hamlib                           # capital H, SWIG module
Hamlib.rig_set_debug(Hamlib.RIG_DEBUG_NONE)
rig = Hamlib.Rig(Hamlib.RIG_MODEL_IC7300)   # model_id is an int constant
```

Mode/PTT/VFO/level constants live as module attributes (`Hamlib.RIG_MODE_USB`, `Hamlib.RIG_VFO_CURR`, `Hamlib.RIG_PTT_ON`, etc.).

### 3.2 `open()` returns an int — never raises

Every Hamlib call that "fails" returns a negative integer error code. The Python bindings do not raise exceptions on Hamlib errors. The contract is:

```python
ret = rig.open()
if ret != Hamlib.RIG_OK:
    msg = Hamlib.rigerror(ret)   # returns a translated error string
    raise RigConnectionError(f"open failed: {msg}")
```

This is a tripwire for new contributors who expect Pythonic exception-raising semantics. **Every Hamlib call needs a `ret != RIG_OK` guard.** The same applies to `set_freq`, `set_ptt`, `set_mode`, `set_conf`, etc.

The negative codes have meanings (e.g. `-RIG_EIO`, `-RIG_ETIMEOUT`, `-RIG_EPROTO`); the v4 plan maps these to our own `RigConnectionError` / `RigCommandError` hierarchy so the existing "5 consecutive poll failures → radio_disconnected" UI logic keeps working unchanged.

### 3.3 PTT — state vs. mechanism

`set_ptt()` only sets keyed/unkeyed state:

```python
rig.set_ptt(Hamlib.RIG_VFO_CURR, Hamlib.RIG_PTT_ON)    # key
rig.set_ptt(Hamlib.RIG_VFO_CURR, Hamlib.RIG_PTT_OFF)   # unkey
```

The *mechanism* (how PTT is physically asserted) is configured **before** `open()` via `set_conf("ptt_type", …)`. Common values: `"RIG"` (CAT command), `"DTR"`, `"RTS"`, `"PARALLEL"`, `"NONE"` (caller asserts via separate path).

Each Hamlib model has a per-rig default in the Hamlib database. A meaningful fraction of users override this — for example, an IC-7300 user might prefer DTR-over-USB instead of CAT PTT to avoid the brief CAT-command latency on key-up.

This is the open design question that triggered the deferral — see § 5.

### 3.4 Signal strength — dB relative to S9

`Hamlib.RIG_LEVEL_STRENGTH` returns a float in **dB relative to S9**. Conventional mapping:

| S-units | dB relative to S9 |
|---|---|
| S9 | 0 |
| S9 + 10 dB | +10 |
| S9 + 20 dB | +20 |
| S7 | −12 |
| S5 | −24 |

This is the documented spec. **Backend calibration quality varies per radio**, and some Hamlib backends report values that are skewed by 5–10 dB off true. For our purposes — surfacing a number in the radio panel's S-meter — a best-effort `int(round(value))` is fine, and this matches the Protocol's signed-dB return type. A per-rig calibration table for known-bad backends would be a quality improvement but is out of scope.

### 3.5 `passband_hz=0` means "use radio default"

When calling `set_mode(vfo, mode_int, passband_hz)`, passing `0` for `passband_hz` instructs Hamlib to use the radio's default passband for that mode (e.g. ~2.4 kHz for SSB on most modern transceivers). This is documented and safe — we use it when the caller doesn't have a specific passband in mind.

### 3.6 Model enumeration — `rig_list_foreach` is broken from Python

The natural Hamlib API for listing supported radios is `rig_list_foreach(callback)` — but **SWIG cannot wrap C function pointers cleanly**, so the Python binding for this is unusable.

Workaround: enumerate model IDs by iterating the integer space and calling `Hamlib.Rig.get_caps(model_id)` for each. The pattern looks like:

```python
Hamlib.rig_load_all_backends()    # populate the model registry
for model_id in range(1, 9999):    # or some sentinel cap
    caps = Hamlib.Rig.get_caps(model_id)
    if caps is not None:
        # caps is a struct-like object with .mfg_name, .model_name, etc.
        register(model_id, caps.mfg_name, caps.model_name)
```

This is the canonical workaround in third-party projects (FLDIGI, WSJT-X, GQRX have all hit this). Performance is fine — typical Hamlib has on the order of hundreds to low thousands of registered models, and `get_caps` is a fast lookup.

For the v4 plan, dynamic enumeration replaces the hardcoded `_COMMON_RIG_MODELS` list (currently used by the rigctld settings group) so the model picker stays accurate as the user's installed Hamlib version evolves.

### 3.7 `Hamlib.cvar.hamlib_version`

The version string is exposed as a SWIG `cvar` — a simple module-level variable, not a function call. Format is plain dotted: `"4.5.5"`, `"4.6.0"`, etc. Older versions sometimes prefixed with `"Hamlib "` so the v4 parser pulls the first `\d+\.\d+` to be tolerant.

### 3.8 Thread safety — none from Hamlib, all from us

Hamlib's C library does **not** provide internal locking on the rig handle. Concurrent calls from multiple threads cause data races and crashes. Our existing pattern (`threading.Lock()` on every public method in the backend) is mandatory, not defensive — it's the only thing standing between a poll-thread `get_strength()` and a TX-thread `set_ptt(True)` colliding.

### 3.9 Windows COM10+ — Hamlib Issue #337

On Windows, serial ports numbered COM10 and above must be specified as `\\.\COM10` to disambiguate from device-namespace lookups. Hamlib 4.x has a known issue (filed as **Issue #337** in the Hamlib repo) where the `\\.\COM10` form is misidentified as a network device and rejected.

Workarounds:

* Recommend COM1–COM9 (re-assign in Windows Device Manager if needed).
* Document the hazard in user-facing setup notes.
* Add UI validation in the settings dialog that warns if the user types `COM10+`. *(Decision pending — see § 5.)*

This issue may be fixed in a later Hamlib 4.x point release; before we ship Hamlib direct on Windows, verify against the user's installed Hamlib version.

---

## 4. Architecture — design plan v4 summary

The plan went through four iterations as design decisions resolved. v4 is the final state.

### Module layout

```
src/open_sstv/radio/
├── __init__.py           # re-export HAMLIB_AVAILABLE
├── base.py               # extend RigConnectionMode with HAMLIB_DIRECT
├── exceptions.py         # unchanged
├── serial_rig.py         # unchanged
├── rigctld.py            # unchanged
└── hamlib_direct.py      # NEW

tests/radio/
├── test_serial_rig.py     # unchanged
├── test_rigctld_client.py # unchanged
├── fake_rigctld.py        # unchanged
├── fake_hamlib.py         # NEW — sys.modules-installable fake
└── test_hamlib_direct.py  # NEW
```

### `hamlib_direct.py` contents

* **`HamlibDirectRig`** class implementing the 10-method `Rig` Protocol structurally.
* **`_parse_hamlib_version(s: str) -> tuple[int, int] | None`** — pure parser. Pulls the first `\d+\.\d+`. Returns `None` on unparseable input.
* **`_check_hamlib_available() -> bool`** — composite check: bindings importable AND parsed version ≥ `HAMLIB_MIN_VERSION`.
* **`HAMLIB_AVAILABLE`** — module constant, set once at import via `_check_hamlib_available()`. The single boolean the UI consults.
* **`HAMLIB_MIN_VERSION = (4, 3)`** — hard floor.
* **`HAMLIB_TESTED_VERSION = (4, 5)`** — informational; surfaced in docs only.
* **Mode tables** — six entries:
  ```
  APP_TO_HAMLIB = {
      "USB":    Hamlib.RIG_MODE_USB,
      "LSB":    Hamlib.RIG_MODE_LSB,
      "FM":     Hamlib.RIG_MODE_FM,
      "AM":     Hamlib.RIG_MODE_AM,
      "PKTUSB": Hamlib.RIG_MODE_PKTUSB,
      "PKTLSB": Hamlib.RIG_MODE_PKTLSB,
  }
  HAMLIB_TO_APP = {v: k for k, v in APP_TO_HAMLIB.items()}
  ```
  Outbound unknowns raise `RigCommandError`. Inbound unknowns degrade to `str(int_value)` so polling never crashes on a mode the app doesn't recognize.

### Method specifications

* **`open()`** — `Hamlib.rig_set_debug(RIG_DEBUG_NONE)` once, `Rig(model_id)`, `set_conf("rig_pathname", device)`, `set_conf("serial_speed", str(baud_rate))`, then PTT-type config (open question — see § 5), then `open()`. Non-zero return → `RigConnectionError`.
* **`close()`** — best-effort, idempotent, swallows errors, logs them.
* **`get_freq()`** — `rig.get_freq(RIG_VFO_CURR)` returns Hz as float; cast to int.
* **`set_freq(hz)`** — `rig.set_freq(RIG_VFO_CURR, hz)`.
* **`get_mode()`** — returns `(mode_int, passband_hz)`; mode_int through `HAMLIB_TO_APP`.
* **`set_mode(mode, passband_hz)`** — translate via `APP_TO_HAMLIB`.
* **`get_ptt()`** — `rig.get_ptt(RIG_VFO_CURR) != RIG_PTT_OFF`.
* **`set_ptt(on)`** — `rig.set_ptt(RIG_VFO_CURR, RIG_PTT_ON if on else RIG_PTT_OFF)`. Two distinct calls; never ORed.
* **`get_strength()`** — `int(round(rig.get_level(RIG_VFO_CURR, RIG_LEVEL_STRENGTH)))`. dB relative to S9; matches Protocol contract.
* **`ping()`** — calls `self.get_freq()`. Success means alive; exceptions propagate.

### Construction-time validation (defence in depth)

Three checks in order, each raising with a specific message:

1. Bindings absent → `RigError("Hamlib Python bindings not installed")`
2. Version unparseable → `RigError("Could not determine Hamlib version")`
3. Version `< (4, 3)` → `RigError("Hamlib 4.3+ required, found <X.Y>")`

These shouldn't trip in normal flow (the UI gates on `HAMLIB_AVAILABLE` first) but guard direct programmatic use, tests, and CLI.

### Settings reuse

No new `AppConfig` fields. The existing `rig_model_id`, `rig_serial_port`, and `rig_baud_rate` (currently used by rigctld) carry over to Hamlib direct unchanged — they have the same semantics inside Hamlib's C library. Migration is zero-friction; old TOMLs continue to load.

The only schema change is extending `RigConnectionMode` with `HAMLIB_DIRECT = "hamlib_direct"`.

### UI behaviour

* **macOS** — Hamlib direct entry is **suppressed** from the connection-mode combo. Setup requires building Hamlib from source on macOS, which is too high a bar for the supported install path. macOS users are routed to rigctld or direct serial.
* **Linux/Windows** — entry appears when `HAMLIB_AVAILABLE` is `True` (bindings importable AND version ≥ 4.3).
* **Model picker** — populated dynamically from `rig_load_all_backends()` + `get_caps()` iteration. Default selection is a placeholder ("Select your radio model…") with `data=0`. Connect button is **disabled** while `rig_model_id == 0` so the Hamlib dummy rig (model 0) can never be the operative selection.
* **Auto-launch checkbox** — hidden in Hamlib direct mode (no daemon to launch).

### Build order — three PRs

1. **PR 1** — `RigConnectionMode` enum extension, `_parse_hamlib_version` + `_check_hamlib_available` helpers, `fake_hamlib.py`, version/availability tests. Pure-Python; no UI, no main_window changes.
2. **PR 2** — `HamlibDirectRig` implementation + protocol/freq/mode/ptt/strength/ping/error-mapping tests. Depends on PR 1.
3. **PR 3** — Settings UI (combo entry, group box, model placeholder, auto-launch hide) + UI tests + `main_window._connect_hamlib_direct()` wire-in + README documentation block. Depends on PR 2.

---

## 5. Open design decisions blocking implementation

These are the items that need a decision before PR 1 starts. Listed in priority order.

### 5.1 PTT type configuration *(blocking)*

**Question:** Does the UI expose a PTT-type selector (CAT / DTR / RTS / Parallel / None), or is Hamlib direct CAT-only with DTR/RTS users routed to the existing direct-serial backend?

**Tradeoffs:**

| Option | Pros | Cons |
|---|---|---|
| **CAT only, route DTR/RTS to direct serial** | Simpler UI, smaller scope, mirrors rigctld's typical setup. | Some users genuinely want Hamlib's protocol abstraction *and* DTR PTT (e.g. for radios where Hamlib's CAT PTT has known latency issues). They'd have to know to drop down to direct serial. |
| **Expose PTT-type selector in Hamlib direct group** | Full feature parity with rigctld and standalone Hamlib usage. | Another setting for users to think about. The default per-model picked by Hamlib's database is *usually* right, so the selector mostly matters for users who already know they want to override it. |
| **Auto-detect from rig caps and don't expose** | Zero UI burden. | "Auto-detect" doesn't really exist — Hamlib's per-model defaults *are* the auto-detection, and overrides are explicit user intent. This option collapses to "CAT only, no override" in practice. |

**Recommendation when resuming:** ship Hamlib direct CAT-only first (option 1), with a footnote in the settings dialog and README pointing DTR/RTS users to direct serial. Add the selector later if user demand surfaces. This keeps PR 3's UI scope tight.

### 5.2 `get_strength()` calibration *(non-blocking)*

dB-relative-to-S9 is the documented spec. Backend quality varies — for some radios Hamlib's reported S-meter is offset by 5–10 dB from the radio's own panel display. Best-effort `int(round(...))` is what the v4 plan uses.

**Optional improvement:** a per-rig calibration table for known-problematic backends. Skip unless users complain — the radio panel's S-meter is informational, not load-bearing.

### 5.3 Windows COM10+ handling *(non-blocking, document at minimum)*

Hamlib 4.x Issue #337 — `\\.\COM10+` syntax misidentified as network device. Decisions:

* **At minimum:** document the hazard in the README's Hamlib direct setup section. *(Required.)*
* **Optional:** UI validation in the settings dialog that warns when the user types `COM10` or higher. *(Nice-to-have; small.)*
* **Optional:** check installed Hamlib version against the version that fixes #337 (when known) and silence the warning if it's a fixed build. *(Probably overkill.)*

---

## 6. Holes found during planning

These were the issues uncovered through the v1 → v4 plan iterations. All except the three in § 5 are resolved; recording them here so a future contributor sees the full reasoning trail rather than just the final shape.

| # | Hole | Resolution |
|---|---|---|
| 1 | **Reuse vs. namespace settings** — should Hamlib direct have its own `hamlib_*` config fields, or share `rig_model_id` / `rig_serial_port` / `rig_baud_rate` with rigctld? | **Resolved (v2):** reuse. Same semantics in Hamlib's library; less re-entry on backend switch. |
| 2 | **Mode vocabulary** — exhaustive (every Hamlib mode constant) or SSTV subset? | **Resolved (v2):** SSTV subset of six (USB, LSB, FM, AM, PKTUSB, PKTLSB). Outbound unknowns raise; inbound unknowns degrade to integer-string. |
| 3 | **macOS Hamlib direct** — show the option, hide it, or warn the user? | **Resolved (v3):** hide on `darwin`. Building Hamlib bindings from source is too high a bar for a supported install path. |
| 4 | **Version floor** — 4.3, 4.4, or 4.5? | **Resolved (v3):** 4.3+ required (Ubuntu 22.04 ships 4.3.3); 4.5+ is the tested/supported range; 4.3–4.4 documented as untested. |
| 5 | **macOS default policy** — change `AppConfig` default for fresh installs on macOS? | **Resolved (v4):** documentation only; no code change to defaults. |
| 6 | **Auto-launch parity** — mirror the rigctld auto-launch checkbox? | **Resolved (v4):** hide the checkbox in Hamlib direct mode (no daemon to launch). |
| 7 | **Module-level constant testability** — how do tests drive the version-availability decision without `importlib.reload`? | **Resolved (v4):** factor into named helpers (`_parse_hamlib_version`, `_check_hamlib_available`); `HAMLIB_AVAILABLE` is the result of one helper call at import. Tests `mock.patch` the helpers directly — no reloads. |
| 8 | **Model 0 default** — the Hamlib dummy rig is model 0, the same as our placeholder integer for "no selection". | **Resolved (v4):** UI invariant — combo's first entry is a placeholder ("Select your radio model…") with `data=0`. Connect button is disabled while `rig_model_id == 0`. |
| 9 | **PTT type configuration** | **Open** — see § 5.1. |
| 10 | **`get_strength()` backend calibration variance** | **Open / non-blocking** — see § 5.2. |

(The Windows COM10+ hazard surfaced after the v4 plan was written; it's recorded here in § 5.3 rather than the holes table to keep the v1–v4 history honest.)

---

## 7. Suggested next steps when resuming

1. **Resolve § 5.1 (PTT type).** Recommendation: ship CAT-only first; add selector if demand surfaces. Decision unblocks all three PRs.

2. **Verify Hamlib version-floor still makes sense.** Check current Ubuntu LTS Hamlib version and adjust `HAMLIB_MIN_VERSION` if needed. The 4.3 floor was chosen for Ubuntu 22.04 (Hamlib 4.3.3); a future Ubuntu 26.04 LTS may have moved the floor up and 4.3–4.4 support may no longer be necessary.

3. **Skim Hamlib changelog for Issue #337 status.** If fixed in a 4.x release, document the fix-version and consider relaxing the COM10+ warning.

4. **Run the three PRs in order:**
   * **PR 1** — Pure-Python: enum extension, two helper functions, `fake_hamlib.py`, version/availability tests. ~150–200 LOC. No UI changes, no main_window changes.
   * **PR 2** — `HamlibDirectRig` backend + tests using the fake module from PR 1. ~250–300 LOC. Depends on PR 1.
   * **PR 3** — Settings dialog UI (combo entry, group box, model placeholder + Connect-button gating, auto-launch hide), `MainWindow._connect_hamlib_direct()` arm, dynamic model picker via `rig_load_all_backends()` + `get_caps()`, README setup section. ~300–400 LOC. Depends on PR 2.

5. **Test on a real Linux + Hamlib 4.5+ box** before merging PR 3. Several of the API quirks (`get_strength` calibration, PTT-type fallthrough on radios with non-default `ptt_type` databases) are hard to validate against the fake.

6. **Document the deferred-then-done state.** When Hamlib direct ships, this file should either be deleted or moved to `docs/archive/` with a final note.

---

## References

* **Hamlib project** — https://github.com/Hamlib/Hamlib
* **Hamlib Python binding examples** — `bindings/python/` in the Hamlib source tree.
* **`Hamlib.h` C header** — canonical reference for return codes, constants, and rig caps struct fields.
* **rigctl/rigctld manpages** — same backend; the daemon is a thin wrapper over the same C calls our Python bindings expose.
* **WSJT-X, FLDIGI, GQRX** — open-source projects using the SWIG bindings; useful for cross-checking patterns (especially the model enumeration workaround).

---

*Last updated 2026-04-30. Author: Kevin (W0AEZ) with research/planning assistance from Claude.*
