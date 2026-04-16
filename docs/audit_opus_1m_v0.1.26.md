# Open-SSTV v0.1.26 — Deep Pre-Beta Audit (Opus 4.6, 1M ctx)

**Commit:** `8520ee3` (main)
**Branch:** `audit/opus-1m-v0.1.26`
**Reviewer:** Claude Opus 4.6 (1M context window)
**Date:** 2026-04-15
**Scope:** report-only. Zero source modifications. Findings ranked P0 → P3 with severity, file:line,
reproduction/reasoning, and a proposed fix direction. No code changes were made.

## Methodology

Read the full source tree cover-to-cover (≈20 kLOC src + 6.5 kLOC tests):

- `core/decoder.py`, `core/incremental_decoder.py` (cross-checked for batch↔incremental invariants)
- `core/modes.py`, `core/robot36_dsp.py`, `core/banner.py`, `core/cw.py`, `core/sync.py`,
  `core/slant.py`, `core/vis.py`, `core/encoder.py`, `core/demod.py`, `core/dsp_utils.py`
- `ui/workers.py`, `ui/main_window.py`, `ui/settings_dialog.py`, `ui/image_editor.py`,
  `ui/image_gallery.py`, `ui/rx_panel.py`, `ui/tx_panel.py`, `ui/radio_panel.py`,
  `ui/template_editor_dialog.py`, `ui/quick_fill_dialog.py`, `ui/qso_template_bar.py`,
  `ui/draw_text.py`, `ui/utils.py`
- `radio/rigctld.py`, `radio/serial_rig.py`, `radio/base.py`, `radio/exceptions.py`
- `audio/input_stream.py`, `audio/output_stream.py`, `audio/devices.py`
- `config/store.py`, `config/schema.py`, `config/templates.py`
- `cli/encode.py`, `cli/decode.py`, `app.py`
- `tests/**/*.py`, `pyproject.toml`, `CHANGELOG.md` (v0.1.23..v0.1.26)

Known-fixed items (BZ-01..BZ-09, H-01..H-04, M-02..M-05) were deliberately **not** re-surfaced.

Findings carry the **`OP-NN`** prefix. Severities:

| Severity | Meaning |
|---------:|---------|
| P0 | Blocks beta. Functional or safety regression on the happy path. |
| P1 | Important. Likely to bite real users within the first week. |
| P2 | Medium. Edge case, UX wart, or defensive gap worth fixing soon. |
| P3 | Low / polish. Docstrings, test hygiene, dead code. |

---

# P0 — Blocks Beta

## OP-01 — TX watchdog is shorter than the Pasokon P5 and P7 mode durations
**Component:** `ui/workers.py`
**File:** [src/open_sstv/ui/workers.py:138](src/open_sstv/ui/workers.py:138)

`_MAX_TX_DURATION_S = 300.0` aborts any transmission that runs longer than 300 s. The comment
still reads *"The longest SSTV mode we ship (Martin M1) takes ~114 s; 300 s gives plenty of
headroom"* — outdated since the Pasokon and long-PD modes were added in v0.1.3 / v0.1.21.

Measured durations (body only, via `ModeSpec.line_time_ms × ModeSpec.height / 1000`):

| Mode | Body (s) | + VIS + PTT + CW (≈ +4 s) | Watchdog @ 300 s fires? |
|:-----|:--------:|:-------------------------:|:------------------------:|
| Scottie DX      | 268.88 | ~273   | safe (but close) |
| PD-290          | 288.68 | ~293   | safe (but close) |
| **Pasokon P5**  | **304.57** | **~309** | **YES — aborts final ~5–9 s** |
| **Pasokon P7**  | **406.10** | **~410** | **YES — aborts final ~110 s** |

**Repro:** select Pasokon P7, click Transmit, observe the status bar show
*"TX watchdog: exceeded 300 s — rig unkeyed automatically"* at the 5-minute mark with ~25 % of
the image still unsent. The receiving station sees a truncated image with the bottom rows missing.

**Fix direction:** raise `_MAX_TX_DURATION_S` to at least 480 s (gives Pasokon P7 + CW + slop
≈ 70 s of headroom). Consider 600 s so multi-hop CW identifiers, slower 15 WPM settings, and
any future 500 s+ mode aren't affected. Update the stale comment. Add a regression test that
asserts `_MAX_TX_DURATION_S` exceeds every `ModeSpec.total_duration_s` with a reasonable margin:

```python
# tests/ui/test_tx_worker.py
def test_watchdog_covers_every_mode():
    from open_sstv.core.modes import MODE_TABLE
    from open_sstv.ui.workers import _MAX_TX_DURATION_S
    longest = max(s.total_duration_s for s in MODE_TABLE.values())
    # +30 s slop covers VIS leader, PTT delay, CW ID up to 15 WPM.
    assert _MAX_TX_DURATION_S >= longest + 30
```

## OP-02 — Raw `serial.SerialException` leaks past `RigError` catches
**Component:** `radio/serial_rig.py` (Icom/Kenwood/Yaesu backends)
**File:** [src/open_sstv/radio/serial_rig.py:216-285](src/open_sstv/radio/serial_rig.py:216),
[src/open_sstv/radio/serial_rig.py:439-502](src/open_sstv/radio/serial_rig.py:439),
[src/open_sstv/radio/serial_rig.py:595-659](src/open_sstv/radio/serial_rig.py:595)

