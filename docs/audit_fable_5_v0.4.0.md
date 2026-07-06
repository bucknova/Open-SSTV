# Full-project stability audit — v0.4.0 (`be7a6d1`)

Audited 2026-06-12, immediately post-v0.4.0 release.  Method: four
subsystem auditors (threading/lifecycle, radio backends, DSP core,
persistence/secondary UI) with a stability-first lens, findings
capped and adversarially screened; three findings empirically
reproduced by the DSP auditor, one verified against the installed
websocket-client library, one cross-confirmed independently by two
auditors, and seven load-bearing claims re-verified by hand against
the source (7/7 upheld).  Known deferrals in `docs/future_work.md`,
`docs/v0.4-plan.md`, and the v0.4 logbook audit were excluded.

**Status: FINDINGS RECORDED, FIXES PENDING** — intended vehicle is a
v0.4.1 stability patch PR.

Severity: 🔴 high (PTT safety / process death / data loss) ·
🟡 medium · 🔵 low.

---

## 🔴 1. Mid-TX disconnect teardown races the unkey path — radio left keyed

`ui/main_window.py:2911-2913` + `ui/workers.py:1247-1314`.
**Cross-confirmed by two independent auditors.**  A transient CAT
failure mid-TX makes the health monitor emit `rig_disconnected`; the
GUI's `_on_radio_disconnected` then runs `_kill_rigctld()` and
`old_rig.close()` while the TX worker is still inside
`_unkey_with_retry` on the *same* backend.  With app-launched
rigctld the daemon is dead before unkey attempt 1 (all three
reconnects refused); with serial, the GUI `close()` can land between
the retry loop's `open()` and `set_ptt(False)`.  Result: transmitter
left on-air after a recoverable glitch — the worst failure mode a
ham app has.  Per-backend locks serialize single commands, not the
close-vs-reopen sequence.

**Fix direction:** the GUI disconnect handler must not tear down the
backend while TX is unwinding — defer `old_rig.close()` /
`_kill_rigctld()` until `transmission_aborted`/`transmission_complete`
arrives (or gate on a "TX unwinding" flag the worker owns), and let
the worker's unkey own the rig for that window.

## 🔴 2. Auto-launched rigctld wedges once its stderr pipe fills

`ui/main_window.py:2570`.  `subprocess.Popen(..., stderr=PIPE)` and
the pipe is never read or closed.  A flaky USB-CAT cable makes
hamlib log per-transaction errors; at ~64 KB of accumulated stderr
the daemon blocks on `write(2)` and stops servicing its TCP socket
entirely — every command times out, involuntary disconnect, and in
the worst case it wedges while PTT is keyed and all unkey retries
time out against the wedged daemon.  **Verified by hand.**

**Fix direction:** `stderr=subprocess.DEVNULL` (simplest, loses
diagnostics) or a small daemon reader thread that drains into the
app log (better — hamlib stderr is exactly what you want in
diagnostics zips).

## 🔴 3. NaN samples permanently wedge the streaming decoder

`core/incremental_decoder.py:234` (same pattern
`core/decoder.py:817`, `core/robot36_dsp.py:102`).
**Empirically reproduced.**  `int(round(...))` on a median/mean of
the IF track with no `isfinite` guard: NaN passes the range checks
(NaN comparisons are False) and raises
`ValueError: cannot convert float NaN to integer`.  The offending
line window is never consumed nor pruned, so *every* subsequent
`feed()` — including clean audio — re-raises identically.  In the
GUI the RX worker survives but RX is dead ("Decoder exception"
spam) until watchdog/manual reset; headless/library callers crash.
Glitchy drivers and virtual audio cables do produce NaN buffers.

**Fix direction:** `np.nan_to_num` (or an `isfinite` mask) at the
`feed()` boundary once, plus a guard in the three samplers; ensure a
sampler exception consumes/prunes the window so the decoder can
never wedge on one bad line.

## 🔴 4. Wedged worker at quit aborts the process (qFatal)

