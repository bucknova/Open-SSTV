# Full-project stability audit — v0.6.13 (`5f45925`)

Audited 2026-10-01, on `main` after v0.6.13. Lens: **stability and
reliability**: PTT safety, hangs, crashes, data loss, and an unattended
station that keeps working. About 40,000 lines across `ui/`, `radio/`,
`audio/`, `remote/`, `core/`, `logbook/`, `config/` and `templates/`.

Method: read the lifecycle and threading code in full (`closeEvent`,
`app.main`, the TX worker, audio output and input), every rig backend,
the remote server, and the persistence layer. Swept the whole tree for
known failure patterns (atomic writes without `fsync`, untimed waits,
`terminate()`, unchecked indexing, bare broad excepts). Followed up the
open leads from earlier review sessions. **Every finding below was
reproduced before it was fixed.** Each fix has a regression test, and
each test was run against the old code and confirmed to fail for the
stated reason. Where an experiment was needed (signal delivery,
`QThread.terminate()`), the numbers are quoted.

**Status: all findings fixed** on `fix/stability-audit-v0.6.13`, one
commit per area. One lead was **disproved** (M1); see below.

Severity: 🔴 high (PTT safety / hang / process death / data loss) ·
🟡 medium · 🔵 low.

---

## 🔴 H1. SIGTERM / SIGINT ignored while idle; SIGHUP not handled

`app.py`. Handlers were installed with plain `signal.signal()`. CPython
runs a Python handler only between bytecodes, and an idle Qt event loop
runs none, so the handler never executed, and because it had replaced
the default action, the signal was ignored. **Reproduced:** a subprocess
test sent SIGTERM and SIGINT to an idle app, and both were still running
10 s later. The Linux release smoke test had already seen SIGTERM ignored
for two hours. `kill`, `systemctl stop` and a logout therefore never
reached `closeEvent`, where PTT is dropped. When the system escalated to
SIGKILL, a keyed rig stayed keyed and a spawned rigctld was orphaned.
SIGHUP (the launching terminal closed) wasn't handled at all, and its
default action kills the process without unkeying.

**Fix:** `install_quit_signals()`. `signal.set_wakeup_fd` writes to a
socket, a `QSocketNotifier` wakes Qt, and the queued Python handler runs
and calls `app.quit()`. SIGHUP is added on POSIX. A second signal during
shutdown is logged and ignored rather than skipping the unkey path.
**Verified:** all three signals now quit an idle app through
`closeEvent` and `aboutToQuit` in about 1 s.

## 🔴 H2. Local TX stopped last at quit — PTT keyed up to ~30 s

`ui/main_window.py:closeEvent`. Remote TX was unkeyed first, but a
*local* transmission was stopped only after the remote-server stop (up
to 7 s), the connect abort (2.5 s) and the offline-worker drain (20 s).
**Reproduced:** a test recorded the order as
`_stop_remote_server, _abort_connect, _abort_offline_workers, tx_stop`.

**Fix:** `request_stop()` (thread-safe, non-blocking) runs first.
`wait_for_idle()` still confirms it later. The offline drain wait drops
from 10 s to 2 s per thread, because its result is discarded at close.

## 🔴 H3. rigctld socket left open after a timeout — replies go off by one

`radio/rigctld.py:_send_recv`. On a timeout, or a malformed or oversized
reply, the client raised but kept the socket. A reply that arrived late
was then read as the answer to the *next* command, and every later
reply was off by one until a reconnect. The poll, the TX health monitor
and the TX worker share this link, so a stale `RPRT 0` could make
`set_ptt(False)` look successful before its own reply had been read.
**Reproduced** with a daemon whose first reply misses the timeout: the
next `get_ptt()` read the late frequency reply and returned **True**,
reporting the rig keyed when it wasn't.

**Fix:** close the socket on any failure that may leave a reply partly
read. A clean `RPRT -N` rejection, which is read in full, keeps it.

## 🔴 H4. Flex: a dropped connection was never detected