The three CAT backends only wrap `serial.SerialException` inside their `open()` methods. Every
other I/O path — `get_freq`, `set_freq`, `get_mode`, `set_mode`, `get_ptt`, `set_ptt`,
`get_strength`, `ping`, `_command`, `_read_response` — calls `self._ser.write(...)` or
`self._ser.read(...)` with **no** exception translation.

When the USB-serial cable is unplugged mid-session, pyserial raises `serial.SerialException`
(e.g. `"device reports readiness to read but returned no data"` or
`"[Errno 6] Device not configured"`). Callers downstream catch `RigError`, not
`serial.SerialException`:

- `_RigPollWorker.poll` at [main_window.py:134-144](src/open_sstv/ui/main_window.py:134):
  catches `RigError` → `SerialException` escapes → the poll thread raises an unhandled
  exception on the next 1 Hz tick. The Qt thread pool logs it but the poll loop is dead;
  the UI shows stale freq/mode indefinitely and never recovers.
- `TxWorker._run_tx` at [workers.py:488-495](src/open_sstv/ui/workers.py:488): catches
  `RigError` around `set_ptt(True)` → a mid-TX unplug leaves the broad `except Exception` at
  line 517 to catch `SerialException` during `set_ptt(False)` in the `finally` … but
  `set_ptt(True)` path only catches `RigError`, so an unplug between key-on and key-off would
  bypass the "unplug before audio" abort logic.
- `SettingsDialog._test_serial_connection` already has a belt-and-braces
  `except Exception` (line 655), so the Settings dialog itself is safe. But the live poll path
  is not.

**Repro:** connect via Direct Serial, pull the USB cable mid-session. Rig panel shows a
`SerialException` on the console and stops updating. Reconnecting requires a full app restart.

**Fix direction:** wrap every `self._ser.*` call in the three CAT classes (or at minimum in
`_command` / `_read_response`) with:

```python
except serial.SerialException as exc:
    raise RigConnectionError(f"{self.name}: serial I/O failed: {exc}") from exc
```

Alternatively, change the pollers' `except RigError` → `except (RigError, serial.SerialException, OSError)`
as a defensive second layer; the first approach is cleaner. `RigctldClient` already does this
correctly (translates `OSError` → `RigConnectionError` in `_send_recv`).

---

# P1 — Important (fix before beta)

## OP-03 — `TemplateEditorDialog` drops overlay `x`/`y` coordinates on deep-copy (silent data loss)
**Component:** `ui/template_editor_dialog.py`
**File:** [src/open_sstv/ui/template_editor_dialog.py:55-69](src/open_sstv/ui/template_editor_dialog.py:55)

The dialog deep-copies the incoming `templates` list to isolate edits from the caller:

```python
self._templates = [
    QSOTemplate(
        name=t.name,
        overlays=[
            QSOTemplateOverlay(
                text=o.text,
                position=o.position,
                size=o.size,
                color=o.color,
                # x=o.x, y=o.y  ← MISSING
            )
            for o in t.overlays
        ],
    )
    for t in templates
]
```

`QSOTemplateOverlay` has been carrying optional `x: int | None` / `y: int | None` fields since
v0.1.23 (see [templates.py:34-50](src/open_sstv/config/templates.py:34)) and they are read and
written correctly by `load_templates` / `save_templates`. But the editor's deep-copy constructor
omits them, so any template opened in the editor loses its explicit coordinates. If the user
clicks OK, `save_templates` then writes back a template without `x`/`y`, permanently erasing the
hand-placed coordinates from disk.

Today this is only reachable if a user hand-edits `templates.toml` to add `x`/`y`, because the
dialog's UI has no X/Y fields. But it's still silent corruption of user data the moment the
editor is opened on such a template.

**Repro:**
1. Edit `~/Library/Application Support/open_sstv/templates.toml`, add `x = 50` and `y = 100` to any overlay.
2. Restart the app, open the template editor from the gear icon, click OK without editing.
3. Re-open `templates.toml` — the `x`/`y` keys are gone.