`ui/main_window.py:3142/3178`.  `closeEvent` discards every
`QThread.wait()` timeout result: TX gets `wait(3000)` (logged,
then proceeds), audio/RX/poll/update get `wait(4000)` in a loop with
the return value ignored.  All five are `QThread(self)` children of
MainWindow, so a wedge (unkey retries needing ~9 s, the documented
macOS Core Audio stop() hang, a long P7 slant-correction decode)
means `~QThread` runs on a live thread at window destruction →
`QThread: Destroyed while thread is still running` → hard abort.
The identical fatal was already fixed for connect and offline
threads (`_abort_offline_workers`).  **Verified by hand.**

**Fix direction:** re-wait with a longer budget and, as last resort,
`thread.setParent(None)` + leak-with-log instead of aborting — same
policy the offline-worker drain uses.  Pairs with #5.

## 🔴 5. `wait_for_stop` waits on the flag `request_stop` just set — TX grace period is a no-op

`ui/workers.py:1326`.  `request_stop()` sets `_stop_event`;
`wait_for_stop()` returns `self._stop_event.wait(timeout)` — which
is already set, so closeEvent's "give TX 1 s to unwind out of
play_blocking" returns True instantly, having waited for nothing.
The whole unwind budget collapses into the single `wait(3000)`,
making #4's abort path far more likely on any close during TX.
**Verified by hand.**

**Fix direction:** an `_idle_event` set in `_run_tx`'s `finally`;
`wait_for_stop` (rename `wait_for_idle`) waits on that.

## 🔴 6. TCI recv loop catches the wrong timeout type — healthy SDR force-disconnected

`radio/tci.py:368`.  websocket-client (1.9.0 installed) raises
`WebSocketTimeoutException` (subclass of `Exception`, **not**
`TimeoutError`) on socket timeout, so the idle-tolerance branch the
comment describes is dead code: any 5 s quiet period on the control
socket kills the recv thread, `is_alive` goes False, and three poll
failures later the app disconnects a perfectly healthy SunSDR —
mid-TX this aborts the transmission.  **Verified against the
installed library.**

**Fix direction:** `except (TimeoutError, websocket.WebSocketTimeoutException):`
(import guarded), keep the generic handler below it.

## 🔴 7. Connect cancel/timeout leaks an opened rig — Windows serial unrecoverable

`ui/main_window.py:249/252`.  `_RigConnectWorker.run` checks the
cancel event after `open()` (and after `ping()`) and returns
*without closing the rig it just opened*.  GUI timeout is 5 s; TCI
READY wait is 10 s — a server that answers at 7 s leaks a live
WebSocket + recv thread per attempt.  Direct serial on Windows is
worse: COM ports are exclusive-open, so one leaked handle makes
every subsequent Connect fail "access denied" until app restart.
**Verified by hand.**

**Fix direction:** in both cancel branches, `close()` the rig
(best-effort) before returning.

## 🔴 8. v0.2 migration re-runs every launch and reverts user-edited templates

`templates/migration.py:243` (+ gate `templates/manager.py:300`).
A v0.2 upgrader who deletes all eight v0.3 starter templates
(curating down to their own) fails the `starter_pack_installed`
gate on every launch; step 2 then re-migrates the legacy
`templates.toml` — `save_template` has no `dst.exists()` check — so
any edits to the migrated templates are silently reverted at every
app start, and the deleted starter pack is force-reinstalled.
Repeats forever because the v0.2 file is kept by design.
**Verified by hand.**

**Fix direction:** stamp a `migration_done` marker (file or config
key) after the one-time legacy migration and gate step 2 on it; and
give migration `save_template` the same `dst.exists()` skip that
`install_starter_pack` has.

---

## 🟡 9. Dead-at-launch rigctld corpse blocks auto-relaunch forever

`ui/main_window.py:2542`.  If rigctld exits within ~500 ms (wrong
model ID, port busy), the connect-failure path never calls
`_kill_rigctld`, so `_rigctld_proc` keeps the dead Popen (also never
reaped) and the `is None` guard skips respawn on every retry — the
user can't reconnect without restarting the app even after fixing
the root cause.  **Fix:** clear/reap the proc on connect failure.

## 🟡 10. Unbounded memory + O(N²) concat when a locked decode stalls

