# Open-SSTV v0.2.9 Stability & Reliability Audit — Opus 4.7 (1M ctx)

**Auditor:** Claude Opus 4.7 (1M context window)
**Target:** Open-SSTV v0.2.9 (commit `8ada835`, branch `claude/bold-hellman-027fe7`)
**Scope:** Full codebase read cover-to-cover. 14 audit scopes from concurrency
through Windows compatibility. Builds on the prior Sonnet audits (BZ-01..BZ-09)
and the v0.1.26 Opus 1M audit (OP-01..OP-34), whose fixes are not re-reported.
**Test baseline:** `pytest -q` → **649 passed, 1 skipped in 316.23 s** (clean).

Finding prefix: **OP2-NN**. Severity scale: **P0** critical (data loss, regulatory
risk, guaranteed crash) / **P1** high (user-visible failure, recovery requires
restart) / **P2** medium (edge-case bug, degraded UX, bounded impact) / **P3** low
(cosmetic / future-proofing / micro-optimisation).

---

## Verdict

**Solid with minor polish needed. Ready for broader beta distribution after
triaging the P1 findings.**

The codebase is mature, well-structured, and visibly hardened by the prior audit
cycles. Every major risk class (PTT watchdog, serial exception handling, VIS
false-positive behaviour, thread affinity of timers, signal disconnect on dialog
close, rigctld subprocess cleanup, CI-V / Kenwood / Yaesu set-command timeouts,
audio device-loss watchdog, RX watchdog per-transmission budget) has been
explicitly addressed. The test suite is genuinely load-bearing: 649 tests, 22
modes covered end-to-end, O(N²) regression guards in place, and no xfails or
flaky patterns.

The findings below are real but narrow. Two P1s are user-visible silent-failure
paths worth closing before wider distribution. The P2/P3 findings are
defence-in-depth and perf/future-proofing notes rather than shipping blockers.

---

## P1 — High

### OP2-01 (P1) — `apply_tx_banner` ValueError propagates out of `TxWorker.transmit()` with no UI feedback

**Component:** TX path, UI lifecycle
**Files:** [src/open_sstv/ui/workers.py:520-532](../src/open_sstv/ui/workers.py#L520-L532), [src/open_sstv/core/banner.py:197-202](../src/open_sstv/core/banner.py#L197-L202)

**Description.** Inside `TxWorker.transmit()`, `apply_tx_banner(image, …)` is
called before the encode-step's `try/except Exception`:

```python
try:                                           # line 518
    if self._tx_banner_enabled:
        image = apply_tx_banner(image, …)      # can raise ValueError (too small)

    self.tx_image_prepared.emit(image, mode)   # line 539 — skipped on raise

    try:
        samples = encode(image, mode, …)       # line 543 — broad except guards this
    except Exception as exc:
        self.error.emit(…)
        return
    …
finally:
    encode_watchdog.cancel()                   # line 581 — runs fine
```

A ValueError from `apply_tx_banner` (raised when `content_height <= 0`, see
banner.py:197) bypasses the inner encode-except, propagates through the outer
`finally`, and leaks out of the `@Slot` handler. Qt prints the traceback to
stderr but emits none of: `error`, `transmission_complete`, `transmission_aborted`.

**Reasoning.** Today's smallest shipping mode (Martin M4 / Scottie S4 at
160×128) leaves 88–104 px clearance against the largest banner (40 px), so the
guard in banner.py:197 never fires in practice. But v0.1.29 deliberately added
the guard (OP-23) because a future small mode or hand-edited preset *could*
trip it, and the silent failure makes diagnosis hard. This is the one path that
escapes the otherwise-thorough error-handling pattern in `transmit()`.

**Also affected:** `transmit_test_tone()` doesn't call banner at all, so this is
TX-only.

**Proposed fix.** Wrap the banner call in a try/except matching the encode
pattern:

```python
try:
    if self._tx_banner_enabled:
        image = apply_tx_banner(image, …)
except Exception as exc:  # noqa: BLE001
    self.error.emit(f"TX banner failed: {exc}")
    return
```

Placed *inside* the outer try so the `finally` still cancels the watchdog.

---

### OP2-02 (P1) — Synchronous rig `open` + `ping` on GUI thread freezes UI up to ~4 s on unresponsive radios