`radio/flex.py`. When the radio closed the link, the reader thread just
exited. `_require_alive()` kept passing, so the getters served cached
state indefinitely. The poll showed a dead radio as connected, and the
TX health monitor never noticed, although `get_ptt` is documented in the
code as the liveness probe it relies on. Flex TX audio goes through DAX,
not this socket, so a transmission carried on with the control link
dead. **Reproduced:** after the fake radio dropped the link, the getters
didn't raise, and `set_ptt(False)` took 5.0 s to fail.

**Fix:** the reader marks the link lost and fails in-flight commands.
The getters raise, and commands fail immediately. The TX unkey retry's
`close()` / `open()` clears the flag.

## 🔴 H5. `QThread.terminate()` at quit aborts or hangs the process

`ui/main_window.py` (offline-worker and connect-abort fallbacks), plus
the existing detach policy for the TX, audio, RX and poll threads (v0.4.0
audit high #4). **Measured** with a thread busy in numpy:

| at exit, thread still running | result |
|---|---|
| `terminate()` (old fallback) | never stopped it. 5/6 runs **abort** ("QThread: Destroyed while thread is still running", exit 134); 1/6 **hung** on the GIL |
| `setParent(None)` only (old detach policy) | **abort**, 5/5 (the qFatal moves to interpreter shutdown) |
| detach + keep referenced + `os._exit()` | **exit 0**, 5/5 |

So `terminate()` never did its job, and the v0.4.0 detach fix only
postponed the abort.

**Fix:** every stuck thread is detached and kept referenced in
`_DETACHED_AT_SHUTDOWN`. When that list isn't empty, `app.main` flushes
the logs and leaves with `os._exit()`. A normal shutdown is unchanged.

## 🔴 H6. Atomic writers didn't `fsync`

`config/store.py`, `config/templates.py`, `templates/toml_io.py`. Each
wrote `<name>.tmp` and then `os.replace`, with no `fsync`. That's atomic
but not durable: after a power cut the replaced file can be empty on
APFS, XFS or btrfs, and an empty config loads as defaults (callsign,
devices and rig setup gone). The template writer also had no lock and a
fixed `.tmp` name, so two saves of one template could interleave.

**Fix:** `fsutil.atomic_write_bytes()`. It writes a unique temp file,
`fsync`s it, replaces the target, then `fsync`s the directory. All three
writers use it, and so does auto-save (M5).

---

## 🟡 M1. `closeEvent` re-entry during its nested event loop — **NOT A BUG**

The teardown runs nested event loops, and its only re-entry guard
(`_teardown_complete`) is set at the end, so a second close request
looked as though it could run the teardown again inside the first. **It
can't:** Qt's `QWidget::close()` refuses re-entry while the widget is
mid-close (its internal `is_closing` flag), and window-system close
events take the same path. A test of the scenario passes on the old
code. No guard was added. Note that `_closing` must not serve as one in
any case: other paths set it early (see
`test_closing_flag_does_not_skip_teardown`).

## 🟡 M2. RX capture never resumed after a device loss

`ui/main_window.py`. A device loss (a USB glitch or a re-plug) stopped
capture until someone clicked Start. The app already flagged a re-plug
for device re-lookup, but nothing retried. On a station left receiving
overnight, or run from the remote page, a one-second glitch ended
reception without anyone noticing.

**Fix:** a retry chain (5, 10, 20, 40, 60 s, then every 60 s) through
the normal start path. It ends on success, when the user presses Start
or Stop, and at shutdown. An attempt due during a TX waits, because
starting capture would lift the RX gate mid-transmission. The
device-loss message no longer says "click Start".

## 🟡 M3. TCI: cached getters ignored a dead link; `modulation:` from any TRX

`radio/tci.py`. The getters answered from the push cache without
checking the WebSocket, so after the SDR app quit they kept reporting
the last frequency and mode, and the poll (which only calls the getters)
showed the radio connected. Separately, `modulation:` was accepted for
any TRX, unlike `vfo:` and `trx:`. **Reproduced:** `modulation:1,LSB`
overwrote our `DIGU`. On two-receiver SunSDR2/ExpertSDR setups, that
value drives the Band Plan keep-DIGU decision from #68.

**Fix:** the getters raise `RigConnectionError` on a dead link, and
`modulation:` is accepted only from TRX 0. This applies directly to
[Lyra](https://github.com/N8SDR1/Lyra-SDR-cpp), whose TCI server
advertises two receivers. Its author confirmed #68's DIGU/DIGL tuning
working on #73 during this audit.

## 🟡 M4. Icom CI-V took any rig frame as the current command's reply

`radio/serial_rig.py`. Read deadlines are 200 ms, so a late reply could
be accepted as the next command's. **Reproduced:** a stale frequency
reply read as **PTT keyed**, and a stale data frame **masked**
`set_ptt(False)`'s own NG, so a rejected unkey was reported as done.

**Fix:** reads require a data frame echoing their command bytes, and
sets require OK/NG; anything else is skipped. Residual: a late OK to an
earlier *set* is indistinguishable from ours, because CI-V
acknowledgements carry no command byte.

## 🟡 M5. Auto-save failures: a dialog per image, truncated files

`ui/main_window.py:_autosave_image`. Every failure opened a modal
dialog. **Reproduced:** 5 dialogs for 5 failures. On an unattended
station with a full disk they stacked up, invisible to the remote page.
PIL wrote straight to the final path, leaving truncated images on a full
disk, and a `ValueError` from PIL escaped the slot.

**Fix:** encode in memory, write with `atomic_write_bytes`, catch both
exceptions, and show one dialog per failure streak (later failures go to
the status bar and the log).

---

## 🔵 L1. rigctld `get_ptt()` IndexError on an empty body

`IndexError` isn't a `RigError`, so it escaped every handler, and this
is the TX health monitor's call. It now raises `RigCommandError`.

## 🔵 L2. Logbook leaked its SQLite connection on `SchemaTooNewError`

The error tells the user to move the file aside, but the open handle
kept it locked on Windows. **Reproduced:** the connection was still
usable after the refusal. It's now closed before the error propagates.

## 🔵 L3. ADIF `<EOH>` text in a value dropped QSOs

The importer searched raw bytes for the first `<EOH>`. **Reproduced:**
in a header-less file, three QSOs went in and one came out (the QSO
whose comment contained the text was lost too). The ADIF rule ("a file
starting with `<` has no header") is now followed. The length-aware
tokenizer still discards a header made of tags at its real `<EOH>`.

## 🔵 L4. Pillow 13 removes `Image.fromarray(mode=)`

Used only in `scripts/roundtrip_all_modes.py`. It was harmless under
today's `Pillow<12` pin, but raising that pin would have broken the
round-trip audit harness. Removed, and verified under
`-W error::DeprecationWarning`.

---

## Verified sound (no action)

- **TX playback watchdog.** A stalled output device can't hold PTT. A
  timer sized to the expected TX length plus 20% (minimum 30 s) calls
  `request_stop()`, and the `finally` always runs `_unkey_with_retry`.
- **Audio-output wedge** (memory: remote TX unkey hang follow-up).
  Resolved by design in v0.6.2: playback uses PortAudio's callback API,
  so the TX thread is never blocked in a `write()`.
- **Remote server.** It has request-read timeouts, body and upload caps,
  an SSE client limit, and a dead-man's-switch tick thread that catches
  its own exceptions. `start()` clears the stop flag, so the switch
  survives disabling and re-enabling remote access.
- **Decoders.** NaN/Inf are sanitised on every chunk in the batch path,
  and the streaming decoder is fed only sanitised audio. Both have
  buffer caps.
- **Kenwood / Yaesu reply matching** (by command prefix).
- **Logbook dedupe key** (callsign + mode + time to the second; omitting
  band is not a collision risk in practice).

## Not changed, noted

- Kenwood/Yaesu `_read_response` polls with a 10 ms sleep under the
  serial lock. That's CPU, not correctness.
- Worst-case shutdown can still take tens of seconds if every component
  wedges at once. PTT is now handled first, and stuck threads no longer
  abort or hang the exit.
- Offline decode has no cooperative cancel. At quit it's now detached
  rather than terminated, which is safe.
- TCI and Flex don't reconnect on their own after a drop. That's now
  *detected* correctly (the poll's three-strike disconnect fires), and
  reconnecting is the operator's choice.