`core/decoder.py:1174` + `core/incremental_decoder.py:589`.
**Empirically reproduced** — VIS lock followed by dead carrier holds
the audio *twice* (232 MB after 5 simulated minutes, ~2.8 GB/hour)
with full-history `np.concatenate` per feed.  GUI watchdog bounds it
eventually (slow modes still accumulate hundreds of MB); headless
users of the documented streaming API exhaust memory.  **Fix:** cap
`_buffer` in DECODING at mode-duration × margin; make `_prune`
drop already-fed samples even when `_syncs_consumed == 0`.

## 🟡 11. PortAudio enumeration failure prevents app launch

`audio/devices.py:96` → `ui/main_window.py:406` →
`app.py` (MainWindow constructed outside try).  A host-API failure
(broken ALSA/PipeWine config, PortAudio error state after USB churn)
raises `PortAudioError` through an unguarded chain and the app dies
with a traceback; the same unguarded calls in SettingsDialog mean
the user couldn't even open Settings to pick a working device.
**Verified by hand.**  **Fix:** wrap `_all_devices()` → return `[]`
+ log; app launches with "no devices" UI state.

## 🟡 12. TCI input stream has no data-flow watchdog and never emits `stream_error`

`audio/tci_input_stream.py:56`.  The PortAudio worker has a 3 s/6 s
device watchdog; the TCI drop-in declares `stream_error` and emits
it nowhere.  Server-side audio stall with a live control channel =
"Capturing" forever, silent, for a monitoring station.  **Fix:**
mirror the InputStreamWorker watchdog (no chunks for N s while
subscribed → `stream_error`).

## 🟡 13. Template TOML numeric fields unvalidated → OOM at render / TypeError at re-save

`templates/toml_io.py:117`.  `width_pct = 1e8` from a shared
template file survives load, renders a ~3.2e8-px box, and
`PIL.Image.new` requests hundreds of GB — on Linux overcommit the
OOM killer SIGKILLs the app mid-TX (no `except` catches SIGKILL).
Wrong-typed fields (string opacity) load fine but make the next
re-save raise `TypeError` in a Qt slot.  **Fix:** clamp/validate
numeric ranges and types at load, same policy as
`AppConfig.__post_init__`.

---

## 🔵 14. `_kill_rigctld` never reaps after `kill()`

`ui/main_window.py:2938` — zombie + serial/TCP port race with the
respawned daemon.  **Fix:** `wait(timeout=…)` after kill;
best-effort.

## 🔵 15. TCI dropped-chunks counter race

`audio/tci_input_stream.py:190` — unlocked RMW across recv/worker
threads; InputStreamWorker fixed the identical pattern with
`_drop_lock` (H5).  **Fix:** same lock.

## 🔵 16. Migration slug collisions silently drop overlays

`templates/migration.py:241` — two same-named v0.2
templates/overlays produce one file; the count still reports both
migrated.  **Fix:** uniquify slugs (`-2` suffix).

## 🔵 17. Duplicate-template handler catches only `OSError`

`ui/tx_panel.py:470` — `TOMLDecodeError` (a `ValueError`) from a
corrupted-since-listing file escapes the Qt slot.  **Fix:** catch
the loader's error family (`TemplateLoadError`, `TOMLDecodeError`).

## 🔵 18. Update checker: `UnicodeDecodeError` escapes the except tuple

`ui/update_checker.py:158` — tuple covers `JSONDecodeError` but not
`UnicodeDecodeError` (a `ValueError`); a captive-portal Latin-1 body
crashes the worker slot.  **Verified by hand.**  **Fix:** add
`ValueError` (covers both) to the tuple.

## 🔵 19. CLI encode accepts `--sample-rate ≤ 0`

`cli/encode.py:64` — `0` → `ZeroDivisionError` traceback, negative →
`wave.Error`, both violating the documented exit-code contract.
**Empirically reproduced.**  **Fix:** argparse positive-int
validator, mirroring `Decoder.__init__`'s `fs<=0` guard.

---

## Suggested fix vehicle

v0.4.1 stability patch, one PR: 🔴 1-8 must-fix, 🟡 9-13 should-fix,
🔵 14-19 ride-along (all are one-to-five-liners).  #1/#2/#5 want a
shared "TX owns the rig while unwinding" design decision; everything
else is mechanical.  Regression tests per finding, matrix-gated as
usual.