**Component:** Radio control, UI responsiveness
**Files:** [src/open_sstv/ui/main_window.py:1104-1144](../src/open_sstv/ui/main_window.py#L1104-L1144) (serial), [src/open_sstv/ui/main_window.py:1146-1220](../src/open_sstv/ui/main_window.py#L1146-L1220) (rigctld)

**Description.** `_connect_serial` calls `rig.open()` (fast) followed by
`rig.ping()` which does a full CAT round-trip — on Icom that's
`get_freq → _command(b"\x03") → _read_response` with a 1 s timeout; Kenwood and
Yaesu are similar with 1 s each. On a slow or half-alive radio (USB audio chip
mid-renegotiation, stale port), the GUI thread can freeze for the full timeout.

`_connect_rigctld`'s auto-launch path is already deferred via
`QTimer.singleShot(500, …)` (line 1189), but the *non*-auto-launch path at line
1192 calls `_finish_rigctld_connect(host, port)` synchronously. That function
runs `RigctldClient.open()` (`socket.create_connection` — up to 2 s timeout) +
`ping()` (another 2 s). Worst case ~4 s of GUI freeze when the rigctld host is
unresponsive.

**Reasoning.** The rig-poll thread exists precisely so steady-state CAT reads
don't block the UI. First-connect falls outside that pattern and was overlooked.

**Proposed fix.** Move the connect sequence to a one-shot thread (or route
through a dedicated slot on `_rig_poll_worker`). Emit a signal when done; the
GUI-thread slot picks up the result and updates `self._rig` / radio panel.

**Mitigations already in place.** The rigctld timeout is 2 s (not infinite) and
the pyserial timeout is 0.5–1 s, so the freeze is bounded. The user will see
the window respond after at most ~4 s.

---

## P2 — Medium

### OP2-03 (P2) — RX Start/Stop button allows double-click race that can request two audio streams

**Component:** UI robustness, RX lifecycle
**Files:** [src/open_sstv/ui/rx_panel.py:258-260](../src/open_sstv/ui/rx_panel.py#L258-L260), [src/open_sstv/ui/main_window.py:891-935](../src/open_sstv/ui/main_window.py#L891-L935)

**Description.** `RxPanel._on_start_clicked` emits `capture_requested(not
self._capturing)`. `self._capturing` is only updated when `audio_worker.started`
fires back. Between click and `started`, the button is still enabled and a
second click also reads `self._capturing == False` and emits a second start
request.

In `MainWindow._on_capture_requested(True)`, each call creates a fresh
`_start_once` closure and connects it to `rx_worker.reset_done`. When
`reset_done` fires, *both* closures execute — each disconnects the identity of
its own closure (different function objects), each emits
`_request_start_capture`. `InputStreamWorker.start()` is called twice; the
second call hits the duplicate-start guard at input_stream.py:174-176 and emits
`"Input stream already running; stop first"` via the error signal.

**Visible impact.** User double-clicks Start → sees a cryptic error message
flash in the status bar. RX itself starts correctly (first call wins), but the
error is confusing.

**Proposed fix.** Disable the Start button immediately in `_on_start_clicked`
and re-enable it from `set_capturing()`. Mirror the pattern `TxPanel` already
uses (`set_transmitting` gates the button).

---

### OP2-04 (P2) — Image gallery temp files keyed on `id(image)` can collide after GC

**Component:** UI / memory management
**Files:** [src/open_sstv/ui/image_gallery.py:145](../src/open_sstv/ui/image_gallery.py#L145)

**Description.** In disk-backed mode, each decoded image is saved at
`img_{id(image)}.png`. `id()` returns the memory address of the PIL object,
which **is reused** after garbage collection. If the first image's Python
object is freed (typical — the gallery only holds the on-disk path afterwards),
a later image can receive the same `id()`. `image.save(str(img_path))` then
silently overwrites the older file that's still referenced by an earlier
gallery item.

**Repro (theoretical).** Fill gallery with 20 images → first several have their
PIL objects freed → continue decoding → a new image gets the same `id()` as one
still listed in the gallery → clicking the older thumbnail loads the new image
content.

**Reasoning.** In practice, `_MAX_IMAGES = 20` and Python's allocator reuses
addresses only under pressure, so the collision rate is low. But it's a
determinism gap that will eventually bite a heavy user.

**Proposed fix.** Use a monotonic per-instance counter or `uuid.uuid4().hex`:
`f"img_{self._counter}.png"`; increment on every `add_image`.

---

### OP2-05 (P2) — `_read_until_rprt` buffer unbounded if rigctld produces no newlines

**Component:** Radio / rigctld
**Files:** [src/open_sstv/radio/rigctld.py:219-246](../src/open_sstv/radio/rigctld.py#L219-L246)

**Description.** `_read_until_rprt` grows `buf` indefinitely until it finds an
`RPRT ` marker followed by `\n`. The explicit cap at line 230 counts *lines*
(`buf.count(b"\n")`), not bytes. A malicious or buggy daemon that streams
large chunks without newlines would keep `buf` growing until the socket timeout
(`self._timeout_s`, default 2 s) fires.

**Reasoning.** Practical exposure is tiny — the daemon is localhost / LAN
rigctld that Hamlib ships. But if the peer is compromised or the network path
is manipulated, this is an unbounded memory allocation on the poll thread.

**Proposed fix.** Add a byte-size cap alongside the line-count cap, e.g.
`if len(buf) > 64 * 1024: raise RigCommandError(...)`.

---

### OP2-06 (P2) — `load_config` catches `Exception` — silently returns defaults on permission errors

**Component:** Config persistence, diagnosability
**Files:** [src/open_sstv/config/store.py:66-68](../src/open_sstv/config/store.py#L66-L68)

**Description.**

```python
except Exception:  # noqa: BLE001 — corrupt file must never crash startup
    _log.warning("Config file %s is corrupt or unreadable — using defaults", path)
    return AppConfig()
```

This catches `tomllib.TOMLDecodeError` (correct), but *also* catches
`PermissionError`, `IsADirectoryError`, and any other OSError. On a user's
machine where the config file exists but isn't readable (root-owned, ACL
mismatch), they see an empty config every launch. The log line goes to stderr
but there's no UI surface.

**Reasoning.** Different from a corrupt file (which genuinely should fall back).
A permission error is an operator configuration problem they'd want to see
surfaced.

**Proposed fix.** Narrow the except to `(tomllib.TOMLDecodeError,
UnicodeDecodeError)` and let OSError propagate; `main()` already can't do much
with it but at least the traceback on stderr names the problem clearly. Or keep
broad-catch but emit a status-bar message from `MainWindow` when defaults were
used.

---

### OP2-07 (P2) — Non-atomic `save_config` write can corrupt TOML on SIGKILL mid-write

**Component:** Config persistence, data durability
**Files:** [src/open_sstv/config/store.py:84-93](../src/open_sstv/config/store.py#L84-L93)

**Description.** `save_config` opens the real config path for writing and
dumps TOML directly. If the process is killed between `open("wb")` and the
write completing, the file is truncated / half-written. On next launch
`load_config` hits `TOMLDecodeError`, falls back to defaults (OP2-06 makes this
silent), and the user's settings appear wiped.

**Proposed fix.** Write to `path.with_suffix(path.suffix + ".tmp")` first, then
`os.replace(tmp, path)` — atomic on every supported platform. Same pattern
applied to `save_templates` in templates.py:230-236.

---

### OP2-08 (P2) — `AppConfig.__post_init__` silently clamps `cw_id_wpm` / `cw_id_tone_hz` from hand-edited TOML

**Component:** Config robustness
**Files:** [src/open_sstv/config/schema.py:167-168](../src/open_sstv/config/schema.py#L167-L168)

**Description.** `cw_id_wpm = max(15, min(30, self.cw_id_wpm))` silently clamps
any out-of-range value to the UI's range. A user hand-editing TOML to set
`cw_id_wpm = 12` for slower ID would see their value rewritten to 15 on next
save with no log message. The overdrive migration at line 158-164 *does* log;
the CW clamps don't.

**Proposed fix.** Log the clamps at INFO level when the input differs from the
clamped output, matching the overdrive pattern.

---

### OP2-09 (P2) — `QTimer.singleShot` callback in `_connect_rigctld` may fire after window destruction

**Component:** Lifecycle
**Files:** [src/open_sstv/ui/main_window.py:1189](../src/open_sstv/ui/main_window.py#L1189)

**Description.** `QTimer.singleShot(500, lambda: self._finish_rigctld_connect(host, port))`.
The lambda keeps `self` alive via closure. If the user closes the window during
the 500 ms delay (`closeEvent` stops the rig-poll timer, kills rigctld, etc.),
the deferred callback still fires and runs `_finish_rigctld_connect`. That
function calls `self._rig_poll_timer.start()` on a timer we just stopped, plus
`client.open()` / `client.ping()` that issue serial/socket I/O during teardown.

**Observed impact.** No crash in practice (Qt's slot-invocation machinery is
forgiving), but the rig is polled once or twice after the user's stated intent
to disconnect.

**Proposed fix.** Keep a weak reference or guard with `if
QCoreApplication.instance() is None or not self.isVisible(): return`. Or store
the QTimer as an attribute and stop it in `closeEvent`.

---

### OP2-10 (P2) — Robot36IncrementalDecoder fallback-threshold arithmetic truncates non-multiples

**Component:** Decoder correctness (edge case)
**Files:** [src/open_sstv/core/incremental_decoder.py:1311, 1448-1450](../src/open_sstv/core/incremental_decoder.py#L1448-L1450)

**Description.**

```python
_DETECT_FALLBACK_SAMPLES: int = 3 * 48_000  # 144_000
…
fallback_threshold = self._fs * (self._DETECT_FALLBACK_SAMPLES // 48_000)
```

Works correctly today because `144_000 // 48_000 = 3`. But the intent is "3
seconds of audio" — and if anyone later tunes `_DETECT_FALLBACK_SAMPLES` to
`100_000` (~2.08 s), the integer division yields `100_000 // 48_000 = 2` and
the threshold collapses to `2 × fs` (2 s exact), not 2.08 s. The fragility is
compounded by `_DETECT_FALLBACK_SAMPLES` being documented as a sample count but
only used as a seconds proxy.

**Proposed fix.** Store the constant as seconds (`_DETECT_FALLBACK_S: float =
3.0`) and multiply: `fallback_threshold = int(self._fs * self._DETECT_FALLBACK_S)`.

---

### OP2-11 (P2) — `IncrementalDecoderBase.feed()` does `np.concatenate` on every chunk — O(N²) per-receive CPU

**Component:** Decoder perf
**Files:** [src/open_sstv/core/incremental_decoder.py:432](../src/open_sstv/core/incremental_decoder.py#L432)

**Description.** `self._buf = np.concatenate([self._buf, arr])` on every feed
call copies the entire current buffer. `_prune()` caps `_buf` at roughly one
line period of audio post-sync, so steady-state buffer size is small
(~7200 samples for Robot 36 at 48 kHz). But at ~10 feed/s on a flush cadence of
0.1 s (DECODING state), that's ~70 k samples copied per second — negligible on
laptop hardware, noticeable on Raspberry Pi-class hardware.

**Proposed fix.** Keep `_buf` as a `list[np.ndarray]` with a parallel
`_buf_total_len: int` counter; concatenate only when a decode window slices
across multiple chunks. Would require audit of every `len(self._buf)` and
`self._buf[slice]` access site.

**Severity downgrade rationale.** Real-time budget is comfortable on every
modern platform we ship (tests exercise this on CI). Fix is invasive; defer
until a user reports a Pi underperforming.

---

### OP2-12 (P2) — `Robot36IncrementalDecoder._try_detect` re-concatenates `_pending` on every feed until backend chosen

**Component:** Decoder perf (narrow window)
**Files:** [src/open_sstv/core/incremental_decoder.py:1405-1409](../src/open_sstv/core/incremental_decoder.py#L1405-L1409)

**Description.** During the ≤3 s detection window, every `feed` call does
`np.concatenate(self._pending)` to examine the tail. Once the backend is
chosen, `_pending` is cleared and subsequent feeds go straight to the backend
— so total wasted work is bounded by 3 s × fs samples.

**Proposed fix.** Same pattern as OP2-11: accumulate by list length and only
concat when measuring. Low impact because detection is already bounded.

---

### OP2-13 (P2) — `_Robot36PerLineIncrementalDecoder` always re-emits the prior row even when the high-water-mark guard will drop it

**Component:** Decoder perf
**Files:** [src/open_sstv/core/incremental_decoder.py:1106-1117](../src/open_sstv/core/incremental_decoder.py#L1106-L1117), [src/open_sstv/core/decoder.py:1298-1300](../src/open_sstv/core/decoder.py#L1298-L1300)

**Description.** Each call to `_decode_window` emits `(grid_index, row)`
followed by `(grid_index - 1, back_filled_row)` (the back-fill chroma update).
The high-water-mark guard in `Decoder._feed_decoding_incremental` skips the
second emission when `row_idx + 1 <= max_row`. But the decoder backend has
already allocated the `rgb[0].copy()` and written `self._image[row]`.

The `self._image` write is actually necessary (so `get_image()` returns the
updated row the next time progress is emitted), so the only wasted work is the
`.copy()` and the append to `out`. Microseconds per line.

**Proposed fix.** Backend could skip the `out.append` for the back-fill since
the caller guarantees it's always discarded. Or split `_fill_missing_chroma`
from the tuple-emission so the image update happens without the allocation.

---

### OP2-14 (P2) — `_connect_rigctld` `subprocess.Popen` lacks process-group isolation — orphan risk on GUI crash

**Component:** Packaging / process lifecycle
**Files:** [src/open_sstv/ui/main_window.py:1177-1179](../src/open_sstv/ui/main_window.py#L1177-L1179), [src/open_sstv/ui/settings_dialog.py:927-931](../src/open_sstv/ui/settings_dialog.py#L927-L931)

**Description.** `subprocess.Popen(cmd, stdout=..., stderr=...)` — no
`start_new_session=True` / `preexec_fn` / Windows `creationflags`. If the Qt
process is SIGKILL'd or crashes without running `closeEvent`, the child
rigctld process is not reaped and continues to hold the serial port and the
TCP listening socket.

`app.aboutToQuit.connect(window.close)` covers graceful shutdown (including
Ctrl-C via the SIGINT handler in app.py:85). It does not cover SIGKILL,
segfault, OOM-kill, or a Python-level `os._exit`.

**Proposed fix.** `subprocess.Popen(..., start_new_session=True)` on POSIX so
the child can be killed via its process group; register an `atexit` handler as
the last-ditch cleanup. On Windows, `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`.

---

### OP2-15 (P2) — `decode_wav` still applies global-polyfit slant correction to Robot 36 via CLI

**Component:** Decoder correctness / CLI parity
**Files:** [src/open_sstv/core/decoder.py:242-244, 253-255](../src/open_sstv/core/decoder.py#L242-L255), [src/open_sstv/cli/decode.py](../src/open_sstv/cli/decode.py)

**Description.** `_decode_robot36_dispatch` calls `slant_corrected_line_starts`
for both wire formats. In the GUI RX path, `RxWorker._dispatch` specifically
skips this path for Robot 36 (see workers.py:1427-1431) to avoid corrupting
noisy signals with outlier-less polyfit. The CLI `open-sstv-decode` calls
`decode_wav` directly and is therefore exposed to the same failure mode that
the GUI path explicitly avoids.

The CLI docstring at cli/decode.py:15-18 notes colour differences but does
*not* mention the slant-correction divergence.

**Proposed fix.** Either (a) have `decode_wav` opt-out of slant correction for
Robot 36 matching the GUI behaviour, or (b) expose a `--slant-correct` flag on
the CLI so the user chooses and the default is "off for Robot 36".

---

### OP2-16 (P2) — `set_weak_signal` / `set_incremental_decode` rebuild the decoder without clearing scratch buffer

**Component:** RX correctness (mid-session setting change)
**Files:** [src/open_sstv/ui/workers.py:906-941](../src/open_sstv/ui/workers.py#L906-L941), [src/open_sstv/ui/workers.py:1000-1005](../src/open_sstv/ui/workers.py#L1000-L1005)

**Description.** `set_sample_rate` clears `self._scratch` (line 1003-1005)
because the old buffer is at the wrong rate. `set_weak_signal` and
`set_incremental_decode` don't — they only replace `self._decoder`. The next
`_flush` feeds the accumulated pre-change scratch into the new decoder. If the
user flipped the setting mid-VIS-hunt, the new decoder sees stale audio from a
different time window.

**Reasoning.** Documented in the docstrings ("callers should toggle this
between transmissions, not mid-RX"), but the scratch accumulation spans IDLE
hunts as well. A user toggling weak-signal mode while a VIS is arriving would
feed the pre-toggle audio to the new decoder's IDLE state. The rolling 3-s
window inside `Decoder._feed_idle` limits the damage to at most 3 s of
potentially-stale samples.

**Proposed fix.** Add `self._scratch.clear(); self._scratch_samples = 0` to both
setter slots, matching `set_sample_rate`.

---

### OP2-17 (P2) — Settings-dialog `_list_serial_ports` module-level cache is thread-unsafe (currently called only from GUI thread)

**Component:** Concurrency (future-proofing)
**Files:** [src/open_sstv/ui/settings_dialog.py:1285-1313](../src/open_sstv/ui/settings_dialog.py#L1285-L1313)

**Description.** `_ports_cache` and `_ports_cache_time` are module-level
globals. A concurrent call would race on read/write of `_ports_cache_time`,
possibly triggering a double `comports()` call. In current usage this is
GUI-thread only, so the race can't happen.

**Proposed fix.** If this helper gets exported or called from other contexts,
guard with a `threading.Lock` or wrap in an `@lru_cache` with a TTL.

**Severity note.** Not actionable today, flagged as future-proofing.

---

### OP2-18 (P2) — `rigctld_proc` started by SettingsDialog is abandoned on OK if MainWindow's save fails

**Component:** Subprocess lifecycle
**Files:** [src/open_sstv/ui/main_window.py:615-633](../src/open_sstv/ui/main_window.py#L615-L633)

**Description.** On Accept, `MainWindow._open_settings` does:

```python
self._config = dlg.result_config()
if dlg.rigctld_process is not None:
    self._rigctld_proc = dlg.rigctld_process     # adopt
self._apply_config()
try:
    save_config(self._config)
except OSError as exc:
    …status-bar msg…
    return                                         # early return
```

The adoption happens *before* `save_config`, so if the save fails the adopted
process is held by `self._rigctld_proc` — correct, and the next
`_on_rig_disconnect` or `closeEvent` will kill it. No leak. (Verified by
tracing `_kill_rigctld` through the close path.)

**But:** if `save_config` fails *and* the user never touches the radio again,
`_rigctld_proc` lives until app exit. They might assume their OK "didn't take"
and relaunch rigctld externally, resulting in two daemons fighting over the
serial port.

**Proposed fix.** On `OSError` during save, explicitly call `self._kill_rigctld()`
before returning, or surface a more specific message ("Settings not persisted,
rigctld *is* running — disconnect via the radio panel to stop it").

---

### OP2-19 (P2) — `QTimer.singleShot(50, ...)` in `_schedule_rx_resume` keeps `self` alive after close

**Component:** Lifecycle (narrow race window)
**Files:** [src/open_sstv/ui/main_window.py:776-784](../src/open_sstv/ui/main_window.py#L776-L784)

**Description.** On TX completion, `_schedule_rx_resume` fires a 50 ms
singleShot that calls `self._request_rx_gate.emit(False)`. If the user closes
the window during that 50 ms, the callback still fires against a partially
torn-down MainWindow. Similar to OP2-09 but with a much smaller window.

**Proposed fix.** Same as OP2-09: guard with visibility check or use a stoppable
QTimer.

---

## P3 — Low

### OP2-20 (P3) — Lazy imports in hot paths (`open_sstv.core.banner`, `incremental_decoder`)

**Files:** [src/open_sstv/ui/workers.py:521-523](../src/open_sstv/ui/workers.py#L521-L523), [src/open_sstv/core/decoder.py:1225-1227](../src/open_sstv/core/decoder.py#L1225-L1227)

Imports inside `transmit()` (banner) and `_feed_idle()` (incremental_decoder
factory) cost ~microseconds per call after the first import thanks to
`sys.modules` cache. Comment on the decoder.py line says "avoids a circular
dependency at module load time" — genuine reason. On workers.py:521-523 it's a
minor perf paper cut; move to module level if the circular-import concern
doesn't also apply.

---

### OP2-21 (P3) — `ImageEditorDialog` crop spinbox `setRange` uses the pre-shrink image width

**Files:** [src/open_sstv/ui/image_editor.py:314, 319](../src/open_sstv/ui/image_editor.py#L314-L319)

`self._crop_x.setRange(0, max(1, image.width - 1))` uses the incoming `image`
parameter, not the post-shrink `_original_image`. `_auto_fit_crop` at line 470
resets the range correctly before the dialog is shown, so the user never sees
the wrong bound. Purely cosmetic.

---

### OP2-22 (P3) — `_dropped_chunks` counter in `InputStreamWorker._audio_callback` is not thread-safe

**Files:** [src/open_sstv/audio/input_stream.py:307, 325](../src/open_sstv/audio/input_stream.py#L307-L325)

`self._dropped_chunks += 1` runs in the PortAudio real-time thread and is read
from the worker thread. Python's int increment is not atomic under the GIL
(LOAD/BINARY_ADD/STORE). In practice the counter is a best-effort diagnostic
displayed after stop, so the race is benign. For strict correctness, use
`itertools.count()` or a `threading.atomic`-style increment.

---

### OP2-23 (P3) — `template_editor_dialog._seed_xy_from_preset` has a dead `except TypeError` for pre-Pillow-10.1

**Files:** [src/open_sstv/ui/template_editor_dialog.py:377-380](../src/open_sstv/ui/template_editor_dialog.py#L377-L380)

`pyproject.toml` pins `Pillow>=10.1`. The `try/except TypeError` around
`ImageFont.load_default(size=...)` was removed in v0.1.29 for `banner.py`,
`draw_text.py`, and `image_editor.py` but this one site was missed.

---

### OP2-24 (P3) — `_bp_window` / `_median_smooth` cast input to float64 on every call even when already float64

**Files:** [src/open_sstv/core/sync.py:448-450](../src/open_sstv/core/sync.py#L448-L450), [src/open_sstv/core/incremental_decoder.py:148-160](../src/open_sstv/core/incremental_decoder.py#L148-L160)

`np.asarray(x, dtype=np.float64)` silently casts when dtype matches but still
allocates a new array with `asarray`'s view-or-copy semantics depending on
contiguity. Usually a view, so cheap. Flagged for completeness.

---

### OP2-25 (P3) — No retry on `RigctldClient` socket timeout (only on BrokenPipe)

**Files:** [src/open_sstv/radio/rigctld.py:165-178](../src/open_sstv/radio/rigctld.py#L165-L178)

`_send_recv` retries once on `BrokenPipeError` / `ConnectionResetError`, but a
transient `TimeoutError` (2 s default) immediately raises `RigConnectionError`.
A rigctld daemon that's slow for one request then responsive again would show
as a freeze in the UI's rig-status readout. Low-impact since the poll resumes
on the next tick.

---

### OP2-26 (P3) — `_bcd_byte_to_int` in IcomCIVRig has no input validation for high nibble > 9

**Files:** [src/open_sstv/radio/serial_rig.py:380-386](../src/open_sstv/radio/serial_rig.py#L380-L386)

`(b >> 4) * 10 + (b & 0x0F)` works for valid BCD (both nibbles 0–9), producing
0–99. For a malformed response with a nibble of 0xA–0xF, the result can be up
to 165 (0xFF → 15*10 + 15). Downstream `get_strength` then applies further
conversion that may land in a nonsensical dBm range, which the S-meter clamp
at radio_panel.py:192 contains. Still, logging the malformed BCD would help
diagnose broken radios.

---

### OP2-27 (P3) — `freq_to_bcd` doesn't validate upper bound — 10-digit freq silently truncates

**Files:** [src/open_sstv/radio/serial_rig.py:398-410](../src/open_sstv/radio/serial_rig.py#L398-L410)

`_freq_to_bcd(hz)` packs into 5 bytes = 10 BCD digits. Any `hz >= 10**10`
(10 GHz) silently drops the high digits. The rig would tune to whatever
garbage frequency results. Not reachable via the UI (no 10 GHz radios in
scope) but `set_freq(hz)` accepts int so a third-party caller could trip it.

---

### OP2-28 (P3) — `_coerce_mode` in `ImageGalleryWidget` fails silently on Mode enum rename

**Files:** [src/open_sstv/ui/image_gallery.py:180-199](../src/open_sstv/ui/image_gallery.py#L180-L199)

If a future refactor renames an enum value (`"robot_36"` → `"robot36"`), stored
gallery items carry the old string. `Mode(str(mode))` raises ValueError,
`_coerce_mode` returns None, and the click/double-click silently do nothing.
A log warning would help diagnose.

---

### OP2-29 (P3) — `_read_response` for Kenwood/Yaesu can busy-loop briefly if bytes arrive with no `;` terminator

**Files:** [src/open_sstv/radio/serial_rig.py:598-618, 797-816](../src/open_sstv/radio/serial_rig.py#L598-L618)

The outer `while time.monotonic() < deadline:` polls `in_waiting`. If
`in_waiting > 0` but no complete `;`-terminated message is present, the inner
`while b";" in buf:` doesn't execute and control falls back to the top of the
outer loop without a sleep (the `else: time.sleep(0.01)` is only reached when
`avail == 0`). For pathological inputs that stream many bytes with no `;` this
could hit CPU at 100% until the deadline. In practice in_waiting drops to 0
immediately after reading.

**Proposed fix.** Add a small `time.sleep(0.001)` at the bottom of the outer
loop regardless of branch.

---

### OP2-30 (P3) — `load_templates` ignores corrupt entries silently on broad except

**Files:** [src/open_sstv/config/templates.py:194-196](../src/open_sstv/config/templates.py#L194-L196)

Same broad-except pattern as `load_config` (OP2-06). If the user has valid
TOML structure but an overlay with a malformed color tuple, the entire
templates file is thrown away and defaults are used. A per-entry try/except
would preserve what's salvageable.

---

### OP2-31 (P3) — `DEFAULT_PTT_DELAY_S` is hardcoded as default arg on TxWorker — loaded config overrides it on init, but value is duplicated in two places

**Files:** [src/open_sstv/ui/workers.py:116](../src/open_sstv/ui/workers.py#L116), [src/open_sstv/config/schema.py:56](../src/open_sstv/config/schema.py#L56)

Both set 0.2s. Not a bug but a duplication waiting to drift. Move to a single
source (e.g., use `schema.AppConfig().ptt_delay_s` as the TxWorker default).

---

### OP2-32 (P3) — `FirstLaunchDialog.callsign()` strips + uppercases, but dialog's live `textChanged` uppercases differently

**Files:** [src/open_sstv/ui/first_launch_dialog.py:84-101, 112](../src/open_sstv/ui/first_launch_dialog.py#L84-L112)

`_on_text_changed` only rewrites when `upper != text`, preserving cursor
position. `callsign()` does `.text().strip().upper()`. If the user types
surrounding whitespace (unlikely), it survives the live-edit and gets stripped
at submit. No visible issue.

---

### OP2-33 (P3) — `pre_vis = joined[:vis_end]` in `_feed_idle` can feed very short audio to incremental decoder's first `feed`

**Files:** [src/open_sstv/core/decoder.py:1238-1241](../src/open_sstv/core/decoder.py#L1238-L1241)

If VIS lands early in a flush (rare), `pre_vis` could be smaller than
`_MIN_BP_SAMPLES` and the first sync search would bail. On the next feed the
accumulating buffer recovers. OK as long as subsequent flushes continue.

---

### OP2-34 (P3) — `sd.stop()` in `output_stream.stop` is global to all sounddevice streams

**Files:** [src/open_sstv/audio/output_stream.py:147](../src/open_sstv/audio/output_stream.py#L147)

`sd.stop()` stops all `sd.play/rec/playrec` calls. Our RX uses `sd.InputStream`
(the class API) which is documented as independent. The mixing is safe today
but fragile against a future refactor that uses `sd.rec()` for RX.

---

### OP2-35 (P3) — `pasokon_p7 VIS_CODE` off-by-one constant duplication

**Files:** [src/open_sstv/core/encoder.py:213, 222](../src/open_sstv/core/encoder.py#L213-L222), [src/open_sstv/core/modes.py:546](../src/open_sstv/core/modes.py#L546)

PySSTV ships P7 as 0xF3 (243); decoder uses 0x73 (115, the lower 7 bits).
Encoder relies on PySSTV's internal 7-bit transmission. Consistent today but
the dual-code footprint is worth documenting in a comment next to the table
entry.

---

### OP2-36 (P3) — No explicit guard on `FirstLaunchDialog` being shown while another modal is active

**Files:** [src/open_sstv/ui/main_window.py:479-480](../src/open_sstv/ui/main_window.py#L479-L480)

`QTimer.singleShot(0, self._show_first_launch_dialog)` fires on the next event
tick. If some other path somehow opens a dialog in that window (e.g., test
harness), two modals stack. Extremely unlikely in production.

---

### OP2-37 (P3) — CI workflow drops macOS Intel — Universal2 build not wired

**Files:** [.github/workflows/build.yml](../.github/workflows/build.yml), [CHANGELOG.md v0.2.3](../CHANGELOG.md)

Documented in CHANGELOG. Intel Mac users fall back to `pipx install open-sstv`.
Not a stability issue — flag for release-ops completeness.

---

### OP2-38 (P3) — `open-sstv.spec` excludes `tkinter` but PyInstaller may still pull it via conditional imports

**Files:** [open_sstv.spec:78-89](../open_sstv.spec#L78-L89)

The `excludes=[...]` list contains `tkinter` and `_tkinter`. Pillow imports
tkinter only inside `ImageTk`, which we don't use — so the exclusion is
correct. No bug; flagged to document the rationale.

---

### OP2-39 (P3) — `_SEPARATOR_COLLAPSE_RE` treats `_` and `-` interchangeably which can surprise users

**Files:** [src/open_sstv/templates/filename.py:48, 72](../src/open_sstv/templates/filename.py#L48-L72)

`r"[_\-]{2,}"` collapses runs of 2+ mixed underscores/dashes to the first
character. A user template `"my-template__v2"` resolves to `"my-template_v2"`
(fine). A template with `"my-template___notes"` resolves to `"my-template_notes"`.
Intentional per the docstring but can surprise — flag for docs.

---

### OP2-40 (P3) — `SerialPttRig.ping()` doesn't actually verify hardware presence

**Files:** [src/open_sstv/radio/serial_rig.py:142-145](../src/open_sstv/radio/serial_rig.py#L142-L145)

PTT-only protocol has no read surface, so `ping` just verifies the serial
port is open. A wrong port selection (serial cable to a non-rig device)
succeeds at connect and fails only at first PTT key. Not fixable without a
protocol — documented behaviour.

---

## Observations / Non-findings

These items were examined in the audit scopes and determined **not** to be
issues, but are recorded for transparency:

- **Decoder cancel event**: `RxWorker.reset()` correctly clears
  `_cancel_event` before re-arming, so a cancelled decode doesn't poison the
  next one.
- **Slant correction skip for Robot 36 in `_dispatch`**: correctly documented
  and implemented at workers.py:1427-1431.
- **`_schedule_rx_resume` 50 ms gate-off delay**: correctly drops trailing
  TX-period audio before the decoder resumes.
- **Rig-poll gating during TX (OP-47)**: correctly saved/restored via
  `_rig_poll_was_active` and gated by `isinstance(self._rig, ManualRig)`.
- **Incremental decoder byte-identical contract**: the `FILTER_MARGIN = 4096`
  padding is massive overkill relative to the sosfiltfilt Butterworth
  impulse-response decay (~200 samples); bit-identical output is preserved.
- **Thread-affinity for QTimer objects**: Every QTimer is lazily created from
  its owning worker's slot, correctly inheriting the worker thread's affinity
  (pattern documented in `_ensure_watchdog_timer`).
- **TX watchdog per-transmission budget**: `_compute_playback_watchdog_s`
  correctly scales with sample count + PTT delay + CW tail; floor/margin
  values are reasonable.
- **`ImageComplete` contract enforcement**: OP-22's defensive `assert
  complete_count <= 1` is in place at workers.py:1192-1197.
- **PD display height doubling**: `ModeSpec.display_height` correctly doubles
  for PD modes based on `color_layout` length.
- **Gallery temp-dir cleanup via `aboutToQuit`**: correctly scoped to Qt app
  lifetime (OP-21).
- **All 22 mode→decoder / mode→encoder completeness asserts** at import time.
- **CW generator WARNING on unsupported characters** (OP-15): logged.
- **VIS 0x00 false-positive handling**: decoder silently drops past vis_end
  and stays in IDLE (D-1).
- **Audio device hotplug**: watchdog at 3 s steady-state with 6 s cold-start
  grace (OP-11) catches lost devices correctly.

---

## Test suite notes

- **649 passed, 1 skipped, 316 s** — clean baseline on v0.2.9 HEAD.
- The single skip is a defensive skip in
  `test_incremental_decoder.py::test_bp_window_called_with_bounded_sample_sum`
  (`_bp_window never called — all chunks below _MIN_BP_SAMPLES threshold`) —
  a conditional skip pattern, not a disabled test.
- Suite execution exercises all 22 modes end-to-end, Robot 36 both wire
  formats, rigctld via a `FakeRigctld` stub, serial rigs via `serial.Serial`
  mock, and the full RX worker watchdog lifecycle.
- No xfails, no known-flaky markers.
- Qt teardown warnings (`qt.qpa.clipboard: Cannot keep promise…`) at the end
  are benign — they come from clipboard-pixmap test fixtures and have been
  present for several releases.

---

## Recommended triage order

1. **OP2-01** (P1) — close the banner-ValueError silent-failure path before
   wider beta.
2. **OP2-02** (P1) — offload rig connect to a worker thread.
3. **OP2-03** (P2) — debounce RX Start button (cheap, visible quality win).
4. **OP2-04** (P2) — replace `id(image)` with a counter (cheap, prevents a
   long-tail user report).
5. **OP2-07** (P2) — atomic config write (one-line change, prevents data
   loss on SIGKILL).
6. **OP2-14** (P2) — `start_new_session=True` on rigctld Popen (one-line, kills
   orphan risk).
7. Everything else can wait for the v0.3 cycle.

No findings indicate a need to delay the beta. The fixes above tighten rough
edges without changing architecture.