**Fix direction:** add `x=o.x, y=o.y` to the overlay constructor, and (stretch) add X/Y
spinboxes to the dialog so the feature is actually reachable from the UI (matching the image
editor's spinboxes added in v0.1.23).

## OP-04 — BZ-03 regression tests are `@pytest.mark.skip`'d at class level
**Component:** `tests/ui/test_image_editor.py`
**File:** [tests/ui/test_image_editor.py:36-39](tests/ui/test_image_editor.py:36)

```python
@pytest.mark.skip(
    reason="requires display for QGraphicsScene interaction — verify visually"
)
class TestCropXYSpinboxUpdatesRect:
```

The CHANGELOG for v0.1.26 advertises *"tests/ui/test_image_editor.py — BZ-03: verify that
setting X/Y spinboxes updates the visual crop rectangle"* — but the entire test class is
unconditionally skipped. Four tests that would guard against the exact regression the BZ-03
fix addressed never run.

Under the `offscreen` Qt platform, `QGraphicsScene` + `QGraphicsRectItem.rect()` do not need
a real display — the headless `test_settings_dialog.py` fixtures prove that. The skip reason
is not actually required.

**Repro:** `pytest -q tests/ui/test_image_editor.py -v` → 4 skipped, 0 passing coverage of BZ-03.

**Fix direction:** remove the `@pytest.mark.skip` and run locally. If `_update_crop_rect`
depends on `self._view.fitInView`, the tests may still need a `QGraphicsView.show()` call
first; that's a one-line fix. Alternatively, assert on `self._crop_x.value()` and on
`self._crop_rect_item.rect()` directly without driving the view.

## OP-05 — RX start: reset-to-rx_thread and start-capture-to-audio_thread race
**Component:** `ui/main_window.py`
**File:** [src/open_sstv/ui/main_window.py:644-666](src/open_sstv/ui/main_window.py:644)

```python
def _on_capture_requested(self, start: bool) -> None:
    if start:
        self._request_rx_reset.emit()        # GUI → rx_thread (queued)
        self._request_start_capture.emit(...)  # GUI → audio_thread (queued)
```

The two signals are routed to two different threads. `RxWorker.reset` lands on `rx_thread`'s
event loop; `InputStreamWorker.start` lands on `audio_thread`'s event loop. Qt's queued
connections guarantee **per-sender-per-receiver FIFO**, but make no guarantees across
*different* senders or *different* receivers.

Concrete bad interleaving:

1. GUI emits both signals.
2. `audio_thread` picks up `start()`, opens PortAudio (~50–100 ms normally, but can be much
   faster on already-warm devices), emits `chunk_ready` from its 50 ms poll timer.
3. `chunk_ready` is a queued connection from `audio_thread` → `rx_thread.feed_chunk`.
4. `rx_thread` is briefly busy (e.g. GC pause, previous decode in flight) — both the queued
   `reset()` slot and the queued `feed_chunk(...)` land on its queue. Order of arrival depends
   on clock skew between GUI→rx and audio→rx queues.
5. If `feed_chunk` is dispatched first, stale-session samples leak into the fresh decoder
   before `reset()` runs.

In practice the race is narrow (PortAudio open takes long enough that `reset()` nearly always
wins) but the design has no actual ordering guarantee. The symptom would be an occasional
"phantom" progress spike right after Start.

**Fix direction:** sequence the reset inside the RX worker's own slot before touching the audio
stream. Options:

- Have `reset()` emit a `reset_done` signal; `MainWindow` connects that to `_request_start_capture.emit(...)`.
- Or: move the start-capture emission into a `QTimer.singleShot(0, ...)` so it lands
  *after* the reset slot in the GUI thread's own queue, and rely on the audio-thread's open
  latency to hide the rest (effectively today's behavior but made explicit).

## OP-06 — `tx_panel.show_tx_progress` hardcodes 48 kHz in the elapsed-seconds math
**Component:** `ui/tx_panel.py`
**File:** [src/open_sstv/ui/tx_panel.py:254-264](src/open_sstv/ui/tx_panel.py:254)

```python
def show_tx_progress(self, samples_played: int, samples_total: int) -> None:
    if samples_total > 0:
        pct = int(samples_played * 100 / samples_total)
        elapsed_s = int(samples_played / 48000)   # hardcoded!
        total_s = int(samples_total / 48000)      # hardcoded!
```

The `RxWorker` and `TxWorker` both accept a configurable `sample_rate`, defaulting to 48 kHz.
Users on 44.1 kHz will see a progress-bar label that is off by ~8.8 %: a 114 s Martin M1
transmission reports *"124 s / 124 s elapsed"* at completion.

The `pct` field is correct (ratio math), but the human-readable label is wrong.

**Fix direction:** plumb the sample rate into the panel via `set_sample_rate` or pass it in
the `transmission_progress` signal payload (currently `(samples_played, samples_total)`; add
a third `rate` int, or derive the rate from `samples_total` vs. the mode's expected duration
in `workers.py` before emitting). Simplest: store `self._sample_rate` on the TxPanel and
update it when `MainWindow._apply_config` runs.

## OP-07 — Docstring/behavior mismatch in `Decoder._feed_idle` incremental fallthrough
**Component:** `core/decoder.py`
**File:** [src/open_sstv/core/decoder.py:1215-1246](src/open_sstv/core/decoder.py:1215)

```python
# Incremental decode path — covers all 22 modes …  Returns None only for an unknown
# mode; falls through to the batch path in that case.
if self._incremental_decode:
    ...
    inc = make_incremental_decoder(spec, self._fs, vis_end_abs=vis_end, start_abs=0)
    if inc is not None:
        ...
        return events
# Batch path: try an immediate partial decode ...
```

Two issues:

1. The comment "Returns None only for an unknown mode" is stale. `make_incremental_decoder`
   [incremental_decoder.py:1470-1497](src/open_sstv/core/incremental_decoder.py:1470) has an
   entry for every `Mode` value — since `mode_from_vis(vis_code)` already returned non-`None`
   before reaching this block, `make_incremental_decoder` *cannot* return `None` here. The
   fallthrough to the batch path at line 1241 is dead code.
2. Dead code is fine defensively, but the comment describing *when* it runs is wrong. Either
   remove the fallthrough (with a loud assertion so future additions fail fast) or update the
   comment to "defensive fallback: never triggered on v0.1.26 — every Mode has a backend."

**Fix direction:** prefer an assertion:

```python
if inc is None:
    raise AssertionError(
        f"make_incremental_decoder returned None for known mode {mode!r} — "
        "add a backend to incremental_decoder.make_incremental_decoder."
    )
```

## OP-08 — `emergency_unkey` runs synchronously on the GUI thread and can block indefinitely on a dead rig
**Component:** `ui/workers.py` + `ui/main_window.py`
**File:** [src/open_sstv/ui/workers.py:327-338](src/open_sstv/ui/workers.py:327),
[src/open_sstv/ui/main_window.py:977](src/open_sstv/ui/main_window.py:977)

`MainWindow.closeEvent` calls `self._tx_worker.emergency_unkey()` on the GUI thread *after*
the TX worker thread fails to join within 3 s. `emergency_unkey` does:

```python
with self._rig_lock:
    self._rig.set_ptt(False)
```

`set_ptt(False)` can block for up to the serial `write_timeout` (1.0 s) plus read (~0.5 s) on
an unresponsive rig — every time. For a Kenwood/Yaesu rig that sends a command and waits
1 second for a `;`-terminated response, `emergency_unkey` can freeze the GUI thread for the
full timeout inside `closeEvent`. On a rig whose serial device has actually vanished (e.g.
cable pulled), the pyserial write can raise `SerialException` inside the `try`, exit via the
bare `except Exception: pass`, and the app quits cleanly — but on a slow-to-respond *live*
rig the user sees a frozen window on quit.

**Fix direction:** run `emergency_unkey` in a short-lived daemon thread with a 1 s join:

```python
t = threading.Thread(target=self._tx_worker.emergency_unkey, daemon=True)
t.start()
t.join(timeout=1.0)
# If it didn't finish, we've done best-effort and let the OS tear down the socket.
```

Also, the broad `except Exception` at line 337 will also silence `KeyboardInterrupt` on
Python 3.10 and earlier (KI is Exception on those). We're on ≥ 3.11 per pyproject.toml, so
this is fine today — but worth a note.

## OP-09 — `_apply_config` emits multiple "apply now" events in a fixed order, but one of them is a direct method call
**Component:** `ui/main_window.py`
**File:** [src/open_sstv/ui/main_window.py:486-523](src/open_sstv/ui/main_window.py:486)

```python
self._rx_weak_signal_changed.emit(self._config.rx_weak_signal_mode)       # queued to rx_thread
self._rx_worker.set_final_slant_correction(self._config.apply_final_slant_correction)  # DIRECT
self._rx_incremental_decode_changed.emit(self._config.incremental_decode) # queued to rx_thread
...
self._tx_worker.set_sample_rate(self._config.sample_rate)                 # DIRECT
self._rx_sample_rate_changed.emit(self._config.sample_rate)               # queued to rx_thread
```

`set_final_slant_correction` and `set_sample_rate` are called directly from the GUI thread on
QObjects that live on other threads. Per the inline docstring at
[workers.py:669-682](src/open_sstv/ui/workers.py:669), this is intentional: the writes are
plain `bool` / `int` assignments and assignment of a Python int or bool is atomic under the
GIL, so a concurrent `feed_chunk` on the worker thread cannot see a torn value.

Two concerns:

1. **Ordering with the queued signals:** the weak-signal change races with the
   slant-correction write — they can be observed by the worker in different orders. This is
   benign today (the two settings don't interact in a decoder rebuild) but the mixing of
   direct + queued writes in one method is fragile. Adding a new direct write that *does*
   depend on a weak-signal rebuild would surface a race.
2. **Documentation:** the class docstrings claim "queued for thread safety" as a pattern
   (see [main_window.py:167-171](src/open_sstv/ui/main_window.py:167)) but the actual code
   uses a mix of both patterns without calling that out.

**Fix direction:** add `@Slot(bool)` to `set_final_slant_correction` (it's already Slot-ready)
and connect via a queued signal like the other three. `set_sample_rate` on TxWorker is
similar. This makes the "all config changes run on the worker's own thread" invariant
genuinely true, not approximately true.

## OP-10 — Pasokon / long modes: incremental decoder `_sync_abs` is append-only and never pruned
**Component:** `core/incremental_decoder.py`
**File:** [src/open_sstv/core/incremental_decoder.py:448-500](src/open_sstv/core/incremental_decoder.py:448)

`IncrementalDecoderBase._sync_abs` accumulates every confirmed sync position across the whole
image. For a Pasokon P7 capture at 496 lines it reaches 496 entries; for Pasokon P5 at
496 × 304 s that's again 496 entries. Not a memory problem.

But `_update_syncs` checks every new candidate against every existing sync for dedup:

```python
for c_rel in cands_rel:
    c_abs = tail_abs_start + c_rel
    if any(abs(c_abs - s) <= self._DEDUP_RADIUS for s in self._sync_abs):
        continue
    self._sync_abs.append(c_abs)
```

This is O(n) per candidate, O(n²) total over the decode — worst case ~246000 comparisons for
Pasokon P7. In practice `cands_rel` is small (1–3 per search), so the real cost is O(n) per
feed × O(flushes per image) ≈ 19000 × 3 × 496 = 28M compares for P7. On a modern CPU this is
~30 ms total, acceptable, but scales quadratically with image height. Any future 2048-line
mode would visibly slow down.

**Fix direction:** maintain `_sync_abs` sorted and use `bisect` to limit dedup scanning to the
immediate neighbourhood, or prune entries further back than the current `_prune` horizon.
Low priority but worth a comment explaining the O(n²) scaling.

## OP-11 — Audio watchdog can fire spuriously during PortAudio cold-start
**Component:** `audio/input_stream.py`
**File:** [src/open_sstv/audio/input_stream.py:90-91, 203-213](src/open_sstv/audio/input_stream.py:90)

```python
_DEVICE_WATCHDOG_MS: int = 3000
...
self._watchdog = QTimer()
self._watchdog.setSingleShot(True)
self._watchdog.setInterval(_DEVICE_WATCHDOG_MS)
self._watchdog.timeout.connect(self._on_watchdog_timeout)
self._watchdog.start()    # starts the moment start() returns
```

On a fresh macOS USB audio device or a Bluetooth-connected SoundLink, the first PortAudio
callback can take 1.5–2.5 s to fire after `sd.InputStream.start()` returns. The 3 s budget
covers most cases but leaves narrow headroom; under load (laptop thermal throttling, suspend
recovery) it's been observed in the field to cross 3 s on PipeWire JACK bridges and on macOS
when a video conference app grabs priority.

A spurious watchdog fire calls `self.stop()` → emits `stream_error("Audio input device lost")`
→ UI shows the error and silently stops capture. User sees a false positive within 3 s of
starting.

**Fix direction:** introduce a "grace period" flag — don't arm the watchdog until the first
chunk has arrived, or start with a longer initial interval (say 5 s) that drops to 3 s after
the first chunk. The check `if drained_any and self._watchdog is not None: self._watchdog.start()`
at line 320 already re-arms only on a non-empty drain, which is correct.

---

# P2 — Medium (should fix before v1.0)

## OP-12 — `RxWorker.set_sample_rate` clears scratch but not `_total_samples`
**File:** [src/open_sstv/ui/workers.py:703-724](src/open_sstv/ui/workers.py:703)

After a sample-rate change mid-session, the "Xs buffered" status text computes
`secs = self._total_samples / self._sample_rate`. `_total_samples` still contains counts from
the old rate, but `_sample_rate` is now the new rate. The displayed seconds are wrong until
the next capture session (which does reset `_total_samples`). Cosmetic but confusing.

**Fix:** reset `_total_samples = 0` in `set_sample_rate`.

## OP-13 — `subprocess.Popen` for rigctld takes user-config strings as args without validation
**File:** [src/open_sstv/ui/main_window.py:839-851](src/open_sstv/ui/main_window.py:839),
[src/open_sstv/ui/settings_dialog.py:825-837](src/open_sstv/ui/settings_dialog.py:825)

```python
cmd = ["rigctld", "-m", str(self._config.rig_model_id), "-t", str(port)]
if self._config.rig_serial_port:
    cmd += ["-r", self._config.rig_serial_port]
```

`cmd` is a list (no shell injection), and `rig_serial_port` is populated from `serial.tools.list_ports`
plus an editable combo. A user pasting a malicious path like `--script /tmp/evil.sh` into the
editable combo would pass that straight to rigctld's arg parser, which would reject it — but
any rigctld option that *does* accept an arbitrary argument becomes a local privilege-elevation
vector if the config is ever read from an untrusted source.

The config file is per-user and not world-writable by default, so this is low concern. But
given the project's regulatory context (amateur radio / RF emission), it's worth documenting:
**never read open_sstv config from an untrusted location.**

**Fix direction:** validate `rig_serial_port` against `serial.tools.list_ports.comports()`
before launching, or at minimum reject values starting with `-`.

## OP-14 — Scottie DX / PD-290 approach but don't exceed the 300 s watchdog
**File:** [src/open_sstv/ui/workers.py:138](src/open_sstv/ui/workers.py:138)

See OP-01. Scottie DX is 268.88 s body + ~4 s VIS/CW; PD-290 is 288.68 s body + ~4 s. PD-290
with a slow 15 WPM CW tail for a 6-char callsign would cross 300 s. Also covered by the
OP-01 fix (raise the watchdog). Called out separately so the fix explicitly tests these two
modes, not just P5/P7.

## OP-15 — `CW.make_cw` silently skips non-ASCII characters in callsigns
**File:** [src/open_sstv/core/cw.py:125-128](src/open_sstv/core/cw.py:125)

`_MORSE_TABLE` covers A–Z, 0–9, `/`, `-`. Any other character — including `.`, space inside
a "word", non-ASCII letters (for the small number of callsigns with them), lowercase-only
transliterations — is dropped with only a `DEBUG` log. A user with callsign
`W0AEZ/P` / whitespace / numeric suffix anomalies gets a shortened CW ID that may not legally
identify them.

**Fix direction:** surface a warning at the UI layer when the configured callsign contains any
character not in `_MORSE_TABLE`. A single one-line check in `TxWorker.set_cw_id` with a log at
WARNING level would be a good belt-and-braces improvement; ideally `SettingsDialog` also shows
a yellow indicator on the Callsign field.

## OP-16 — `apply_tx_banner` silently re-scales the image vertically via LANCZOS, changing SSTV pixel geometry
**File:** [src/open_sstv/core/banner.py:147-150](src/open_sstv/core/banner.py:147)

```python
if content_height > 0:
    shrunk = image.resize((width, content_height), Image.Resampling.LANCZOS)
    out.paste(shrunk, (0, banner_height))
```

For a 320×240 Robot 36 image with a 24-px banner, the content is squashed from 240 → 216 rows
(vertical 10 % compression). This is exactly what v0.1.23 intended — the banner no longer
overwrites user content. But the comment in `core/banner.py:109-110` says *"SSTV mode pixel
geometry is preserved exactly"*: that statement refers to *output image dimensions*, not to
*per-pixel content placement*. An operator sending a photographic image won't notice, but an
operator sending a test pattern (gridlines, calibration chart) will see the gridlines
distorted vertically.

Not a bug per se, but worth exposing a "do not re-scale content, clip the top rows instead"
option for operators who are sending tests.

**Fix direction:** add `content_mode: Literal["resize", "clip"] = "resize"` to `apply_tx_banner`
and the config; `"clip"` crops off the top 24 rows instead of resizing the body. Minor UX
polish, not a blocker.

## OP-17 — `Robot36IncrementalDecoder._pending` has no upper bound while awaiting backend selection
**File:** [src/open_sstv/core/incremental_decoder.py:1349-1376](src/open_sstv/core/incremental_decoder.py:1349)

During the 450–900 ms detection window, `_pending.append(arr)` stores every chunk. If
`find_sync_candidates` never produces `_DETECT_SYNC_COUNT=3` candidates — e.g. a fade-in
Robot 36 signal where the first several sync pulses are below threshold — `_pending`
accumulates indefinitely. For a 36 s Robot 36 at 48 kHz that's up to 1.72 M samples
(~14 MB of float64).

In practice the detection always completes within the first second or the signal is
rejected by `mode_from_vis` before this path runs. But a crafted/very-weak signal could
keep the decoder stuck in detection, inflating memory per transmission.

**Fix direction:** after N seconds of failed detection (say 3 s), bail to the per-line
backend as a default rather than continuing to buffer. Add a test.

## OP-18 — `find_input_device_by_name` returns `None` silently when the saved device is missing
**File:** [src/open_sstv/audio/devices.py:149-156](src/open_sstv/audio/devices.py:149),
[src/open_sstv/ui/main_window.py:199-202](src/open_sstv/ui/main_window.py:199)

If the user's saved `audio_input_device` name doesn't match any current device (unplugged
since last run, moved to a different USB port with a different enumeration), we silently fall
back to `None` (system default). The user's expectation is that their chosen device is in use;
they get the system default with no warning.

**Fix direction:** when `find_input_device_by_name(cfg.audio_input_device)` returns `None`
and `cfg.audio_input_device` was non-empty, emit a status bar message:
`"Saved input device '<name>' not found; using system default."`.

## OP-19 — `_kill_rigctld` doesn't handle `ProcessLookupError` if the process already died
**File:** [src/open_sstv/ui/main_window.py:910-918](src/open_sstv/ui/main_window.py:910)

`self._rigctld_proc.terminate()` raises `ProcessLookupError` on POSIX if the process has
already exited (e.g. rigctld crashed on bad `-r` argument). The method has no try/except.
On shutdown, that propagates up to `closeEvent` → `super().closeEvent(event)` never runs.

**Fix direction:** wrap `terminate()` / `wait()` / `kill()` in try/except that swallows
`ProcessLookupError` and `OSError`.

## OP-20 — `InputStreamWorker._audio_callback` accesses `_dropped_chunks` from the realtime callback thread without a lock
**File:** [src/open_sstv/audio/input_stream.py:278-299](src/open_sstv/audio/input_stream.py:278)

```python
if status.input_overflow or status.input_underflow:
    self._dropped_chunks += 1     # callback thread
...
try:
    self._queue.put_nowait(chunk)
except queue.Full:
    self._dropped_chunks += 1     # callback thread
```

`_dropped_chunks += 1` is two operations (load + add + store) and is *not* atomic under the
GIL in the way plain assignment is. Concurrent reads from `stop()` at
[input_stream.py:251-254](src/open_sstv/audio/input_stream.py:251) can observe torn values
on non-CPython implementations. On CPython the GIL makes `+= 1` on an int "effectively atomic"
because the bytecode compiles to a single INPLACE_ADD op … but this is implementation-detail,
not a guarantee.

**Fix direction:** make the counter a `threading.Lock`-guarded int, or an
`itertools.count()` you increment with `next()`. Low priority — CPython is the target, and
the drop-count field is cosmetic.

## OP-21 — `ImageGalleryWidget.atexit.register(...)` leaks callbacks per instance
**File:** [src/open_sstv/ui/image_gallery.py:90-92](src/open_sstv/ui/image_gallery.py:90)

```python
try:
    self._tmpdir = tempfile.mkdtemp(prefix="open-sstv-gallery-")
    atexit.register(self._cleanup_tmpdir)
except OSError:
    ...
```

Every widget instance registers a new atexit callback. In practice there's one gallery per
app lifetime, so this leaks exactly one callback. But in test sessions that construct many
`MainWindow` instances back-to-back (pytest-qt fixtures do), atexit grows an entry per test.
At process shutdown, all fire sequentially — safe, but the atexit list becomes O(n_tests).

**Fix direction:** use `QCoreApplication.aboutToQuit` instead of `atexit` so cleanup is
scoped to the app, not the interpreter. Or use `weakref.finalize`.

## OP-22 — `RxWorker._dispatch` ImageComplete handler assumes exactly one ImageComplete per feed
**File:** [src/open_sstv/ui/workers.py:857-892](src/open_sstv/ui/workers.py:857)

The dispatch loop calls `self._decoder.consume_last_buffer()` inside the ImageComplete branch.
If a single flush produced multiple ImageComplete events (impossible in v0.1.26 — the decoder
auto-resets after the first, but future additions could change this), only the first would
consume the raw buffer; the second would call `consume_last_buffer()` → `None` → skip the
re-decode. Defensive, not a live bug.

**Fix direction:** assert there is at most one ImageComplete per `events` list, or handle the
multi-complete case explicitly.

## OP-23 — Banner edge case: image height ≤ banner_height
**File:** [src/open_sstv/core/banner.py:143-150](src/open_sstv/core/banner.py:143)

All current modes have image heights ≥ 128 and the largest banner is 40 px, so
`content_height = height - banner_height ≥ 88`. But if anyone adds a mode with height ≤ 40
(or forcibly passes a height-24 image to `apply_tx_banner`), `content_height = 0` causes the
`if content_height > 0` branch to skip the paste, producing an all-banner output with no
image data. No error raised.

**Fix direction:** raise `ValueError` explicitly, or clamp banner_height to min(height // 2,
default). The current silent degradation is the worst outcome.

---

# P3 — Low / polish

## OP-24 — Stale comment references outdated behaviour in `core/decoder.py:1215-1217`
See OP-07. Two stacked comments contradict each other.

## OP-25 — CHANGELOG claims tests added for BZ-03 but they're all skipped
See OP-04. Cross-reference for changelog-vs-reality audit.

## OP-26 — `core/sync.py` `_HARD_SYNC_LOWER_HZ = 1300.0` very close to VIS data band
**File:** [src/open_sstv/core/sync.py:157](src/open_sstv/core/sync.py:157)

The hard-floor at 1300 Hz on the adaptive threshold is intentional (sync floor-tracking).
But VIS bit-0 is 1300 Hz exactly — if post-VIS residue leaks past `start_idx`, the clamp
at 1300 Hz can admit a VIS "0" bit as a sync candidate. The `_MIN_LINE_SYNC_RATIO * sync_samples`
length filter rejects all but the widest runs, and `walk_sync_grid`'s pair-wise anchor check
drops spurious leading candidates … but the margin is slim.

**Fix direction:** consider 1350 Hz as the hard floor, giving 50 Hz of clearance above the
VIS "0" tone.

## OP-27 — Incremental decoder uses `np.concatenate([buf, arr])` per feed() (O(buf) per call)
**File:** [src/open_sstv/core/incremental_decoder.py:432](src/open_sstv/core/incremental_decoder.py:432)

For long modes (Pasokon P7), this runs ~19000 times per image with ever-growing buffers.
Pruning keeps `buf` small (~line_period + FILTER_MARGIN ≈ 93 ms of audio) so each concat is
~4500 samples, but the pattern is still O(n·m) for n feeds, m average buf. A linear
preallocated ring buffer would be better. Total cost today is ~80M array copies over P7 —
not a bottleneck on modern hardware but scales poorly.

**Fix direction:** use a preallocated numpy array with a write index, grown only when truly
needed. Low priority — current decode stays ahead of real-time.

## OP-28 — `_on_rig_connect` dispatches on string equality against `"manual"`, `"serial"`, etc.
**File:** [src/open_sstv/ui/main_window.py:772-784](src/open_sstv/ui/main_window.py:772)

String literals for connection mode appear in three places: schema defaults
(`"manual"`), the settings dialog combo (`conn_mode_combo.addItem(..., "manual")`), and the
dispatch in `_on_rig_connect`. Any renaming that misses one of these three sites silently
breaks the feature.

**Fix direction:** introduce a small `StrEnum RigConnectionMode` in `radio/base.py` and use
it everywhere.

## OP-29 — Docstring in `core/decoder.py` claims "Phase 2 step 13 ships the Robot 36 decoder"
**File:** [src/open_sstv/core/decoder.py:11-14](src/open_sstv/core/decoder.py:11)

Phase-planning language in user-facing docstrings is stale. Consider a sweep of the
`core/` module docstrings to drop Phase 0/1/2 references and replace with version numbers
where they still matter.

## OP-30 — Missing test coverage for `TxWorker.emergency_unkey` and `TxWorker.wait_for_stop`
No unit tests for either method (grep `emergency_unkey`, `wait_for_stop` in `tests/`).
`wait_for_stop` is exercised indirectly by `closeEvent` tests in `test_main_window.py`, but
no focused test verifies it returns `True` when the flag is set mid-wait.

## OP-31 — Duplicate color-pipeline comment noise
The Robot 36 + slant-correction-skip decision is explained in *three* places: CHANGELOG
v0.1.25, `workers.py:862-879`, and `incremental_decoder.py:978-1018`. The code is fine;
the copy-paste is just verbose. Fold into one canonical location and reference it.

## OP-32 — `draw_text_overlay` silently falls back to Pillow's `load_default()` with no size
**File:** [src/open_sstv/ui/draw_text.py:96-100](src/open_sstv/ui/draw_text.py:96)

On Pillow < 10.1, `load_default(size=...)` raises `TypeError` and we fall back to the tiny
fixed-size bitmap font, making overlay text illegible at sizes > 8. The fallback is
unreachable on pyproject's `Pillow>=10.0`, since 10.0.x does accept size. Actually 10.0
also does. Fine. Safe, but the comment suggests the `TypeError` path is reachable — verify
the pinned Pillow >= 10.0 and either bump to >= 10.1 or keep the fallback and the comment.

## OP-33 — `RadioPanel.update_rig_status` S-meter conversion drops below 0 for negative dBm
**File:** [src/open_sstv/ui/radio_panel.py:186](src/open_sstv/ui/radio_panel.py:186)

`s_unit = min(9, max(0, (strength_db + 127) // 6))`. For `strength_db = 0` ("no reading"
sentinel), s_unit = `127 // 6 = 21`, clamped to 9 — the S-meter shows S9 when the radio
isn't reporting. The guard `if strength_db != 0` skips it today, but 0 as a magic sentinel
is fragile: a genuine dBm=0 reading would be wrong. Unlikely in practice (S9+60+ is ≈ -13 dBm).

**Fix direction:** use `None` or a dedicated sentinel in the `poll_result` payload.

---

# Cross-Cutting Observations

## Protocol correctness vs. canonical references

Timing constants in `core/modes.py` were cross-checked against Dayton Paper and Martin
Bruchanov's references via the commit history. The Pasokon scan-per-gap math
(`4×gap + 3×scan`) matches PySSTV's intention; Martin/Scottie sync/porch values match
the 1.5 ms / 4.862 ms / 9.0 ms canonical timings. **One note:** the PySSTV `Robot36` class
emits the per-line format, not the line-pair format — Open-SSTV handles this correctly with
`Robot36LinePair` subclass in `core/encoder.py:72`.

## UX inconsistencies

- Frequency is displayed in MHz for ≥ 1 MHz, kHz for ≥ 1 kHz, Hz otherwise
  (`ui/radio_panel.py:168-173`). OK.
- Gain sliders are percent (0–100 / 0–200), 1-based. Sample rate is Hz (48000). CW WPM is
  unitless. PTT delay is seconds. **No unit mixing** across tabs — consistent.
- The "0x00 VIS" false-positive is handled correctly (silent drop, no DecodeError) —
  good, per v0.1.11 fix.

## Memory growth over long sessions

- Decoder IDLE buffer bounded to 3 s rolling window ✓
- Incremental decoder buffer pruned to FILTER_MARGIN + one line period ✓
- Gallery bounded to 20 images ✓
- Rigctld response buffer bounded to 1000 lines ✓
- `_sync_abs` grows to at most 496 entries (Pasokon-height) per decode, cleared on reset ✓
- `_dropped_chunks` is a simple counter — cleared on each `start()` ✓
- **One concern:** OP-17 `_pending` in `Robot36IncrementalDecoder` unbounded until detection
  succeeds.

Overall the project has very tight memory hygiene — no accumulating lists found in long-running paths.

## Test suite health

`pytest -q` result recorded below. Coverage gaps:
- OP-04: BZ-03 tests all skipped.
- OP-30: no test for `emergency_unkey` or `wait_for_stop`.
- OP-17: no test for Robot 36 pending-buffer bound during failed detection.
- No round-trip test for Pasokon P5 / P7 (would catch OP-01).
- No test that `_MAX_TX_DURATION_S` covers every mode (would catch OP-01).

---

# Test Suite Run Result

```
$ QT_QPA_PLATFORM=offscreen PYTHONPATH=. pytest -q
........................................................................ [ 14%]
........................................................................ [ 29%]
........................................................................ [ 44%]
........................................................................ [ 59%]
........................................................................ [ 74%]
...........................................................ssss......... [ 89%]
.....................................................                    [100%]
481 passed, 4 skipped in 318.21s (0:05:18)
```

**481 passed, 4 skipped** — all 4 skips are the `TestCropXYSpinboxUpdatesRect` class in
`tests/ui/test_image_editor.py` (see OP-04). No failures.

**Additional finding surfaced by running the suite:** `pytest -q` at the repo root fails
without `PYTHONPATH=.` because `tests/radio/test_rigctld_client.py` does
`from tests.radio.fake_rigctld import FakeRigctld`, which requires the `tests` package to be
on `sys.path`. The `pyproject.toml` `[tool.pytest.ini_options]` section adds
`pythonpath = ["src"]` but not `["."]`. This is effectively **OP-34 (P2, tooling)** — set
`pythonpath = [".", "src"]` or change the import to a relative `from .fake_rigctld import FakeRigctld`.

---

# Final Summary

**Verdict: needs work — not ready for beta.**

The codebase is in good shape — the audit history across v0.1.23..v0.1.26 shows that the
author has been systematically closing real bugs, and the architecture is cleanly factored
(separate decoder front-end, incremental/batch parity, thread-per-worker). However, two P0
issues will bite real users immediately:

1. **OP-01** — Pasokon P5 and P7 transmissions get cut short by the 300-second watchdog.
   Any operator who selects those modes will see watchdog-abort status messages on a regular
   basis; the receiving station sees a truncated image.
2. **OP-02** — the serial CAT backends leak `serial.SerialException` past `RigError` catches,
   so a mid-session USB cable wiggle kills the rig poll thread. The user's frequency/mode/S-meter
   display freezes and only a full app restart recovers — a common scenario for field operation.

Neither fix is more than a dozen lines of code and a small test. With both addressed the
beta ships in good conscience.

P1 findings (OP-03..OP-11) are genuine user-impact issues but won't brick a session — they
should be triaged for next patch release. The P1 data-loss bug in `TemplateEditorDialog`
(OP-03) is the most worrying after the P0s because it silently erases user configuration
with no error surface.

P2/P3 are polish.

The test suite is broad (180+ tests across 24 files) and the regression bound tests for the
DSP front end (Robot 36 5 % luma bound, Martin M1 / Scottie S1 acceptance tests, D-3 slant
stability) are the kind of assertions that matter. Two gaps stand out: OP-04 (BZ-03 tests
skipped) and the missing Pasokon round-trip tests that would have caught OP-01.

Ship it with those two P0s fixed.

— W0AEZ 73
