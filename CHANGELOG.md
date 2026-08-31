# Changelog

All notable changes to Open-SSTV are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- **Kenwood and Elecraft transmissions no longer abort a second in.**
  Reading PTT sent `TX;` — which on those radios is the *set* command that
  keys the transmitter, not a query. It is never answered, so the
  once-a-second health check timed out and aborted the transmission, and
  the stray key could switch the radio from its data input to the
  microphone mid-image. PTT is now read from the `IF` status string, as
  Hamlib does.
- **Connecting a rig no longer keys it.** Opening a serial port asserted
  both DTR and RTS (pyserial raises both on `open()`), and the CAT backends
  never lowered them. On the common single-cable interface that wires one
  of those lines to PTT, pressing **Connect Rig** put the radio on the air
  for the whole session, transmitting a dead carrier with nothing in the UI
  to show it. Both lines are now held low from the moment the port opens.

- **The remote web UI no longer scrolls sideways on narrower phones.** The
  header's own controls added up to about 408 px and flex items refuse to
  shrink below their content, so on a 375 or 390 px screen — iPhone SE, 12/13
  mini, 14 — the page was forced wider than the display and the **Refresh**
  button sat off the right edge. The header now wraps to a second row instead.
- **QSO-bar RSTr / QTH / Grid now reach the logbook, not just the UDP
  broadcast.** The three fields added to the TX panel's QSO bar in v0.6.7
  fed only the **[External Log]** datagram, so typing a QTH and grid and
  then logging the contact silently dropped both — even though the capture
  dialog has rows for exactly those fields, sitting one row below where you
  typed them. They now pre-fill the logbook draft the same way ToCall, RST,
  Name, and Note always have.
- **ADIF `OPERATOR` field now contains the station callsign, not the
  configured operator name**, matching the ADIF 3.1.5 definition ("the
  call sign of the operator(s) in control of the station"). This
  affects both file-based ADIF export and the UDP companion-logger
  broadcast — some cloud QSL services (eQSL, LoTW, ClubLog) use this
  field for operator matching, and a non-callsign value could cause a
  QSO to silently fail to auto-upload. The operator's configured
  display name now goes into the correct field, `MY_NAME`.

---

## [0.6.8] — 2026-08-17

### Fixed

- **Band Plan tuning silently failed on the Yaesu FT-450/450D.** Yaesu's
  `FA` set-frequency command uses a model-dependent digit width — the
  FT-450/450D wants 8 digits, the FT-991A and other modern-CAT rigs want 9
  — and the wrong width is rejected outright by the radio. Because Yaesu
  set commands are fire-and-forget (a legitimate set gets no reply at all),
  the rejection was invisible: mode changes worked while the frequency
  never moved. Open-SSTV now detects the width from a live read and caches
  it per connection instead of hardcoding one.
- **Band Plan tuning now verifies that the frequency actually changed**
  rather than assuming it did, and reports failures in the status bar
  instead of only the debug log. Verification tolerates a brief settle
  delay, because some backends (TCI, FlexRadio) report frequency from a
  cache updated by an async push rather than from the set itself.
  Frequency and mode are applied independently, so a verification problem
  can never suppress the mode change.

### Added

- **SSTV mode policy for Band Plan tuning** (**Settings → Radio → Direct
  Serial**), mirroring WSJT-X's rig mode setting: **Don't change mode** /
  **Voice (USB/LSB)** (default, unchanged behavior) / **Data/Pkt**. Band
  Plan previously always forced plain USB/LSB with no way to ask for a
  data-mode variant. Data/Pkt currently resolves for Yaesu CAT
  (`DATA-U`/`DATA-L`); Icom and Kenwood/Elecraft fall back to Voice with a
  logged warning, since their data modes need model-specific commands we
  haven't verified against real hardware.

Both contributed by [@dacrhu](https://github.com/dacrhu).

---

## [0.6.7] — 2026-08-14

### Added

- **External Log: broadcast a QSO over UDP to companion logging software.**
  A new **[External Log]** button next to **[Logbook…]** on the TX panel's
  QSO bar sends the current contact as a single UDP datagram, so QLog,
  JTAlert, GridTracker, Log4OM, N1MM, and similar programs can pick it up
  automatically — the same "UDP logging" convention WSJT-X popularized.
  Deliberately separate from the existing per-image auto-capture flow (an
  SSTV QSO is usually several images; this sends exactly one record, once,
  for the whole contact) and from the local SQLite logbook (sending never
  writes a row, and logging never sends a datagram). The QSO bar gained an
  **RSTr** field next to the renamed **RSTs**, plus **QTH** and **Grid**
  (upper-cased as you type); frequency comes from the Radio panel's
  **Freq:** field, time is the click's UTC timestamp, and mode is always
  `SSTV`. **Settings → Logging → UDP QSO log** configures the destination
  host/port (default `127.0.0.1:2237`, WSJT-X's own default) and picks
  between the framed **WSJT-X protocol** (QLog/JTAlert/GridTracker/N1MM)
  and unframed **Raw ADIF** (Log4OM-style). The status bar reports
  "sent (not confirmed)" on purpose — UDP has no acknowledgement, so a
  successful send only means the datagram left your machine, never that
  the companion program received it. See
  [docs/logbook.md](docs/logbook.md#broadcasting-to-a-companion-logger-udp).
  Contributed by [@dacrhu](https://github.com/dacrhu).

---

## [0.6.6] — 2026-08-13

### Fixed

- **Quitting could raise an error on the way out.**  ``closeEvent`` fires
  twice in ordinary use — once for the window's own close, once from
  ``app.aboutToQuit`` — and the second pass reached worker objects the
  first had already freed, escaping as a ``SystemError`` out of the Qt
  override.  It is now a no-op the second time.  The app still exited, so
  this was noise rather than a crash, but it also masked two long-standing
  teardown errors in the test suite (one of which prevented a test from
  ever running).

---

## [0.6.5] — 2026-08-13

### Fixed

- **Deleted default templates came back on the next launch.**  The startup
  check asked "are any starter templates present?" and treated *none* as a
  fresh install — so removing one or two stuck, but curating away all eight
  silently reinstalled the lot.  Whether the pack has ever been installed
  is a historical fact, and is now recorded as one (a marker file, the same
  approach the v0.2 legacy migration already used).  Deleting every starter
  now sticks.  Existing installs are grandfathered — nobody's templates
  reappear or vanish on upgrade.  Reported by
  [@dacrhu](https://github.com/dacrhu) (#42).

### Added

- **Settings → General → Restore Default Templates.**  Since deleting a
  starter is now permanent, there is a deliberate way back.  It re-installs
  only the templates that are actually missing — your own templates, and
  any edits you have made to a starter, are never overwritten.

---

## [0.6.4] — 2026-08-11

Receive robustness under fading conditions.

### Fixed

- **Deep QSB could cut a decode short no matter how patient you told it to
  be.**  The receive watchdog has always had two independent guards — one
  for "no new line in N seconds", one for total elapsed time — but
  Settings → Receive → *No-progress timeout* only ever moved the first.
  The second stayed pinned at ``mode duration × 1.5``, so a Scottie S1 was
  abandoned after 164 s however long a fade the operator said to tolerate,
  and raising the setting appeared to do nothing.  The total budget now
  also scales with that setting (``duration + timeout × 4``), so a decode
  can ride out several fades of the length you asked for.  Modes longer
  than 40 s keep their previous default budget exactly; the three short
  modes gain 2–5 s.  Reported by
  [@dacrhu](https://github.com/dacrhu) (#40).

---

## [0.6.3] — 2026-08-11

macOS live capture fix.

### Fixed

- **macOS: live capture received only silence** because the app bundle was
  missing its microphone permission.  macOS gates *every* audio input
  behind TCC — including virtual loopback devices like BlackHole that never
  touch a real microphone — and under the hardened runtime it refuses to
  even show the prompt unless the bundle carries the
  ``com.apple.security.device.audio-input`` entitlement.  Worse, the denial
  is invisible from the app's side: the input stream opens successfully and
  delivers nothing but zeroes, so the UI reported "Capturing… / Listening…"
  while nothing could ever decode.  The bundle now ships
  ``NSMicrophoneUsageDescription`` and the audio-input entitlement, and
  Open-SSTV checks the authorization status before starting capture —
  refusing with a message that names the exact System Settings pane rather
  than pretending to listen.  Reported, root-caused, and independently
  verified by [@y1feng200156](https://github.com/y1feng200156) (#35).

---

## [0.6.2] — 2026-08-11

Linux audio: PipeWire sink routing, and a crash fix that supersedes the
v0.6.1 TX-wedge workaround.  Contributed by [@dacrhu](https://github.com/dacrhu).

**Linux users on v0.6.1 should update:** pressing Stop mid-transmission
could segfault the application on that release.

### Added

- **TX audio can now target a specific PipeWire sink on Linux** (e.g. a
  user-created virtual "Radio" routing sink), listed in Settings → Audio →
  Output alongside the regular PortAudio device list, labelled "(PipeWire)".
  PortAudio only exposes PipeWire's own named sinks under its JACK host
  API, and writing to that host API directly via PortAudio's blocking
  `OutputStream.write()` was found to corrupt real audio (confirmed via
  spectrogram comparison: a WAV export of the exact TX buffer is clean, the
  same buffer played over the ALSA "default" pass-through is clean, only
  writing to the JACK-hostapi device directly produces broadband noise —
  `sd.play()`'s callback-based path does not exhibit this). Instead,
  Open-SSTV now always opens the safe ALSA "default" stream and uses
  `pactl move-sink-input` to move that specific, already-open stream onto
  the chosen sink after the fact — every other application's audio is
  unaffected, unlike changing PipeWire's system-wide default sink would be.
  New module: `audio/pipewire_route.py`. Everything degrades gracefully
  (plays on the system default, as before) if `pactl` is unavailable, no
  PipeWire session is running, or the routing call fails for any reason —
  a routing failure never aborts a transmission.

### Fixed

- **Test Tone was disabled unless a rig was connected.** Both the Radio
  panel and Settings → Audio's "Test Tone" buttons required an active
  rigctld/serial/TCI/FlexRadio connection before they could be clicked —
  but PTT keying already went through the no-op `ManualRig` backend
  without a rig connected, so the gating served no functional purpose and
  just blocked VOX/manual-PTT operators (who never connect a rig at all)
  from calibrating ALC. Both buttons are now enabled whenever no
  transmission is already in flight, regardless of rig-connection state.
- **Pressing Stop mid-transmission could segfault the whole application.**
  Root-caused with `faulthandler` and a clean-worktree comparison: once
  PortAudio's blocking `OutputStream.write()` genuinely wedged on a real
  Linux/PipeWire system, nothing could safely unblock it — `stream.abort()`
  from another thread returned without actually interrupting the write;
  the existing wedge-escalation fallback (`stream.close()` from a third
  thread, added in v0.6.1 for a Windows/MME hang) corrupted the heap inside
  PortAudio's ALSA XRUN-recovery path while that write was still blocked;
  and even leaving the wedged stream alone didn't help, since `sounddevice`
  closes every open stream at interpreter exit, re-triggering the same
  crash the next time the app quit. Confirmed unrelated to the PipeWire
  routing feature above — it reproduced identically on the plain "System
  default" device. `play_blocking` now drives TX audio entirely through
  PortAudio's callback API (the same mechanism RX already used safely, and
  the one the old fast path was quietly built on) instead of a blocking
  write loop, eliminating the wedge by construction: there is no longer a
  blocking write() call anywhere in the hot path for a stream to get stuck
  in. Stress-tested at ~20 varied Stop timings — cross-thread, with and
  without PipeWire-sink routing live, through the real TX worker — with
  zero wedges and zero crashes. The now-unreachable escalate-to-`close()`
  machinery has been removed from `audio/output_stream.py`.
- **Worked around a `sounddevice` internal that could crash a long TX under
  test instrumentation.** While validating the fix above, a *different*
  segfault turned up during a ~2-minute real-audio integration test (not
  part of this project's own CI selection, which excludes `gui`-marked
  tests): `sounddevice`'s own callback-wrapping code builds each buffer via
  an in-place `ndarray.shape = ...` assignment deprecated since NumPy 2.5,
  firing on *every* callback — tens of thousands of times over a real
  transmission. Python's default warning filter dedupes these harmlessly in
  normal use, but a warning-*capturing* context (pytest's default per-test
  capture) disables that dedup, and the warnings module isn't thread-safe
  against that volume from PortAudio's real-time callback thread. Patched
  `sounddevice._array()` at import time to build the same buffer via
  `np.reshape` (an equivalent zero-copy view, never deprecated) instead —
  confirmed via a forced `simplefilter("always")` check that the warning no
  longer fires at all, and via repeated clean runs of the previously-
  crashing scenario. Not a real-world risk for users (the default filter
  already made this harmless outside a test-capture context), but cheap
  insurance now that it's been seen.

---

## [0.6.1] — 2026-07-30

Rig-control and audio-path robustness, driven by a FlexRadio operator's
bug report.  No new SSTV features; everything here is about the paths
that were failing quietly.

### Added

- **FlexRadio direct control** — a new Radio connection mode that talks the
  SmartSDR TCP API straight to a 6000-series radio, so a Flex no longer
  needs ``rigctld`` or a virtual serial port in between.  Enter the radio's
  IP, pick a slice, and PTT / frequency / mode work directly; there's a
  *Test FlexRadio Connection* button beside it.  Audio still comes from
  your sound device (DAX or otherwise).  The S-meter is not implemented —
  Flex streams meters over VITA-49/UDP — and reads as 0.

### Fixed

- **rigctld: the connection test could do nothing at all.**  Numeric
  responses were parsed with a bare ``int()``, so any unexpected
  formatting raised ``ValueError`` — not a ``RigError`` — and escaped every
  handler in the app.  In the Settings connection test that meant no dialog
  whatsoever (and on a packaged Windows build the traceback goes to a
  stderr that doesn't exist).  Float-formatted values are now accepted, an
  unreadable passband falls back to Hamlib's "default width" (0), and every
  parse failure surfaces as a proper ``RigError``.  The test button also
  catches everything now, so it can never fail silently again.
- **rigctld: an unsupported S-meter dropped the whole rig.**  The 1 Hz poll
  read frequency, mode, and signal strength in one block, and three
  consecutive failures trigger auto-disconnect — so a backend without a
  ``STRENGTH`` level (answers ``RPRT -11``) showed "Connection lost" about
  three seconds after connecting, on a rig whose PTT and frequency control
  were working fine.  The S-meter is cosmetic: it can no longer disconnect
  the radio.

- **TX audio wedge — recovery and diagnosis.**  On some Windows/MME setups
  the TX audio device stops draining mid-transmission and ``abort()``,
  called from another thread, does not unblock the writer — leaking the TX
  worker thread, so further transmissions failed until the app was
  restarted.  (Safety was never affected: PTT is dropped directly and
  independently of the audio path.)  Stopping playback now **escalates**:
  after ``abort()``, a watchdog checks whether the writer has actually made
  progress and, if not, closes the stream outright.  Whether or not the
  escalation succeeds, the wedge is now logged explicitly instead of
  vanishing.
  *This one could not be reproduced here — it needs the affected hardware —
  so treat it as an attempted fix with much better instrumentation behind
  it.*

### Changed

- **Much more diagnostic logging around audio and rig control**, because
  the last bug report's log couldn't answer basic questions:
  - the app version is now written to the **log file** (it was printed only
    to stderr, discarded on a packaged Windows GUI build — so no report
    ever showed which version produced it);
  - rig connect success/failure is logged;
  - ``rigctld`` traces every command and response at DEBUG, to pair with
    rigctld's own ``-vv``;
  - every TX audio chunk write is timed, and a stalling device is called
    out by name and host API;
  - input-stream teardown times ``stop()`` and ``close()`` separately, so
    the "audio worker stop() did not complete in 2 s" warning on shutdown
    now says *which* call blocked and for how long — and no longer blames
    TCI on setups that aren't using it;
  - a TX output device on the **MME** host API logs a one-time warning
    recommending WASAPI, since MME is where the audio wedge has been seen.

---

## [0.6.0] — 2026-07-11

Remote station — drive Open-SSTV from a phone or laptop browser.  An
embedded, **opt-in** web server (off by default; loopback + token) lets a
paired browser watch RX live, browse the gallery and logbook, and —
behind an explicit gate and a connected CAT rig — compose and transmit
SSTV images.  Server-push over SSE, control over POST; the only new
dependency is ``segno`` (pure-Python QR) for pairing.  Design notes:
``design/remote/architecture.md``.

### Added

- **Remote view** — a read-only gallery + a **live RX stream** (SSE) that
  repaints as frames decode, a read-only logbook, and **Settings →
  Remote** to enable it with host / port / token and a scannable pairing
  **QR**.  A status pill in the browser and a "Remote on" indicator in
  the desktop app.
- **Remote transmit** — a Qt-free reference-monitor control plane is the
  sole authority for keying: **off by default**, single-writer lease,
  per-transmit confirm token, a **dead-man's-switch** (browser heartbeat;
  a dedicated tick thread unkeys if it lapses), and it **requires a
  connected CAT rig** (refuses the no-op manual/VOX backend).  A
  full-window **ON AIR** takeover shows a progress bar and
  elapsed / remaining timer.
- **Remote compose** — the browser takes a **photo (camera or upload)**,
  **crops/adjusts** it (pan · pinch · zoom, matched to the selected
  mode's frame aspect — including the tall Scottie S2 / Martin M2), picks
  a station template, and transmits.  The station re-renders the exact
  on-air bytes with the desktop compositor; composed images are staged in
  memory, never written to the gallery.
- The remote gallery lists **newest first**.
- **Desktop:** the RX "Listening" indicator is now an activity-gated
  animation (pulses while audio flows, greys on a stall) instead of a
  ticking "Xs buffered" counter.

### Fixed

- **Dead-man's-switch stuck-PTT (safety):** the unkey dropped PTT via the
  audio worker's unwind, so a worker wedged in a blocking PortAudio write
  could leave the rig keyed.  It now commands PTT off **directly**,
  independent of the audio thread.  Dummy-load verified on a real rig.
- Composed photos honour **EXIF orientation** (phones store rotation as a
  tag).
- Aborting a transmission no longer reports a spurious "audio output
  device error" (that error *was* the abort); the compose status no
  longer sticks on "Transmitting…".

### Dependencies

- Added ``segno`` (pure-Python QR codes) for remote-access pairing.

---

## [0.5.0] — 2026-07-07

The image gallery — completing the v0.4 logging workflow.  A built-in
browser for the pictures you've received (and, with TX auto-save on,
transmitted), joined to the logbook so every image knows its contact.
Design notes: ``docs/v0.5-plan.md``; operator guide: ``docs/gallery.md``.

### Added

- **Image gallery** — Tools → Gallery… (Cmd/Ctrl+G) opens a detached
  thumbnail-grid window paired with the Logbook.  It scans your image
  save directory (plus opt-in TX auto-saves and any
  ``gallery_extra_dirs``) and **left-joins the logbook**: a logged
  image shows its callsign / mode / frequency / time and a one-click
  **→ QSO** jump; images you never logged still appear, dated and
  mode-tagged from their filename.
- **Filter & group** — by callsign, mode, or date range (debounced,
  UTC-correct), sorted by date / callsign / mode with captions that
  follow the sort.
- **Operations** — **Re-send to TX** (loads an image into the TX panel
  to transmit again), **Export…** (copy elsewhere; original untouched),
  and **Delete** (removes the file; a linked contact is kept, its image
  link cleared — the mirror of the Logbook's "delete a QSO keeps the
  file").
- **Cross-links** — Gallery **→ QSO** focuses the Logbook on that
  contact; the Logbook's **Show in Gallery** does the return trip.
- **Lazy disk-cached thumbnails** — generated on demand as you scroll
  and cached under the platform cache dir (keyed by path + mtime, so
  edits regenerate), pruned on close.  Smooth into the tens of
  thousands of images with a plain list view — no virtualization.
- New advanced config: ``gallery_extra_dirs`` (extra scan folders,
  TOML-only) and ``find_by_image_path`` on the logbook store.

---

## [0.4.1] — 2026-06-13

Stability patch: a full-project audit run immediately after the
v0.4.0 release (four subsystem auditors, findings adversarially
verified — ``docs/audit_fable_5_v0.4.0.md``) produced nineteen
findings, all fixed here with a regression test each.  No new
features; every change hardens an error, shutdown, or stall path.

### Fixed

**PTT safety & TX lifecycle**
- A transient CAT failure mid-TX could leave the radio keyed: the
  involuntary-disconnect handler tore down the rig backend while the
  TX worker's unkey retries were using it.  Teardown is now deferred
  until the worker reports fully unwound.
- ``closeEvent``'s "let TX unwind" grace period waited on the stop
  *request* flag (a no-op); it now waits on a real idle event that
  covers the whole transmission including unkey retries.
- A wedged worker thread at quit no longer qFatal-aborts the process
  — timed-out threads are detached from the window (leak-with-log).

**Rig plumbing**
- Auto-launched rigctld's stderr is drained into the app log — the
  unread pipe used to fill (~64 KB of hamlib chatter) and wedge the
  daemon, worst case while PTT was keyed.  A rigctld that dies at
  launch is now reaped and cleared so reconnect can respawn it, and
  ``kill()`` is followed by a reap (no zombie racing the respawn).
- TCI: the recv loop now catches websocket-client's actual timeout
  exception — an idle control channel no longer force-disconnects a
  healthy SDR (or aborts a TX).  The TCI audio stream gains the same
  stall watchdog the PortAudio input has, and its overflow counter is
  lock-protected.
- A cancelled/timed-out connect closes the rig it opened — a leaked
  exclusive COM handle on Windows blocked reconnects until restart.

**Decoder & audio**
- Non-finite samples (NaN/Inf from glitchy drivers or virtual audio
  cables) are zeroed at the decode entry points and clamped in all
  three pixel samplers — one NaN chunk used to wedge the streaming
  decoder permanently.
- A stalled decode (VIS lock then dead carrier, or a mid-image fade)
  no longer grows memory without bound (~2.8 GB/hour): both the
  streaming front-end and the incremental backend now cap their
  buffers at 1.5 image-durations.
- PortAudio enumeration failure degrades to "no devices" instead of
  aborting app launch and locking the user out of Settings.

**Templates, migration & misc**
- The one-time v0.2 template migration is marker-gated and never
  overwrites existing files — upgraders who deleted the starter pack
  no longer have their edited templates silently reverted every
  launch; same-named legacy templates get unique slugs instead of
  overwriting each other.
- Template numeric layer fields are validated at load: a shared
  template with an absurd ``width_pct`` can no longer OOM-kill the
  app at render time.
- Duplicating a template corrupted after gallery listing shows an
  error dialog instead of a traceback; the update checker tolerates
  non-UTF-8 (captive portal) responses; ``open-sstv-encode`` rejects
  non-positive ``--sample-rate`` values cleanly.

## [0.4.0] — 2026-06-12

The logbook release.  Every SSTV contact you make or hear can now be
captured — image, mode, frequency, time, callsign, RSV report, and
notes — and exchanged with the rest of the logging ecosystem as ADIF.
Design notes live in ``docs/v0.4-plan.md``; the operator guide is
``docs/logbook.md``.

### Added

- **QSO logbook** — SQLite database at ``user_data_dir/logbook.db``
  (schema-versioned, stdlib ``sqlite3``, no new dependency).  Rows
  store *paths* to images/audio, never blobs; deleting a QSO never
  touches your files.
- **Capture flow** — a pre-filled Log QSO dialog opens after every
  transmission and reception (mode, UTC time, rig frequency, image
  thumbnail; TX drafts pull ToCall/RST/Name/Note from the QSO bar).
  Esc writes nothing.  ``auto_log_qsos`` switches to silent drafts.
- **Party-line RX capture setting** — SSTV calling frequencies carry
  everyone's exchanges, so Settings → General → Logbook → *RX
  capture* chooses when a reception offers the dialog: after every
  decode (default), only while a QSO is in progress (ToCall filled),
  or never.  Your own transmissions always offer the dialog.
- **Gallery "Log QSO…"** — right-click any decoded thumbnail to log
  it deliberately; the monitoring workflow (decode freely, log only
  the exchange that was yours).
- **Logbook window** — Tools → Logbook… (Cmd/Ctrl+L) or the
  [Logbook…] button on the QSO bar: filterable table (callsign,
  mode, direction, local-day date range), image preview with
  missing-file indicator, edit/delete, manual entry with Save & New.
- **ADIF 3.1.5 import/export** — export the filtered view with your
  station identity stamped per record (SUBMODE in MMSSTV compact
  form); import from other loggers with dedupe on
  (callsign, time, mode), batched in a single transaction.
- **Settings → Logging tab** — log-level selection (next launch;
  ``OPEN_SSTV_DEBUG`` still wins), Open Log Folder, diagnostics
  cross-link.  New ``log_level`` config field.
- **Diagnostics** — optional logbook member in the export zip,
  default **off** (worked callsigns are identifiable info), snapshot
  taken via sqlite's backup API for a consistent copy.
- **Note field** on the TX panel QSO bar (the field reserved since
  v0.3) — resolves the ``{note}`` template token and pre-fills the
  capture dialog.

### Fixed

Pre-release audit hardening (full findings in
``docs/audit_v0.4_logbook.md``): one corrupt record no longer aborts
an entire ADIF import; silently-drafted QSOs keep their image on
disk; the capture flow stands down during app shutdown; offline WAV
decodes are stamped with the recording's mtime and no rig frequency;
date filters interpret picked days in local time; ``%``/``_`` in
filters match literally; failed edits can't display as saved; filter
typing is debounced and a broken logbook DB degrades to an inline
notice instead of modal storms.

---

## [0.3.23] — 2026-06-09

Stability and usability fixes from the 2026-06-09 full audit — the
critical + high findings landed first (PR #22); the medium + low
findings followed in a second PR (#23).  No feature changes.

### Fixed (medium/low audit findings)

- **Hand-edited config values are validated at load (M1).**  Ports,
  baud rate, input gain, and the string-enum fields
  (``rig_connection_mode``, ``default_tx_mode``, ``rig_serial_protocol``,
  ``rig_ptt_line``, ``tx_banner_size``) are clamped or reset to defaults
  with a logged warning instead of mis-dispatching or failing later at
  connect/render time.  Wrong-typed values (a string where an int
  belongs) no longer crash startup.
- **A corrupt config no longer silently wipes your settings (M2).**
  The unreadable file is preserved as ``config.toml.corrupt`` and a
  startup dialog says where it went — a hand-edit typo is now
  recoverable instead of a mystery reset.
- **Template editor confirms destructive actions (M3).**  Cancel (or
  Esc) with unsaved edits asks before discarding; *Remove* asks before
  deleting a template.
- **Template system failures are visible (M4).**  Unreadable template
  files show a warning banner in the gallery (tooltip names the files);
  a missing station image logs a WARNING naming the layer; a broken
  bundled font falls back to Pillow's built-in instead of killing the
  render.
- **v0.2 → v0.3 template migration preserves custom color and position
  (M5)** instead of resetting every migrated overlay to white /
  bottom-center.
- **Settings save failures are a modal warning (M6)** instead of a
  transient status-bar message — no more "the app forgot my settings"
  after a read-only config directory ate the write.
- **Truncated audio files decode instead of crashing (M7).**  WAVs cut
  mid-frame or mid-sample are trimmed with a logged warning; a zero/
  negative header sample rate raises a clear error; empty buffers pass
  through resampling.
- **Unplugged serial ports release their OS handle (M8)** — a failed
  ``close()`` on a vanished USB adapter force-closes the file
  descriptor so the replugged port doesn't come back "busy".
- **PortAudio reset is atomic with the TX interlock (M9)** — closes the
  check-then-act window where a TX starting at the wrong microsecond
  could have its stream killed mid-open.
- **QSO template saves are serialised and stale ``.tmp`` files swept
  (M10)**, matching the config store's atomic-write contract.
- **Low-priority polish:** clipped TX-banner callsigns log a §97.119
  warning; the Ctrl+S / Cmd+S save-image shortcut is now discoverable
  as *File → Save Received Image…*; CW/config clamp messages upgraded
  to warnings; User Guide no longer claims a stale version number;
  the incremental decoder rejects nonsensical sample rates at
  construction.

### Fixed (critical/high audit findings)

- **Clipboard copy no longer risks a crash on exit (C1).**  *Copy to
  Clipboard* in the image gallery now places a ``QImage`` on the
  clipboard instead of a ``QPixmap``.  A ``QPixmap`` clipboard entry is
  a macOS pasteboard *promise* that needs a living ``QGuiApplication``
  to honour; quitting with the promise pending logged
  "Cannot keep promise…" and could segfault during Qt teardown (the
  intermittent CI segfault), and the copied image silently vanished
  from the clipboard on quit.
- **PTT unkey now retries with a backend reconnect (H1).**  If the
  control link dies mid-TX (USB-serial unplug, rigctld/TCI drop),
  ``set_ptt(False)`` on the dead link can never succeed.  The TX worker
  now closes and re-opens the backend between up to 3 unkey attempts —
  a replugged adapter or restarted daemon gets the radio unkeyed
  automatically.  The final failure message states explicitly that the
  radio may still be transmitting.
- **App can no longer hang on quit when the rig backend is dead (H2).**
  ``closeEvent`` caught only ``RigError`` around ``rig.close()``; a raw
  ``OSError``/``termios.error`` from an unplugged port escaped the Qt
  virtual override, aborting shutdown midway and orphaning the rigctld
  child process.
- **Export-to-audio writes atomically (H3).**  The offline encode
  worker writes to a ``.tmp`` sibling and renames into place, so a
  force-terminated worker (app closed mid-export) can no longer leave
  a truncated, unplayable WAV at the chosen path.
- **Rig health check no longer stalls TX audio (H4).**  The mid-TX rig
  ping used to run inline in the playback write loop; a wedged rigctld
  daemon blocked audio output for its full 2 s socket timeout, causing
  an audible underrun in the transmitted image.  The ping now runs on
  a dedicated monitor thread and the write loop only reads a flag.
- **Device-loss detection is now race-free (H5).**  The watchdog
  (worker thread) and the PortAudio finished callback (PortAudio
  thread) deduplicate the "device disconnected" toast via an atomic
  test-and-set instead of a bare bool — no more double toasts/stops on
  an unlucky unplug.
- **TCI: RX-subscribe state can no longer go stale (H6).**  The
  subscription flag is reset on both connect and disconnect, so a
  failed session can't make the next TX skip its audio start/stop
  bracket and leak a server-side RX stream.  Malformed binary audio
  frames are now counted and logged (first + every 100th) instead of
  dropped silently — a corrupt stream no longer looks like "RX just
  stopped".

---

## [0.3.22] — 2026-05-28

Surgical follow-up to v0.3.21.  User testing of the v0.3.21 Windows
binary revealed that the starter-template install was still being
skipped on machines with a stale ``cq.toml`` (or any other non-v0.3
``.toml``) leftover from a much older Open-SSTV install.  The
diagnostics export button shipped in v0.3.21 was used to pinpoint the
root cause within minutes — exactly what the feature was added for.

### Fixed

- **`starter_pack_installed()` now checks specifically for our 8 v0.3
  starter filenames** instead of any ``.toml`` in the user dir.  The
  pre-v0.3.22 check returned True if a user upgrading from v0.2.x
  still had stale files like ``cq.toml`` in
  ``%LOCALAPPDATA%\\open_sstv\\open_sstv\\templates\\``, which caused
  ``run_migration()`` to skip ``install_starter_pack()`` and the 8
  v0.3 starters never landed.  After v0.3.22:

    - Fresh install (empty templates dir) → install runs, all 8 ship.
    - Upgrade from v0.2.x with old ``cq.toml`` only → install runs,
      8 starters land alongside the existing ``cq.toml`` (left
      untouched).
    - User who deleted a starter intentionally → still treated as
      "installed", deletion is respected.
    - Normal post-v0.3 user → True, no-op.

  ``install_starter_pack()`` itself was already idempotent
  (``overwrite=False`` default), so the only thing that needed
  tightening was the gate.

### Tests

- New ``test_false_when_only_non_starter_toml_present`` regression
  test for the exact diagnostic scenario (single non-starter ``.toml``
  in user dir → gate must say "not installed").  Reworded the existing
  positive test to use a real starter filename.

---

## [0.3.21] — 2026-05-28

Three connected fixes plus a diagnostics-export button.  User testing
of the v0.3.20 Windows binary surfaced two bugs (templates not loading,
no way to see logs from a console-less GUI build) alongside the
already-known macOS Dock label issue.  v0.3.21 closes all three with a
single coordinated PR.

### Fixed

- **Bundled starter templates now actually install on first launch
  in PyInstaller bundles.**  The bug was latent since v0.3.0 ship.
  `_bundled_templates_dir()` used `importlib.resources.files(...) /
  "assets" / "templates"` wrapped in `as_file()` and returned the path
  from inside the `with` block — a pattern that's documented for
  *file* resources but is implementation-defined for *directories*.
  In the PyInstaller onedir bundle the returned path didn't satisfy
  `Path.exists()` checks, so `install_starter_pack` logged
  "Bundled starter template missing" for every file and copied zero
  templates.  Wheel / pipx installs happened to work by coincidence.
  Fix: switch both `_bundled_templates_dir()` and `_shipped_fonts_dir()`
  to `Path(open_sstv.__file__).parent / "assets" / "<sub>"` — works
  identically in every install layout (editable, wheel, PyInstaller
  onedir, PyInstaller `.app` BUNDLE).

- **macOS Dock label now reads "Open-SSTV".**  Three prior attempts
  (`setApplicationName` reorder in v0.3.19, `NSProcessInfo.setProcessName`
  via ctypes in v0.3.19 + v0.3.20) all failed to move the Dock label.
  User testing confirmed the actual cause: the Dock pulls from the
  launcher binary's basename for non-`.app` launches, and runtime
  process-name changes don't propagate.  The bullet-proof fix is a
  proper `.app` wrap with `CFBundleName = "Open-SSTV"` in `Info.plist`.
  Implemented via a new `BUNDLE(...)` target in `open_sstv.spec` that
  produces `dist/Open-SSTV.app/` on macOS only.  Both `CFBundleName`
  and `CFBundleDisplayName` set to "Open-SSTV".  `CFBundleVersion` /
  `CFBundleShortVersionString` read straight from `pyproject.toml`
  at spec-evaluation time so the bundle version always matches the
  release version with no second place to forget to bump.

### Added

- **Settings → Diagnostics → Export Diagnostics…** button.  Bundles
  the user's recent log file + a system-info report + their config
  (sensitive fields redacted) into a single zip via `QFileDialog`.
  Designed to make bug reports one-click instead of asking users to
  navigate to platformdirs paths or scrape terminal output.  Tested
  end-to-end on macOS; smoke output looks clean.
- **Rotating log file** at `<platformdirs.user_log_dir>/open-sstv.log`
  always activates at startup now (capped at ~6 MB via
  `RotatingFileHandler` with `backupCount=2`).  Previously logs only
  went to stderr, which is a dead handle in the Windows GUI build
  (PyInstaller `console=False`) and inconvenient to scrape on macOS
  / Linux.  The diagnostics button (above) bundles this file into the
  export zip.

### Changed

- **macOS release artifact layout.**  `open-sstv-macos-arm64.zip` now
  contains `Open-SSTV.app` (single bundle, Finder-aware) instead of
  `open-sstv/` (folder of files).  Artifact filename is unchanged so
  README links continue to work.  Codesign step in `build.yml`
  updated: bundle-level `codesign --force --sign - --options=runtime
  --deep --entitlements packaging/macos-entitlements.plist
  dist/Open-SSTV.app` after the existing dylib re-sign sweep.

- **README macOS install section.**  Replaces the ambiguous
  `xattr -cr open-sstv` instruction (which routinely got applied to
  the launcher binary instead of the folder, leaving `_internal/Python`
  quarantined and triggering "library load disallowed by system policy")
  with `xattr -cr Open-SSTV.app` (the bundle is a single
  unambiguous Finder object).  Adds note that Finder double-click
  works after the one-time quarantine clear.

### Not in this release (deferred)

- macOS Developer-ID notarization (removes the `xattr -cr` step
  entirely).  Requires Apple Developer enrollment + a separate
  workflow using `notarytool`.
- Linux source-install dock-label fix — there's no `.app` equivalent
  for source/pipx launches on Linux; the dock will continue to show
  "python" in those cases.  Not a regression.

---

## [0.3.20] — 2026-05-28

**Critical packaging fix** plus a follow-up macOS process-name attempt.
User testing on the v0.3.19 Windows binary surfaced that templates
were failing to render with "Fallback font not found at …\\_internal\\
open_sstv\\assets\\fonts\\DejaVuSans-Bold.ttf" — the bundled fonts,
starter templates, app-icon PNG, and TX-panel default photo had
*never* been included in any PyInstaller release since v0.3.0.  The
H-6 fallback added in v0.3.15 is what made the bug visible; the
underlying packaging defect was older.

### Fixed

- **PyInstaller spec now bundles `src/open_sstv/assets/`.**  Added
  `datas += collect_data_files("open_sstv")` in `open_sstv.spec`.
  Definitively fixes:
    - Template render failures on every platform (missing fonts).
    - Generic Windows taskbar icon (the runtime
      `setWindowIcon(QIcon(PNG))` call was silently failing because
      the PNG wasn't in the bundle — the `.ico` embedded in the
      `.exe` separately is unaffected and continues to show in
      Explorer).
    - Generic AppImage launcher icon for users who launch from the
      `.AppImage` rather than via the `.desktop` entry.
    - Missing default TX-panel photo (had been falling back to an
      empty canvas).
  Wheel / `pipx` installs were never affected — those got the assets
  via hatch's `packages = ["src/open_sstv"]`.  This was a
  PyInstaller-only bug.

- **macOS Dock label** now re-applied *after* `QApplication()`
  construction in addition to the v0.3.19 pre-construction call.
  Qt's NSApplication init during `QApplication()` was silently
  resetting the process name back to whatever macOS derives from
  the embedded `Python.framework`'s `CFBundleName` (which is
  `"Python"`).  The v0.3.19 pre-call was correct in isolation but
  was being overwritten by Qt one frame later.  The post-call is
  what actually sticks for the Dock.  If the Dock still reads
  "Python" after v0.3.20, the next fix is the deferred `.app`
  BUNDLE wrap (tracked as v0.3.21).

### Added

- **Startup asset sanity check** in `app.py` that verifies the
  critical bundled files exist and emits a clear log warning +
  status-bar message if any are missing.  Non-blocking — the app
  still launches with whatever's available — but a future
  PyInstaller-bundling regression now produces a one-line
  diagnostic at startup instead of mysteriously-broken template
  renders.  Catches: missing DejaVu Sans Bold font, missing app
  icon PNG, missing TX default photo.

### Docs

- README status banner bumped v0.3.19 → v0.3.20 per the (now-
  established) release-prep checklist.

---

## [0.3.19] — 2026-05-27

App-name fix across all operating systems.  Open-SSTV used to render as
"python" / "python.exe" in dock tooltips, taskbar hovers, and process
lists when launched via the `open-sstv` console script (source / pipx
installs) — the actual executable is the venv's Python interpreter,
and Qt's `setApplicationName` was being called too late for the
platform window systems to pick it up.

### Fixed

- **Cross-OS process-name fix** in `src/open_sstv/app.py`:
  - **All OSes**: `sys.argv[0]` overridden to `"Open-SSTV"` very early
    in `main()`; some platforms (notably Linux X11 `WM_CLASS`) sniff
    `argv[0]` for the application name.
  - **All OSes**: `QCoreApplication.setApplicationName()` /
    `setApplicationVersion()` / `setOrganizationName()` /
    `setOrganizationDomain()` moved to *before* `QApplication()` is
    constructed.  Qt's docs are explicit that calling these after
    construction "may not propagate properly to the platform's window
    system" — which is exactly why the dock/taskbar tooltips ignored
    them in v0.3.18 and earlier.
  - **Linux** (Wayland): `app.setDesktopFileName("open-sstv")` so the
    compositor matches the running window to the AppImage's
    `open-sstv.desktop` entry for the taskbar tooltip and icon.
  - **macOS**: `-[NSProcessInfo setProcessName:]` via `ctypes` against
    `libobjc.dylib` so the Dock tooltip reads "Open-SSTV" instead of
    "Python".  Verified locally: process name flips `b'Python'` →
    `b'Open-SSTV'` after the call.  No new dependencies (`libobjc` is
    always present on macOS; `pyobjc` not required).
  - **Windows**: already covered by the v0.3.18 AppUserModelID +
    `.exe` icon embed — no change needed.

### Docs

- **README status banner** bumped v0.3.13 → v0.3.19 — five releases
  of drift caught.  The matching memory recipe has been updated
  locally so README banner verification is now part of the
  release-prep checklist alongside pyproject + CHANGELOG.

### Failure modes

Every layer is wrapped in `try/except` so a missing ctypes API or an
ABI change can't block the GUI launch — losing the friendly name is
cosmetic, never fatal.

---

## [0.3.18] — 2026-05-27

App icon ships end-to-end.  No functional changes — Open-SSTV stops
appearing as a generic Python window in title bars, the Windows
taskbar, and the Linux window-manager hint area.

### Added

- **Bundled app icon** at `src/open_sstv/assets/icons/Open-SSTV.png`
  (1024×1024 RGBA).  Copied from the long-standing
  `assets/Open-SSTV-icon.png` so it ships inside the wheel and is
  resolvable via `importlib.resources` from pipx, source checkouts,
  and the PyInstaller bundle alike.
- **Multi-resolution Windows `.ico`** at
  `src/open_sstv/assets/icons/Open-SSTV.ico` (sizes 16/32/48/64/128/
  256).  Generated from the same source PNG via Pillow.  Embedded
  into the `.exe` rsrc section by PyInstaller so Windows Explorer
  shows the right icon even when Open-SSTV isn't running.
- **Runtime `setWindowIcon`** in `app.py` immediately after
  `QApplication` construction.  Propagates to every window via Qt's
  default-icon mechanism.
- **Windows `AppUserModelID`** (`github.bucknova.OpenSSTV`) set via
  `SetCurrentProcessExplicitAppUserModelID` before `QApplication`
  construction.  Without this the Windows taskbar groups
  Open-SSTV under the Python interpreter's AppUserModelID and the
  tooltip reads "python.exe".

### Where the icon shows up

| OS | Where it appears now |
|---|---|
| Windows | `.exe` icon in Explorer + taskbar + window title bar |
| Linux (AppImage) | `.desktop` entry, taskbar, window manager (already worked; runtime icon now also applies inside the window) |
| Linux (pipx / zip) | Window title bar + window-manager taskbar hint |
| macOS | Qt window title bar.  **Dock icon stays generic** for the onedir bundle — fixing that requires re-spec'ing as a PyInstaller `BUNDLE` target with an `.icns` file (tracked as a follow-up). |

### Not in this release

- macOS `.icns` + `BUNDLE` target restructure → real dock icon on
  macOS.  Larger change because it touches the release pipeline's
  codesign re-pass step.

---

## [0.3.17] — 2026-05-22

Third audit follow-up release.  Closes the actionable Low-severity items
and the one outstanding Minimal item from the v0.2.9 stability audit.
One user-visible TX-side feature (per-instance test-tone frequencies);
everything else is polish, docs, or metadata.

### Added

- **L-2 — Settings entry for two-tone test frequencies.**  The SSB
  linearity test signal was hard-coded to the ARRL twin-tone standard
  (700 + 1900 Hz).  Two new spinboxes on the Audio tab beneath the
  Test Tone button let operators choose values inside any reasonable
  SSB passband.  Range [300, 3000] Hz, validated and re-ordered in
  `AppConfig.__post_init__`, applied to the running `TxWorker` without
  a restart.  Defaults unchanged — ARRL standard for everyone who
  hasn't touched the new fields.

### Fixed

- **L-3 — Per-instance rig name labels.**  `SerialPttRig` /
  `IcomCIVRig` / `KenwoodRig` / `YaesuRig` used a class-attribute
  `name`, so two instances on different ports rendered as the same
  label in error messages and the (future) rig picker.  Promoted to
  a `@property` that interpolates the serial port:
  `"Icom CI-V (COM3)"`, `"Yaesu CAT (/dev/cu.usbserial-1410)"`, etc.
  `RigctldClient` and `TciRig` already followed this pattern.
- **L-4 — TCI recv-thread leak now visible.**  `TciConnection.disconnect()`
  joins the recv thread with a 2 s timeout to avoid hanging the GUI on
  a wedged WebSocket; the thread is daemon=True so a leak doesn't keep
  the interpreter alive.  Previously a leak was *silent*; now it
  produces a WARNING log so a hung session via `OPEN_SSTV_DEBUG=1` is
  immediately diagnostic.

### Documented

- **N-1 — PIL import side-effect in `open_sstv/__init__.py`.**  Added a
  paragraph explaining that `apply_pil_security_limits()` is called at
  package import on purpose and that removing it would disarm every
  entry point (GUI, CLI encoder/decoder, tests), not just the GUI.

### Metadata

- **L-7 — Author email in pyproject.toml.**  Replaced empty string
  (which made PyPI emit an upload warning) with
  `bucknova@users.noreply.github.com` — GitHub's noreply alias is
  package-page-visible without exposing a personal address.

### Audit Low items not in this release

- **L-1** — verified clean (`.claude/` files were never tracked).
- **L-5** — non-issue per the audit (`ModuleNotFoundError` is a subclass
  of `ImportError`; the existing fallback works as intended).
- **L-6 / L-8** — already validated by the three successful release
  pipelines since PR #7.

### Minimal items not in this release

- **N-2** — already closed in v0.3.14.
- **N-3** — PyPI lowercases automatically; no action.
- **N-4** — `CODEOWNERS` / `SECURITY.md` deferred until promotion
  beyond beta.

### Audit follow-ups still open after v0.3.17

- mypy strict-mode cleanup (~196 errors) → CI gate
- ruff E/B/SIM cleanup (~151 manual fixes) → widen CI ruff selection
- GUI marker via Xvfb on Linux
- Integration marker against fake rigctld
- M-4 (per-frame `ndarray.copy()`) — graded as known cost; revisit if
  drop-rate measurements demand it
- M-13 (mypy CI gate) — waits on the strict-mode cleanup above

---

## [0.3.16] — 2026-05-22

Second audit follow-up release.  Closes seven of the eight unresolved
Medium-severity findings from the v0.2.9 stability audit (M-4 is left
as a known cost per the audit's own framing; M-13 mypy CI gating
still needs the strict-mode error cleanup).

### Fixed

- **M-1 — `SO_KEEPALIVE` on rigctld TCP socket.**  Half-open
  connections (laptop sleep/resume, daemon crash, network partition)
  now get detected via OS-level keepalive instead of waiting for the
  next command's recv timeout.  Normalises behaviour between Windows
  (which tears down half-open sockets faster by default) and Linux/
  macOS.  Graceful degradation when SO_KEEPALIVE isn't available
  (containers without CAP_NET_ADMIN, exotic socket implementations).
- **M-2 — CI-V `_read_response` busy-poll replaced.**  Previously the
  Icom CAT response reader polled `in_waiting` every 10 ms via
  `time.sleep(0.01)` while holding the serial lock.  Now uses a
  short-timeout blocking `read` so the OS schedules the wait — same
  worst-case responsiveness, no Python wake-up cost, GIL released
  during the blocking syscall.  User-configured `ser.timeout`
  snapshot+restored via try/finally so the function has no side
  effect on the next caller.
- **M-3 — Waterfall slots warn on cross-thread invocation.**  Adds a
  `_check_gui_thread()` guard at the top of `add_rx_column` /
  `add_tx_column` that logs a `WARNING` if the slot fires off the
  GUI thread.  Codifies the invisible "queued-signal-only" contract
  protecting `_buf` / `_cursor` / `_overlap` without an explicit lock.
- **M-6 — Stale template `.tmp` sweep.**  Extends the v0.3.15 H-5
  config-tmp cleanup pattern to the templates directory.  `list_templates`
  now opportunistically removes `*.toml.tmp` orphans left by a
  SIGKILL between `tomli_w.dump` and `os.replace`.
- **M-7 — TOML serializer helper for future Path/Enum config fields.**
  `_serialize_for_toml(value)` in `config/store.py` recursively
  converts `pathlib.Path → str`, `enum.Enum → value`, and
  `list/tuple/dict` containers in place.  No-op on the current
  AppConfig (all primitives); prevents a future `images_save_dir:
  Path` or `default_tx_mode: Mode` refactor from crashing
  `save_config` with `TypeError`.
- **M-8 — `update_checker` 6-hour backoff against rate-limited
  corporate NATs.**  Sidecar timestamp file at
  `platformdirs.user_cache_dir("open_sstv") / "last_update_check"`
  caches the last *successful* check.  Subsequent launches within
  the window skip the network round-trip.  Persisted on
  structurally-valid GitHub responses (including rate-limit JSON,
  intentionally — hammering won't help) but *not* on transport
  errors (so a user retrying at a wifi boundary still gets a fresh
  attempt).  No new AppConfig field — transient runtime cache.
- **M-9 — CLI decode `-o` is now optional.**  `open-sstv-decode foo.wav`
  used to exit 2 from argparse, forcing the user to add `-o out.png`
  which then landed in the current working directory (the source of
  the `.gitignore` `sstv_*.png` rule).  Default is now
  `<platformdirs.user_pictures_dir()>/Open-SSTV/sstv_<mode>_<utc-ts>.png`
  — mirrors the GUI auto-save policy.  Directory is created on demand.

### Audit M-class items not in this release

- **M-4** (per-frame `ndarray.copy()` in audio callback) — graded by
  the audit as a known cost, not a regression.  Revisit only if
  real-world measurements show the drop rate on long PD modes is
  unacceptable.  Avoiding the copy needs a pre-allocated ring buffer
  with non-trivial sync against queue consumption.
- **M-5** — already closed in v0.3.14 (pinned appimagetool).
- **M-10 / M-11 / M-12** — already closed in PR #7 (gitignore +
  repo hygiene).
- **M-13** — mypy strict-mode CI gating.  Waits on the 196 errors
  the v0.3.15 parser fix exposed.  Separate PR.

### Audit follow-ups still open

- mypy strict-mode cleanup → CI gate
- ruff E-class / B-class / SIM-class cleanup (~151 manual fixes) →
  widen CI selection from `--select=I,F,UP,W`
- GUI marker via Xvfb on Linux
- Integration marker against fake rigctld

---

## [0.3.15] — 2026-05-22

Audit follow-up release closing every High-severity finding from the
v0.2.9 stability audit plus the CI gate-debt items flagged alongside.
No user-visible behaviour changes on the happy path — everything in
this release is defence-in-depth, documentation, or CI infrastructure.

### Fixed

- **H-1 — defensive ``sd._terminate`` / ``sd._initialize`` calls.**
  Split the broad ``except Exception`` around the sounddevice private
  re-init API into an explicit ``AttributeError`` branch with an
  upgrade-or-pin log message, so a future sounddevice version that
  drops the underscored symbols degrades to a logged warning rather
  than a silent failure mode buried in the broader catch.
- **H-2 — RX device-loss ``stream_error`` dedupe.**  The watchdog
  timer and the PortAudio finished_callback could both fire on the
  same USB unplug, producing duplicate "Audio device disconnected"
  toasts.  Added ``_device_loss_emitted`` flag set by whichever path
  fires first; the other short-circuits.
- **H-4 — Windows wide-char WAV paths.**  Four ``wave.open(str(path))``
  call sites (Export to Audio writer, offline TX worker writer, CLI
  encoder writer, WAV file loader) now use ``path.open(mode)`` +
  ``wave.open(f, mode)`` so non-ASCII characters in save paths survive
  the Windows ANSI code page on non-UTF-8 locales.
- **H-5 — config tmp-file cleanup.**  ``load_config`` now removes any
  ``<config>.toml.tmp`` left behind by a SIGKILL between
  ``tomli_w.dump`` and ``os.replace`` in a prior save.  Failure to
  unlink is logged at debug; the load proceeds regardless.
- **H-6 — font-load fallback.**  ``renderer._load_font`` now wraps
  ``PIL.ImageFont.truetype`` in a try/except OSError and falls back to
  the bundled DejaVu Sans Bold on failure.  A corrupted file in the
  user fonts directory no longer crashes the entire TX render path.
- **H-8 — bounded filename-collision search.**  ``_resolve_collision``
  previously walked up to 999 sequential ``stat()`` calls — fine on
  SSD, painful on a slow network share (tens of seconds of GUI freeze).
  Cap at 10 sequential trials, then switch to a 6-hex-char random
  suffix from ``secrets.token_hex``.  16.7 M unique suffixes per stem.
  Absolute worst case is now 60 stat calls (vs. 999 before).

### Documented

- **H-3 — ``output_stream.stop()`` cross-thread semantics.**  ``stop()``
  is called from the GUI thread while ``stream.write()`` runs on the
  TX worker thread; PortAudio's ``Pa_AbortStream`` is not portably
  documented as cross-thread safe.  Recorded the empirical behaviour
  per OS (macOS / Linux / Windows WASAPI safe in practice; the WDM-KS
  output-device filter at ``audio/devices.py`` mitigates the historic
  Windows sharp edge) and the audit's trade-off rationale.
- **H-7 — process-global ``_pa_reset`` warning.**  ``sd._terminate``
  + ``sd._initialize`` is a process-wide PortAudio operation and the
  Open-SSTV TX/RX interlock only sees Open-SSTV's own activity.
  Embedders who import the package alongside other PortAudio users
  may see their unrelated streams invalidated by a device-loss
  recovery here.  Warning added so the package's behaviour is clear
  outside the standalone-app case.
- **H-9 — macOS Intel install guidance.**  Universal2 isn't viable
  because PySide6 ships per-arch wheels (no fat upstream wheel to
  merge against), and GitHub retired the ``macos-13`` Intel runner
  pool — so neither option-(a) nor option-(b) from the audit is
  available.  README now points Intel Mac users at ``pipx install
  open-sstv`` and the cross-platform bullet documents the limitation.

### CI / repo

- **mypy parser blocker resolved.**  ``src/open_sstv/radio/tci.py:296``
  contained a ``# type: TX_AUDIO`` comment that mypy parsed as a PEP
  484 type-comment annotation, aborting the strict run.  Renamed to
  ``# msg_type:`` so the file parses cleanly.  Full mypy strict mode
  still reports 196 errors across 33 files (mostly unused
  ``type: ignore`` comments and None-narrowing); adding mypy to CI
  waits for a follow-up.
- **ruff applied: 116 safe auto-fixes across 37 files.**  Pure
  mechanical: import sort, unused-import removal, pyupgrade typing,
  trivial SIM/B fixes.  No logic changes.  Remaining 151 manual
  fixes (E402, E501, SIM105, etc.) deferred to a follow-up.
- **ruff added to CI as a hard gate.**  ``test.yml`` now runs
  ``ruff check src tests --select=I,F,UP,W`` before the test job.
  The curated selection is the subset that's fully clean today.
  Widening to E / B / SIM is a separate PR once the manual cleanup
  lands.
- **F-class cleanup in tests.**  Six ``F821`` / ``F841`` reports were
  legitimate forward-annotation bugs (``Callable`` in
  ``ui/workers.py``; ``KenwoodRig`` / ``YaesuRig`` in
  ``test_serial_rig.py``) and dead intermediate bindings in five
  test files.  All fixed inline so the new ruff gate passes.

### Audit follow-ups not in this release

- mypy CI gating (waits on the 196 strict-mode errors being addressed)
- ruff E-class / B-class / SIM-class cleanup (151 manual fixes)
- GUI marker via Xvfb on Linux for the test workflow
- Integration marker against fake rigctld
- Medium-severity audit items (next PR)

---

## [0.3.14] — 2026-05-21

CI / release-pipeline hardening release.  No functional code changes.

Driven by a full-repo stability audit that flagged two Critical issues
with the existing CI:

1. The release workflow only ran ``pyinstaller`` and never invoked
   ``pytest`` — a tagged binary could ship a regression with nothing
   blocking it.
2. CI exercised Python 3.11 only, while ``pyproject.toml`` advertises
   ``>=3.11`` and classifies 3.12 (and now 3.13).  3.12/3.13 regressions
   reached users before being caught.

This release closes both findings and bundles a handful of nearby
hygiene fixes.

### Added

- **``tests`` workflow.**  New ``.github/workflows/test.yml`` is a
  reusable workflow that runs ``pytest -m "not gui and not
  integration"`` across the full supported matrix — Python 3.11 / 3.12
  / 3.13 × Ubuntu 22.04 / Windows / macOS 14.  Triggers on push,
  pull_request, manual dispatch, and ``workflow_call`` so the release
  pipeline reuses it.  Linux runners install ``libportaudio2`` +
  ``libsndfile1`` because the sounddevice and soundfile wheels there
  are thin ctypes shims.
- **Release builds now gate on a green matrix.**  ``build.yml`` invokes
  ``test.yml`` via ``workflow_call`` as a leading ``test`` job and the
  existing ``build`` job has ``needs: test`` — a tag push can no longer
  produce binaries if any matrix cell is red.
- **Pinned ``appimagetool``.**  The AppImage step previously pulled from
  the ``continuous`` channel, making every release vulnerable to an
  upstream regression at the exact moment we cut a tag.  Now pinned to
  ``1.9.1`` with per-arch sha256 verification.
- **Concurrency groups** on both workflows.  Branch pushes coalesce
  in-flight test runs to save CI minutes; tag refs never cancel an
  in-progress release upload.
- **Tests status badge** in the README byline.
- **Classifiers** for Python 3.13 and Microsoft Windows in
  ``pyproject.toml`` — both already shipping, just unannounced in the
  metadata.

### Changed

- **``.gitignore``.**  Added ``Open-SSTV/`` (a stale v0.2.0 snapshot
  sometimes left behind by tooling — a future ``git add -A`` could
  have accidentally committed gigabytes), ``/testimage.jpg`` (scratch
  file at repo root; the bundled
  ``src/open_sstv/assets/testimage.jpg`` is unaffected because the
  pattern is anchored to the repo root), and ``docs/design/*.png``
  (working mmsstv reference screenshots that are not part of the
  published docs site).

### Audit follow-ups not in this release

Still open from the audit and tracked for subsequent PRs:

- ``ruff check`` reports 266 issues (114 auto-fixable) across the tree;
  gating CI on lint would dominate the diff and is its own cleanup.
- ``mypy`` aborts on a parser issue at ``src/open_sstv/radio/tci.py:296``
  before it can check anything; needs investigation before gating.
- GUI-marker tests need Xvfb on Linux to run in CI; ``integration``
  marker needs a fake-rigctld fixture in CI.
- Lower-severity audit items (PortAudio reset wrapping, rigctld
  ``SO_KEEPALIVE``, Windows wide-char WAV paths, font-load fallback,
  filename-collision random suffix, Universal2 macOS build) are
  individually tracked.

---

## [0.3.13] — 2026-05-20

Policy change: the TX banner stamp is now always applied when
``tx_banner_enabled`` is True in Settings, regardless of whether a
v0.3 template is selected.  Previously the banner was skipped on
templated transmissions to avoid double-stamping over the template's
own header text — per user feedback (Kevin/W0AEZ): banner-on means
banner-always-on, with the operator responsible for choosing between
banner and template by toggling the Settings checkbox.

Applies to both the live-TX path (``TxWorker.transmit``) and the
offline Export to Audio path
(``MainWindow._on_export_to_audio_requested``) so the two stay
symmetric.

### Changed

- **Banner gating now obeys only ``tx_banner_enabled``.**  Removed
  the previously-coupled ``not v3_template_active`` check from both
  the live-TX path and the Export to Audio path.  Operators who
  want template-only output (no banner strip) disable the banner in
  Settings → TX.
- **TxWorker no longer tracks template-active state.**  Removed the
  ``_v3_template_active`` field and the ``set_v3_template_active``
  slot from ``TxWorker``.  The corresponding
  ``MainWindow`` → ``TxWorker`` connection on
  ``TxPanel.template_composited`` is gone too.  The signal itself
  remains on ``TxPanel`` in case future code wants to listen to
  template selection changes.
- **Removed v0.3.12's ``TxPanel.has_v3_template_composited()`` helper**
  — added one version ago to support the now-removed gating, it has
  no remaining caller.

### Testing

- ``tests/ui/test_v3_tx_integration.py::TestTxWorkerV3Flag``
  removed (4 tests) and replaced with
  ``TestTxWorkerBannerPolicy`` (2 tests): banner stamps when
  enabled regardless of template state, and the ``_v3_template_active``
  field / ``set_v3_template_active`` slot are confirmed absent from
  the worker so a regression that re-introduces template-aware
  gating fails this test.
- ``tests/ui/test_main_window.py::TestExportToAudioBanner``: the
  v0.3.12 ``test_banner_skipped_when_v3_template_composited`` is
  replaced with ``test_banner_applied_regardless_of_template_state``,
  pinning the new policy.

---

## [0.3.12] — 2026-05-20

Bundle of two independent post-v0.3.11 fixes:

1. **Template gallery thumbnails stayed stale on inactive QSO-role
   tabs after Load Image** — the gallery's role-filter optimisation
   skipped hidden cards on re-render, so non-active tabs held onto
   the previously-loaded image.
2. **TX banner strip was missing from Export to Audio output** — the
   new offline encode path (added in v0.3.10) bypassed
   ``TxWorker.transmit`` and therefore the banner-stamp step that
   the live-TX path applies.

### Fixed

- **Template gallery thumbs stale on inactive role tabs after Load
  Image** (reported by Kevin / W0AEZ): ``TemplateGallery._rerender_all``
  used to skip ``not card.isVisible()`` cards as a perceived
  performance optimisation, so any card hidden by the current role
  filter held onto whatever photo / QSO state / mode was active the
  last time that filter was shown.  Repro: launch (default image
  auto-loads on CQ), Load Image to swap photo on CQ, switch to Reply
  → Reply thumbs still showed the old image.  Fix: render every card
  on every ``set_photo`` / ``set_qso_state`` / ``set_rx_image`` /
  ``set_mode``, regardless of role-filter visibility.  Cost is ~ms
  per thumb on a modern CPU — imperceptible for the typical 8-card
  starter pack.  Regression covered by
  ``test_set_photo_rerenders_role_filter_hidden_cards``.
- **TX banner missing from Export to Audio output** (regression
  introduced in v0.3.10, reported by Kevin/W0AEZ after a manual
  encode-then-decode round-trip showed no banner at the top of the
  decoded image): the new in-panel Export to Audio button bypasses
  ``TxWorker`` entirely (it goes straight to ``OfflineEncodeWorker``)
  so the banner stamp at ``ui/workers.py:606`` never ran.  Fix:
  apply ``apply_tx_banner`` in
  ``MainWindow._on_export_to_audio_requested`` before handing the
  image to the offline worker, with the same gating rule TxWorker
  uses — banner only when ``tx_banner_enabled`` AND no v0.3
  template composited (templates carry their own text overlays;
  double-stamping would clobber them).  No risk of double-stamping
  via the live-TX path because ``TxWorker.transmit`` still does its
  own banner application, and that path is unchanged.

### Added

- **New ``TxPanel.has_v3_template_composited()`` method** —
  exposes the same template-active state TxPanel already emits via
  ``template_composited(bool)``, so MainWindow can check it
  synchronously without storing duplicate state.

### Testing

- ``test_set_photo_rerenders_role_filter_hidden_cards`` in
  ``tests/ui/test_template_gallery.py`` — applies a "cq" role filter
  that hides two of three cards, then asserts every card (visible
  and hidden) is re-rendered on ``set_photo``.
- Three new tests in
  ``tests/ui/test_main_window.py::TestExportToAudioBanner``:
  banner applied when enabled with no template, banner skipped when
  disabled, banner skipped when a v0.3 template is composited.
  Uses stub ``OfflineEncodeWorker`` and ``QThread`` so the
  assertion logic runs without a real encode.

---

## [0.3.11] — 2026-05-20

Patch on top of v0.3.10 to drain the offline encode/decode worker
threads on window close.  Without the drain Qt aborts the process
with ``QThread: Destroyed while thread is still running`` when
MainWindow's destructor (or ``PySide::destroyQCoreApplication`` at
Python shutdown) walks its child QObjects and finds a still-running
``OfflineEncodeWorker`` / ``OfflineDecodeWorker`` thread.

### Fixed

- **closeEvent QThread shutdown race** (regression introduced in
  v0.3.10, reported via macOS crash report on
  ``PySide::destroyQCoreApplication`` → ``QObjectPrivate::deleteChildren``
  → ``QThread::~QThread()`` → ``qFatal``): the new offline encode and
  decode worker threads are parented to MainWindow
  (``QThread(self)``), and if either is still running when the
  window's destructor walks its child list, Qt aborts the process.
  Fix: new ``_abort_offline_workers()`` helper called from
  ``closeEvent`` right after ``_abort_connect()`` (mirroring the
  ``_RigConnectWorker`` shutdown drain).  Three-stage drain:
  ``thread.quit()`` → ``thread.wait(10_000)`` → as a last resort,
  ``thread.terminate() + wait(1000)``.  10 s covers every mode
  except a mid-encode Pasokon P7; in that edge case terminate kicks
  in so we get a slightly ugly process exit instead of ``qFatal``.

---

## [0.3.10] — 2026-05-20

Fixes the silent-failure offline encode bug introduced in v0.3.9, moves
the encode / decode surfaces out of the File menu and onto in-panel
buttons, and reuses the live TX-panel composite so exported WAVs match
exactly what would have been transmitted (template + photo + QSO
overlays, not a separately-picked image).

### Added

- **TX panel "Export to Audio" button** — sits directly below the
  Transmit button.  Uses the same composited image Transmit would
  emit, so the WAV contains exactly what would have gone over the
  air (template + photo + QSO state).  Single mode picker (the
  panel's existing one), single image picker (the panel's existing
  one) — no parallel mode/image dialogs from v0.3.9's File-menu
  flow.  Mirrors Transmit's enable state: disabled while live TX is
  in flight so a mid-TX click can't race the live encoder.
- **RX panel "Decode Audio" button** — sits on a row below
  Start/Stop/Save.  Opens a WAV/FLAC file picker; decoded image
  lands in the gallery exactly like a live decode.  Always
  enabled, including during live capture (results interleave in
  the same gallery and are unambiguous from the per-thumb
  metadata).
- **Regression test** ``tests/ui/test_offline_worker_threaded.py``
  — exercises the queued-invoke + cross-thread path that broke in
  v0.3.9.  Reproduces the production launch shape (worker held as
  an instance attribute, ``QMetaObject.invokeMethod`` with
  ``QueuedConnection``) and asserts ``encode_complete`` /
  ``image_complete`` actually fires.  A regression where the
  worker is GC'd before its slot runs causes this test to time out.

### Fixed

- **Offline encode/decode worker GC race** (regression introduced
  in v0.3.9, user-reported by Kevin/W0AEZ): the File-menu encode
  action showed "Encoding…" in the status bar and then silently
  did nothing — no WAV file, no error message, no signal emission.
  Root cause: ``OfflineEncodeWorker()`` / ``OfflineDecodeWorker()``
  were constructed as *local* variables inside the slot
  function.  PySide6 signal connections hold only a *weak*
  reference to the receiver QObject, so as soon as the slot
  returned, Python dropped the only strong reference and the
  worker was garbage-collected — before Qt could dispatch the
  queued ``encode()`` invocation.  Fix: store both worker and
  thread as instance attributes
  (``self._offline_encode_thread`` /
  ``self._offline_encode_worker`` and the matching pair for
  decode), following the existing ``_RigConnectWorker`` pattern.
  Cleanup happens on ``thread.finished`` via dedicated handler
  slots that null the attributes once the operation completes.
  Re-entrant clicks while an encode/decode is in flight are now
  dropped with a status-bar message instead of stacking workers
  on top of each other.

### Changed

- **Offline encode/decode moved from File menu to in-panel buttons.**
  The v0.3.9 ``File → Encode Image to Audio…`` and
  ``File → Decode Audio File…`` menu items are gone; the new
  buttons on the TX and RX panels take their place.  Single
  discovery path through the panels, no parallel menu surface to
  keep in sync.
- **OfflineEncodeWorker now takes a ``PIL.Image`` directly** via a
  new ``encode_from_image(image, mode, sample_rate, output_path)``
  slot.  The v0.3.9 ``encode(image_path, …)`` slot is removed —
  the GUI never needs a path-based encode now that the TX panel
  always has the composited image on hand.  Side effect: removes
  the "did the user pick the right image / mode / template combo?"
  question entirely, because the WAV is now built from the same
  preview pixels the user already sees in the TX panel.
- **TX panel ``_compose_for_emit()`` helper** factored out of
  ``_on_transmit_clicked`` so the new ``_on_export_clicked``
  reuses identical composite logic.  Keeps the two click handlers
  from drifting apart.

### Testing

- Added ``tests/ui/test_offline_worker_threaded.py`` (2 tests):
  the regression check described above for both encode and decode
  workers under the actual queued-invocation + cross-thread launch
  path.
- Added ``tests/ui/test_rx_panel.py`` (4 tests): Start, Clear, and
  Decode Audio button → signal wiring; verifies Decode Audio
  stays enabled across capture state transitions.
- Updated ``tests/ui/test_offline_workers.py`` to call
  ``encode_from_image(pil_image, …)`` instead of the removed
  ``encode(path, …)`` slot.  Three encode tests now (removed two
  path-coupled ones that no longer apply; added one OSError-write
  case to cover the WAV-write error branch we kept).
- Added two tests to ``tests/ui/test_tx_panel.py`` covering the
  new Export to Audio button: signal emission on click, and
  enable-state mirroring with the Transmit button across
  ``set_transmitting(True/False)``.

---

## [0.3.9] — 2026-05-20

Brings the CLI encode / decode capabilities into the GUI as menu
actions and skips a Windows-incompatible test that was producing
spurious failures for non-Mac contributors.

### Added

- **File → Encode Image to Audio…** — opens a file dialog for an
  image, lets the user pick from the 22 SSTV modes (initial selection
  = current default TX mode), then opens a second dialog for the
  output WAV path.  Encodes off the GUI thread on a one-shot
  ``OfflineEncodeWorker`` so a Pasokon P7 encode doesn't hitch the
  UI.  Status bar reports the duration and mode of the written
  file; errors surface via a QMessageBox.  Same job as
  ``open-sstv-encode`` but accessible without dropping to a shell.
- **File → Decode Audio File…** — opens a file dialog for a
  ``.wav`` or ``.flac`` audio file, decodes off-thread on a
  ``OfflineDecodeWorker``, and routes the resulting image into the
  gallery via the same ``_on_rx_image_complete`` path used for live
  RX.  Status bar confirms the mode + VIS code on success; errors
  ("No SSTV header found", "unsupported format", etc.) appear in the
  status bar.  Closes the saved-WAV → re-decode loop that the RX
  audio recording feature (v0.3.6) opened up.

### Changed

- **WAV/FLAC loading consolidated.**  The ``_read_wav`` helper that
  used to live inside ``cli/decode.py`` is now ``load_audio_file``
  in a new ``audio/file_io.py`` module, shared between the CLI and
  the new GUI offline-decode worker.  FLAC support is included via
  the optional ``soundfile`` dep (the same ``[flac]`` extra that
  backs RX audio recording); WAV continues to work without any
  optional deps.  ``_read_wav`` is preserved as a thin shim so any
  external callers don't break.

### Testing

- **Windows ``termios`` tests now skipif-guarded.**  The
  ``TestTermiosErrorWrapping`` class in ``tests/radio/test_serial_rig.py``
  imports ``termios`` inside each test body — a Unix-only stdlib
  module — which raised ``ModuleNotFoundError`` on Windows runs and
  showed up as a failure.  Wrapped the class with
  ``@pytest.mark.skipif(sys.platform == "win32")`` so it's skipped
  cleanly on Windows; pyserial uses ``SerialException`` there which
  the parallel ``TestOSErrorWrapping`` class already covers.  The
  ``OSError``-wrapping paths around ``termios.error`` still get
  exercised on macOS / Linux where the import succeeds.
- **17 new tests** covering the offline workers and the new
  ``load_audio_file`` shared helper:
  - 8 tests for ``audio/file_io.py`` — missing file, unsupported
    extension, 8/16-bit / unsupported WAV sample widths, stereo
    downmix length-check, FLAC round-trip (gated on ``soundfile``),
    FLAC ``ImportError`` when soundfile is unavailable.
  - 9 tests for ``ui/offline_workers.py`` — valid WAV decode round
    trip, missing-file / unsupported-extension / no-VIS / empty-WAV
    error paths, encode produces correct WAV format (channels /
    width / sample-rate), missing / invalid image error paths,
    encode creates parent directories.

---

## [0.3.8] — 2026-05-20

Weak-signal RX improvements: configurable QSB watchdog, better VIS
detection on fading signals, and a small DSP tweak to reduce colour
noise on weak decodes.  All landed via PR #6 from @nreed97 on top of
the v0.3.7 audit base.

### Added

- **Configurable RX no-progress watchdog timeout.**  Settings →
  Receive → "No-progress timeout" (range 5–300 s, default 5 s).
  The watchdog was previously hard-wired at 5 s, terminating any
  in-progress decode that hit a QSB fade longer than that even
  though ``walk_sync_grid`` already bridges sync gaps with predicted
  positions and can resume when audio returns.  Wired via a queued
  ``Signal(int)`` on ``MainWindow`` and a ``set_watchdog_timeout``
  slot on ``RxWorker`` so the value takes effect live without a
  restart.  Hand-edited TOML values outside ``[5, 300]`` are clamped
  in ``AppConfig.__post_init__`` with an ``INFO`` log, matching the
  v0.3.7 M3 pattern for ``autosave_file_format`` / ``rx_audio_format``.

### Changed

- **Weak-signal VIS detection** uses a separate, wider smooth for
  the leader-fraction check.  The 2 ms IF smooth that gates bit
  classification was previously also used for leader detection,
  leaving too much per-sample noise in the leader estimate; the
  40 % fraction threshold then rejected legitimate weak signals
  that were audible and visible on the waterfall.  A new 20 ms
  smooth (≈10× noise reduction) is used exclusively for the leader
  presence check, leaving bit-edge timing unaffected.  Threshold
  relaxations rolling off the noise reduction:
  - Normal-mode leader threshold ``0.40 → 0.35``
  - Weak-signal leader threshold ``0.25 → 0.20``
  - Normal-mode minimum start-bit duration ``20 ms → 17 ms`` (still
    7 ms above the 10 ms mid-leader break) to tolerate
    noise-fragmented start bits.

### Fixed

- **3-sample IF pre-smooth on the incremental decoder** reduces
  weak-signal colour noise variance by ``√3 ≈ 1.7×``, compounding
  with the existing per-pixel central-60 % median to give visibly
  cleaner images on signals below ~10 dB in-band SNR.  3 samples
  is ≤25 % of the narrowest pixel span across all supported modes
  (Robot 36 luma at 44.1 kHz ≈ 12 samples/pixel), so strong-signal
  sharpness is unaffected.  Applied as ``np.convolve(inst, k,
  mode="same")`` after the bandpass + Hilbert but before pixel
  extraction, so sync detection runs on the un-smoothed track and
  per-pixel timing is unchanged.

---

## [0.3.7] — 2026-05-20

A May 2026 codebase audit identified 47 issues across critical, high,
moderate, and low severity tiers.  This release closes all of them.
Detailed per-finding rationale lives in commits `c79f33f`, `8fa3843`,
`e7ec275`, `14e229a`, and `68a7582`; the audit and tier breakdown is
preserved in the commit bodies.  No user-facing API changed; this is a
pure correctness / hardening release.

### Rig control

- **Band-plan tuning preserves data-variant modes** for IC-7300
  `USB-D`, Yaesu `USB-DATA`, Kenwood / Hamlib `PKTUSB`, plus Elecraft
  K3 `DATA-A` / `DATA-B`, `PSK-U` / `PSK-L`, and `FT8-U` / `FT8-L`.
  Previously every band-plan pick dropped the rig to the literal
  `USB` / `LSB` stored in the entry, which broke SSTV TX immediately
  on data-routed setups (USB-Audio Data-IN replaced by mic; speech
  processor re-enabled).  Sideband-family check now only re-issues
  `set_mode` when the family actually changes.  (Originally
  `c79f33f`; extended classifier in M6.)
- **TCI WebSocket socket timeout** (5 s) so a wedged `send_binary()`
  on a stalled network can't keep the rig keyed indefinitely.  Recv
  loop tolerates idle `socket.timeout` and continues; only true
  connection loss terminates it.  (CRIT-2)
- **TCI sample-rate mismatch refused with a clear error** rather than
  silently played at the server's rate (off-pitch / slanted SSTV at
  the receiver).  (H5)
- **TCI server-side RX subscription** no longer leaks on TX-only
  flows.  Connection tracks `is_rx_audio_subscribed()`; `_play_via_tci`
  only sends `audio_start:0;` if the RX path didn't already, and
  pairs it with `audio_stop:0;` after TX in a try / finally.  (H6)
- **Serial rig diagnostic commands** (`get_freq` / `get_mode` /
  `get_strength` / `get_ptt`) use a 200 ms deadline instead of 1 s
  for Icom CI-V, Kenwood, and Yaesu backends.  A stale read no longer
  holds the shared serial lock long enough to delay PTT-off writes —
  Stop / watchdog unkey now pre-empts within ~200 ms instead of up
  to 1 s.  Set commands keep the 1 s default.  (H12)
- **Settings → "Launch rigctld Now"** kills the previously-owned
  rigctld process before adopting the dialog's process.  Previously
  the assignment was unconditional and the orphan kept the serial
  port + TCP socket open until the OS reaped it.  (H2)
- **`_RigPollWorker.tune` failures logged at WARNING** with frequency
  + mode context.  Persistent connection loss still surfaces via the
  poll cycle within 3 s; transient errors are no longer invisible.
  (M10)

### TX correctness

- **Stop button actually aborts the chunked-write path.**
  `output_stream.stop()` previously called `sd.stop()` which only
  cancels `sd.play()` streams — every `TxWorker` call uses the
  chunked-write path, so Stop was effectively a no-op.  Now also
  calls `stream.abort()` on the active `sd.OutputStream` (tracked
  in a module-level slot) so a wedged `stream.write()` is
  interrupted from any caller thread.  (CRIT-4)
- **Stop honoured during the PTT settle delay.**  `time.sleep(ptt_delay_s)`
  replaced with `self._stop_event.wait(...)` so a click during the
  delay (up to 2 s) aborts within milliseconds instead of holding
  the rig keyed for the full settle window.  (M7)
- **Stale watchdog from a prior TX cycle** no longer races the next
  `transmit()` call's `_stop_event.clear()` or cancels a concurrent
  test tone via the global `sd.stop()`.  A monotonic `_tx_id`
  captured at schedule time is compared on fire; mismatched fires
  no-op.  (H4)
- **Slant-correction re-decode** skipped if the worker's cancel event
  was set before dispatch.  `decode_wav` is uncancellable and a
  Pasokon P7 re-decode takes several seconds; running it past a
  pending Stop made the worker thread unresponsive.  (CRIT-3)
- **CW-ID boundary envelope ramp** — 5 ms half-cosine ramp at the
  SSTV → silence boundary so the hard zero-cut at the end of
  PySSTV's sync tail no longer leaks a faint key click into the RF
  passband at high TX gain.  (M9)
- **TCI playback watchdog budget includes the 1.5 s silent tail**
  appended by `_play_via_tci` to drain the server-side audio
  pipeline.  Today's 20 % margin masked the gap, but a future
  larger tail constant would have caused false watchdog fires.  (M8)
- **`tx_audio_chunk` gated by waterfall visibility** — the waterfall
  hook fires ~10× per second during TX; without this gate a
  multi-minute Pasokon P7 transmission with the waterfall hidden
  emitted ~3000 wasted cross-thread events.  (part of H11 cleanup)
- **Encode-stage watchdog removed.**  PySSTV's `encode()` doesn't
  honour `_stop_event` and the stage-1 timer firing couldn't
  actually unblock anything.  Encoding is fast (~100 ms even for
  Pasokon P7); only the keyed-playback watchdog remains.  (H11)

### RX correctness

- **PortAudio reset refuses to run while a TX OutputStream is alive.**
  `_pa_reset()` calls process-wide `sd._terminate()` + `sd._initialize()`;
  if the user clicked RX Start mid-PTT-delay or mid-playback the
  PortAudio host was ripped out from under the live TX stream and
  the next callback crashed the process.  Now guarded by
  `is_tx_active()` and logged when skipped.  (CRIT-1)
- **RxWorker decoder state reset on audio worker swap** so the first
  decode after a TCI hot-swap doesn't continue from PortAudio
  samples in a different clock domain.  (H1)
- **`_start_once` closure de-duplication** on rapid Start / Stop /
  Start.  Previously each click connected a new closure to
  `reset_done`; the disconnect inside each closure only removed
  itself, leaving stale closures attached.  Now tracked in
  `self._start_once_closure` and disconnected by reference.  (H7)
- **`TciInputStreamWorker.stop()` drops queued chunks silently**
  instead of emitting them to a downstream RxWorker that the swap
  has already reset.  Stale audio < 100 ms old is not worth
  reseeding a fresh decoder with.  (M14)
- **Decoder `_feed_idle` keeps a 200 ms preamble window** before
  `vis_end` on an unknown VIS so a real VIS arriving within ~100 ms
  of a noise-induced false detect is still discoverable on the
  next feed.  (M15)
- **`find_input_device_by_name` skipped on the common-case Start.**
  M16 gates the call on `_input_device_needs_relookup` (set by
  `_on_audio_device_lost`).  Avoids the 50–500 ms macOS Core Audio
  GUI-thread freeze on every capture start in the no-replug case.

### Lifecycle and threading

- **`_swap_audio_worker` bounded wait (2 s).**  The
  `BlockingQueuedConnection` invocation of the old worker's `stop()`
  could freeze the GUI forever if PortAudio's `stream.stop()` /
  `close()` hung (known macOS Core Audio behaviour after USB device
  removal).  Now uses a queued invocation + `QEventLoop` + `QTimer`
  hard cap; on timeout the swap proceeds with a warning rather than
  hanging.  (H8)
- **`closeEvent` uses the same bounded-wait pattern** for the audio
  worker stop so TCI `audio_stop:0;` reaches the server before
  `rig.close()` runs.  (M2)
- **TCI audio callback teardown guard** — `getattr(self, '_queue',
  None)` mirrors the PortAudio callback so a recv-thread callback
  after `deleteLater` doesn't raise AttributeError into the
  swallow-all dispatch loop.  (H9)
- **`_on_watchdog_timeout` dispatches stop via `QTimer.singleShot(0)`**
  so a wedged Core Audio close doesn't block the worker event loop.
  (M12)
- **Emergency PTT-unkey thread join** bumped 1.5 s → 3 s for slow
  USB-CAT chains.  Daemon stays daemon; this is "give the unkey a
  real chance to complete before the interpreter exits."  (L8)
- **View → Waterfall toggle persists** across app restart.
  `_set_waterfall_config` now calls `save_config()` instead of
  only updating in-memory state.  (H3)

### Thread safety

- **TciConnection callback register / unregister + dispatch
  snapshot** lock-guarded.  Closes the register-between-snapshot-
  and-call gap (silent dropped deliveries) and unregister-during-
  dispatch race (callback fired on torn-down queue).  (H13)
- **`_dropped_chunks` counter** lock-guarded against the worker
  thread's reset / read.  Plain RMW from the RT thread vs. worker
  was lossy under CPython and unsafe under free-threaded Python.
  (H10)
- **RX audio buffer copied before `rx_audio_ready` emit** so the
  GUI-thread disk-write path and the worker-thread slant-correction
  re-decode don't share an ndarray reference.  Today both consumers
  only read; an explicit copy makes the no-sharing contract
  obvious.  (M13)

### Diagnostics / logging

Silent exception-swallowing across the codebase was a systemic
issue identified by the audit's cross-cutting concerns list.  Ten
sites now log at WARNING (user-relevant) or DEBUG (developer):

- `update_checker` catches `http.client.HTTPException` and guards
  `isinstance(data, dict)` so a malformed GitHub response doesn't
  crash the worker silently.  (M1)
- `AppConfig.__post_init__` logs at WARNING when coercing unknown
  `autosave_file_format` or `rx_audio_format` values.  (M3)
- Settings combo `findText` / `findData` silent-fallback patterns
  log when a stored value isn't found.  (M4, L4)
- First-launch Skip path no longer asymmetrically saves the
  update-checker checkbox while discarding the typed callsign.
  (M5)
- `_RigPollWorker.tune` failures, `TciConnection.disconnect`
  ws.close() errors, and `_dispatch_audio` / `_dispatch_text`
  callback exceptions all log at DEBUG or WARNING.
  (M10, L11, L14)

### Performance

- **`_all_devices()` has a 500 ms TTL cache.**  `sd.query_devices()`
  is slow on macOS Core Audio (50–500 ms after USB events) and was
  invoked 4–6 times back-to-back during Settings open and app init.
  Multiple calls inside the same UI operation now share the cache;
  user-initiated re-opens after the TTL cross the window.  New
  `invalidate_device_cache()` helper for explicit refresh.  (L1)

### Documentation

Several misleading or absent comments replaced with accurate ones:

- `templates/tokens.py` `TYPE_CHECKING` block (load-bearing for
  type-checkers, not "dead code" as the original comment implied).
  (L3)
- `config/store.py` `None`-stripping semantics and the sentinel-
  string workaround for future schema fields.  (L5)
- `templates/filename.py` `_resolve_collision` `Path.exists()` cost
  on slow network shares.  (L7)
- `serial_rig.py` CI-V broadcast-frame filter behaviour (was
  already safe; safety is now documented inline).  (L9)
- `core/cw.py` 0.01 % per-dit timing skew at non-standard sample
  rates (inaudible; documented so future readers don't try to
  "fix" the rounding).  (L10)

### Correctness polish

- **`_parse_version`** handles PEP-440-ish pre-release tags
  (`"v1.0.0rc1"` → `(1, 0, 0)` instead of all rc/a/b sorting equal).
  (L6)
- **`ImageProgress.lines_decoded`** explicitly clamped to
  `image_height`.  walk_sync_grid caps by contract; this makes the
  invariant local.  (L2)
- **`rigctld._read_until_rprt`** anchors `RPRT ` on line start
  instead of `rfind` so a get-response value containing the literal
  text "RPRT " can't end the read prematurely.  (L12)
- **TX watchdog `threading.Timer` instances are `daemon=True`** so
  an in-flight timer caught mid-shutdown can't block interpreter
  exit.  (L13)

### Internal tests

Approximately 30 new tests added across the four batches (15
for Critical, 6 for High, 8 for Moderate, plus a few autouse
fixtures).  Three existing tests adapted for the new semantics
(M7, M12, M16 changed behaviour the tests previously asserted).
**862 passed, 4 skipped, 0 warnings** on the audio / radio /
config / templates / touched-UI suites.

---

## [0.3.6] — 2026-05-20

### Added

- **RX audio recording** — opt-in lossless capture of the raw received
  audio alongside each decoded image, so operators can re-decode later
  (e.g. through the CLI or a different decoder) if the live incremental
  decoder missed something on a marginal signal.  Configure via
  Settings → Audio → "RX Audio Recording": toggle the checkbox and pick
  WAV (stdlib `wave`, 16-bit PCM) or FLAC (`soundfile`, lossless
  compressed ~40 % smaller).  Lossy formats are deliberately excluded
  because compression artefacts degrade re-decode quality.  Audio files
  share the image's filename stem when `auto_save` is also on; otherwise
  the auto-save filename template resolves an independent name.
- **Band-plan frequency helper** — one-click tune to a standard SSTV
  calling frequency via a new "Band Plan" popup button on the radio
  panel.  Twelve entries covering HF (80/40/20/17/15/10 m), VHF (2 m),
  and UHF (70 cm) with correct mode and passband (LSB below 10 MHz,
  USB above, FM on VHF/UHF).  The 20 m 14.230 MHz USB primary entry
  is shown in bold.  Button is disabled when no rig is connected or TX
  is in progress.  Tune commands run on the rig-poll thread via a
  queued cross-thread signal so they can't race with the 1 Hz poll on
  a shared serial port or WebSocket.

### Changed

- **`soundfile` moved to an optional `[flac]` extra.**  The library is
  only needed for FLAC recording; the WAV path uses stdlib `wave`.
  Install with `pip install "open-sstv[flac]"` if you want FLAC support;
  WAV-only users no longer pull in `libsndfile` (~10 MB DLL on Windows).
  Pattern matches the existing `websocket-client` lazy-import gate for
  TCI.  The FLAC code path gracefully degrades to a user-visible
  warning when the package isn't installed.
- **Waterfall paint uses smooth bilinear scaling.**  The pixmap scale
  now uses `Qt.TransformationMode.SmoothTransformation` instead of
  `FastTransformation`, eliminating staircase artefacts at the
  frequency-marker dotted lines.  Negligible cost on the 400×200
  backing buffer.

### Fixed

- **TCI secondary-TRX events no longer misread as PTT.**  The `trx:`
  handler now requires the TRX index to be `0` before updating
  `_last_ptt`; a hypothetical `trx:1,true;` from a second receiver was
  previously treated as a PTT event on TRX 0.
- **`_audio_thread.finished` no longer accumulates `deleteLater`
  connections** across `_swap_audio_worker` calls.  Every TCI
  connect/disconnect previously added one more queued `deleteLater`
  slot on thread shutdown.  The existing `old.deleteLater()` further
  down handles cleanup correctly.
- **`tx_audio_chunk` cross-thread signal is gated by waterfall
  visibility.**  Previously emitted ~10 Hz throughout a multi-minute
  TX even when the waterfall window was hidden, sending ~3000 wasted
  cross-thread events per Pasokon P7 transmission.  `TxWorker` now
  exposes `set_waterfall_active(bool)` which `MainWindow` calls from
  `_on_toggle_waterfall`.

### Testing

- 14 new tests for RX audio recording — signal emission paths
  (happy path, `None` buffer, `MagicMock` guard, empty buffer), WAV
  write properties + PCM quantisation, FLAC roundtrip, unknown-format
  fallback, the standalone-filename branch where `auto_save=False`
  but `autosave_rx_audio=True`, and the settings dialog state.
- 18 new tests for TCI — silence-dither correctness (no consecutive
  equal even/odd pairs, inaudible amplitude, tone passes through
  unchanged, mixed tone/silence chunks fully protected), 64-byte
  v2.0 audio header layout (`type=TX_AUDIO`, `format=float32`,
  `channels=1`, declared rate uses caller-supplied not RX rate), and
  `_on_text` parser (VFO A stores, VFO B ignored, TRX 1 ignored,
  malformed/truncated inputs don't raise).
- 21 new tests for the band-plan feature — data integrity
  (positive freqs, valid modes, exactly one primary, no duplicates),
  known-frequency regression guards (20 m USB, 40/80 m LSB, 2 m FM,
  10/15 m USB, region tags), `primary_entry()` helper, frozen
  dataclass immutability, and menu structure (region separators
  land at HF→VHF and VHF→UHF boundaries; default-arg lambda binds
  the correct entry to each menu action).

---

## [0.3.5] — 2026-05-19

### Added

- **TCI (Transceiver Control Interface) rig support** — for the Expert
  Electronics SunSDR2 family (ExpertSDR2 / ExpertSDR3) and the
  AetherSDR.  A single WebSocket connection carries both CAT control
  and binary PCM audio, so rig control and RX/TX audio routing flow
  through one configurable host:port (default `127.0.0.1:40001`).
  Configure via Settings → Radio → Connection mode → "TCI (ExpertSDR2
  / SunSDR)".  `websocket-client` is added as a runtime dependency and
  lazily imported, so users who never enable TCI don't pay for it at
  startup.
- **FFT waterfall display** — floating spectrogram window accessible
  via View → Waterfall.  Shows the 0–4 kHz SSTV audio band as a
  scrolling FFT spectrogram with distinct cool (RX) and warm (TX)
  palettes, plus dotted reference lines at the 1200 / 1500 / 1900 /
  2300 Hz SSTV tones.  Lazy-created on first open and hidden (not
  destroyed) on uncheck so scroll history is preserved between
  toggles; visibility persists across app restarts.

### Fixed

- **Windows WDM-KS output devices are excluded from the output device
  picker.**  PortAudio's blocking output API is not implemented for
  the Windows WDM-KS host (e.g. DAX Audio virtual cables), so a saved
  WDM-KS output caused every transmission to abort immediately with
  PaErrorCode -9999.  WDM-KS input devices are unaffected
  (callback-based) and continue to appear normally.  The filter is a
  no-op on Linux and macOS.
- **"Transmission aborted" no longer overwrites the real TX error
  message.**  `_on_tx_error` and `_on_tx_aborted` are queued Qt
  signals that fire back-to-back on a fatal playback error; the abort
  handler was unconditionally wiping the explanatory text from the
  status bar.  A pending-error flag now keeps the actual error
  visible for its full 8 s timeout.
- **TCI audio hot-swap now stops the old worker cleanly and restarts
  capture on the new one.**  The previous implementation tore down
  the old `InputStreamWorker` without invoking its `stop()` slot, so
  `audio_stop:0;` never reached the TCI server, and any RX capture
  that was active when the user connected to (or disconnected from)
  TCI silently died until the user restarted it manually.  Both are
  now handled inside `_swap_audio_worker`: `stop()` is dispatched via
  a blocking queued invocation so the audio thread actually runs it
  before teardown, and capture is re-emitted on the new worker if it
  was active before the swap.
- **TCI VFO B updates no longer clobber the operating frequency
  cache.**  `TciRig._on_text` accepted every `vfo:` event into
  `_last_freq`, so the post-`READY:` state burst — which sends both
  VFO A and VFO B — flipped the displayed frequency between the two.
  Filtered to VFO index `0` (VFO A) only.
- **TCI reconnect now starts cold.**  `_freq_received` /
  `_mode_received` / `_ptt_received` and their `threading.Event`
  partners are reset in `TciRig.close()` so a reconnect re-populates
  the cache from the new `READY:` state burst rather than serving
  stale values from the previous session.

### Changed

- **Default window size bumped to 1280×720, splitter biased toward the
  TX panel.**  The previous 1100×640 with a 1:1 splitter put the TX
  panel at ~550 px, where the template gallery's flow layout could
  only fit 3 cards per row with ~58 px of trailing whitespace.  The
  new defaults give the TX panel ~640 px out of the box (`setSizes([640,
  540])`), enough for a clean 4-card row at the 140 px max thumbnail
  width.  Stretch factors stay 1:1 so the gallery still reflows when
  the user resizes.
- **`sounddevice` pin narrowed to Windows only.**  The `<0.5` cap
  exists to work around a stack overrun in `Pa_Initialize` that only
  affects Windows; on Linux and macOS the 0.5.x release is fine.  The
  dependency now reads `sounddevice>=0.4.6,<1` for all platforms with
  an additional `sounddevice<0.5; sys_platform=='win32'` constraint,
  so macOS and Linux users on 0.5+ no longer get a forced downgrade
  when reinstalling.

---

## [0.3.4] — 2026-04-29

### Added

- **General settings tab** — new first tab consolidating app-level
  settings: callsign + operator info (name / grid / QTH), default TX
  mode, and the update checker.  Audio / Radio / Images stay focused
  on their own domains.
- **Persistent operator info in `AppConfig`.**  Three new fields with
  empty-string defaults, surviving roundtrip through TOML:
  - `operator_name` — short display name (e.g. "Kevin")
  - `grid_square` — Maidenhead grid locator (e.g. "EM29")
  - `qth` — free-text location (e.g. "Kansas City, MO")
  Pre-v0.3.4 configs load unchanged; the new fields just appear with
  empty defaults.
- **First-launch dialog gained three optional fields** — Name, Grid
  Square, QTH — sitting below the existing Callsign input.  All three
  are optional; Skip and empty-Save still behave the same way.  Grid
  Square forces uppercase as the user types; Name and QTH preserve
  case.  Intro copy now mentions the v0.3 template tokens.
- **`{qth}` template token** for the v0.3 image-template compositor,
  resolving from `AppConfig.qth`.  Named-form only — no percent-form
  because `%q` is already the QSO serial token (introduced in v0.3.0)
  and a breaking change there would silently rewrite existing
  templates.
- **`{name}` and `{grid}` tokens (and their `%n` / `%g` percent
  equivalents) now read from the new `operator_name` and `grid_square`
  AppConfig fields.**  Pre-v0.3.4 the resolver speculatively read from
  fields that never existed (`op_name`, `grid`) and always returned
  empty; templates that used these tokens will now resolve real values
  for users who have them set.

### Changed

- **Callsign moved from the Radio tab to the new General tab.**  The
  Radio tab's PTT/Identity group is renamed to "PTT" (PTT delay
  remains there).  Existing tests and integrations that grab
  `dialog._callsign` keep working — the attribute name is unchanged,
  only the tab placement moved.
- **Default TX mode picker moved from the Images tab to the General
  tab's Defaults group.**  Single source of truth for the setting.
- **Update checker checkbox moved from the Images tab to the General
  tab's Updates group.**  Images tab is now focused purely on
  image-related settings (auto-save, TX banner).

### Fixed

- **rigctld help text vertical clipping in the Settings dialog.**
  Qt's QLabel.sizeHint() underestimates height for word-wrapped rich
  text inside QFormLayout — at the dialog's minimum width the third
  rendered line ("Hamlib installed.") was clipping at the top.
  Reserve room for ~3 wrapped lines using font metrics so the fix
  scales with the user's UI font size.

### Added

- **"?" help button next to the auto-save filename template field**
  on the Images tab.  Clicking it pops a modal listing every supported
  filename token (`%d`, `%t`, `%c`, `%m`, `%rx_tx`, …) with a
  rendered example.  The hover-only tooltip from v0.2.8 stayed
  invisible to most users — especially on macOS where QLineEdit
  tooltips don't always trigger reliably — so the same content is
  now one click away.

### Notes

- The original feature spec mentioned a "Check now button + status
  label" alongside the Updates checkbox.  Those don't exist on the
  Images tab today (only the checkbox), so they aren't part of the
  move; the General tab Updates group is checkbox-only for now.

---

## [0.3.3] — 2026-04-29

### Fixed

- **`__version__` now reads from package metadata via
  `importlib.metadata.version("open_sstv")`** instead of being hardcoded
  in `src/open_sstv/__init__.py`.  `pyproject.toml` is the single source
  of truth for the version string going forward.  Prior releases left
  `__version__ = "0.3.0"` stale through 0.3.1 and 0.3.2, which fed
  wrong values into the About dialog, the TX banner stamped on every
  transmitted image, and the update checker's "newer version available"
  comparison.  A `PackageNotFoundError` fallback (`"0.0.0-dev"`)
  preserves the previous "always importable" behaviour for unpacked-
  source runs without an install.
- **JACK host-API audio devices are filtered out on Linux.**  PortAudio
  enumerates the same physical card under both ALSA and the JACK virtual
  routing daemon, producing confusing duplicate entries in the device
  picker.  On Linux only, devices whose host API name contains "jack"
  (case-insensitive) are now hidden.  All other platforms unchanged.
- **Settings dialog default minimum width bumped from 480 to 640 px.**
  At the prior minimum the Radio tab's rigctld group title, wrapped
  help paragraph, and "Auto-launch rigctld on Connect" checkbox label
  all clipped, forcing users to manually resize before they could read
  the panel.  640 gives every form row breathing room without making
  the dialog feel oversized on small screens.

---

## [0.3.2] — 2026-04-28

### Fixed

- **TX source images are center-cropped to the SSTV mode's frame size before
  template compositing.**  Banner and overlay placement is now predictable for
  any source aspect (phone portrait, 4:3, 16:9, etc.) without distorting the
  photo.  The original image is never mutated — the editor and preview still
  see the user's source.  Photos already at the mode's exact frame size pass
  through with object identity preserved, so there's no spurious LANCZOS
  resample on the hot path.  Both TX and live preview share the same compose
  path, so what you see in the preview is exactly what gets transmitted.

---

## [0.3.1] — 2026-04-28

### Added

- **+RX Image layer button** in the v0.3 template editor.  Surfaces the
  existing `RxImageLayer` model so users can author received-image insets
  without hand-editing TOML.  Default geometry: BR-anchored 30%×25% with
  a 2% inset and `fit="cover"` — the conventional RX-preview placement.
- **Five new bundled OFL-licensed fonts** — Orbitron Bold, Oswald Bold,
  Exo 2 Bold, Bebas Neue, Share Tech Mono.  Total shipped fonts: 8.
- **Variable-font Bold-axis support.**  Orbitron, Oswald, and Exo 2 ship
  as variable fonts; the renderer's `_load_font` snaps the `wght` axis
  to the "Bold" named instance when the family name carries Bold intent.
  Static fonts (Inter Bold, DejaVu Sans Bold) are unaffected — the
  variation call swallows `OSError` on fonts without a weight axis.
- **Rainbow gradient text mode** for text layers.  New `color_mode` field
  on `TextLayer` with values `"solid"` (default) and `"rainbow"`.  In
  rainbow mode the renderer paints the full glyph silhouette in the
  stroke colour first, then composites a smooth HSV hue sweep through a
  glyph-only mask — horizontal text gets a left-to-right gradient,
  stacked text gets a top-to-bottom gradient.  Stroke ring keeps its
  uniform colour.  Alpha is taken from `layer.fill[3]` so semi-transparent
  rainbow text remains possible.  Default `"solid"` is omitted from TOML
  output so existing templates roundtrip unchanged.
- **Solid/Rainbow combo** in the text-layer inspector.  Switching to
  Rainbow drops the now-meaningless Fill picker from the form.

---

## [0.3.0] — 2026-04-28

The headline release: a full layered template compositor replaces the old
text-overlay button bar, plus a thorough security and stability audit.

### Added

- **Layered template compositor.**  Each template is now a stack of layers
  (photo, text, rect, gradient, pattern, station_image, rx_image) composited
  at TX time onto the selected mode's native frame size.  Positions and sizes
  are stored as percentages so one template renders cleanly at every
  resolution from Robot 36's 320×240 to PD-290's 800×616.
- **Template gallery on the TX panel.**  Live thumbnails of every template
  rendered with the user's current photo and QSO state, filtered by role
  (CQ / Reply / 73 / Custom).  Single-click selects, double-click opens the
  editor, right-click offers Edit / Duplicate / Rename / Delete.
- **Three-panel template editor** with a live preview, scrollable property
  inspector, and reorderable layer list.  Non-modal — operate while you
  edit.
- **MMSSTV-style and named tokens.**  `%c` / `{callsign}`, `%o` / `{tocall}`,
  `%r` / `{rst}`, `%name_o` / `{tocallname}`, `%n` / `{name}`, `%g` / `{grid}`,
  `%m` / `{mode}`, `%d` / `{date}`, `%t` / `{time}`, `%f` / `{freq}`,
  `%b` / `{band}`, `%q` / `{qso_serial}`, `%v` / `{version}`.  Unknown
  tokens pass through unchanged for forward compatibility.
- **QSO State widget** (ToCall / RST / Name) that drives every dynamic
  token in real time across the gallery and editor previews.
- **RX-to-TX image pipeline.**  Single-click any thumbnail in the RX history
  gallery to pin it as the active RX image; any Reply template with an
  `rx_image` slot then composites it into every gallery thumbnail and the
  TX preview, so a one-click reply with their picture in your card is the
  default workflow.
- **Stacked vertical text** orientation for classic side-of-photo callsign
  banners; auto-shrink + word-wrap so long contest exchanges never overflow.
- **Three shipped fonts** — DejaVu Sans Bold, Inter Bold, Press Start 2P —
  plus drop-in support for user-supplied `.ttf` / `.otf` files in
  `{user_config_dir}/open_sstv/fonts/`.
- **TOML template format** for sharing templates between operators.
  Schema-versioned (`schema_version = 1`) so future builds can refuse
  templates from a newer format rather than misinterpret them.

### Security

- **Confine `StationImageLayer.path` to the assets directory.**  Template
  files can no longer reach `/etc/passwd` or smuggle a path past the
  renderer via absolute paths or `..` segments — both are rejected at
  TOML load time, and the renderer re-verifies `is_relative_to(assets_dir)`
  after symlink resolution as defense-in-depth.
- **PIL decompression-bomb cap.**  `MAX_IMAGE_PIXELS` is pinned to 32 MP at
  package import; every entry point (GUI, CLI, tests) catches
  `DecompressionBombError` and surfaces it as a clean error rather than
  letting a crafted PNG OOM the process.

### Fixed

- **Stable layer count for empty-text TextLayers.**  An empty resolved
  string now reserves a transparent cell instead of skipping the layer
  entirely, so the composite pipeline's layer count is data-independent.
- **`reference_frame` floats are rounded, not truncated.**  `[320.7, 256.3]`
  used to silently become `(320, 256)` via `int()`; now it rounds to
  `(321, 256)` and warns that the field is integer-only.
- **Concurrent `save_config` writes are serialised** with a module-level
  `threading.Lock` so future background-thread callers can't interleave
  tmp-then-replace sequences.
- **`ImageGalleryWidget` releases evicted PIL handles** in the in-memory
  fallback path — the disk-backed path was already ref-clean, but the
  fallback used to keep PIL.Image objects alive past `_MAX_IMAGES`.
- **Centralised worker exception handler** (`MainWindow._handle_worker_error`)
  surfaces previously-silent except blocks (TxWorker emergency unkey,
  RxWorker slant-correction fallback) as status-bar messages with full
  exc_info logging.
- **Robot-36 chroma pairing uses `zip(strict=True)`** so a future
  even/odd-row length divergence raises `ValueError` at encode time
  instead of silently truncating a chroma row.
- **RGBA short-list warning.**  TOML `fill = [255, 128, 0]` (no alpha)
  still loads as opaque but now logs a warning so authors notice the
  missing channel.
- **`duplicate_template` slug double-check** now documents *why* the
  two-condition guard is intentional, not redundant.
- Renderer's `PatternLayer` tint vectorised with NumPy (~10× faster on
  large patterns).
- Multiple smaller fixes: narrow `except` in update-checker, RX slot
  empty-state placeholder ("RX" label + bordered box; no border when an
  image is present), `_text_bbox` cached once per candidate in
  `_wrap_text`.

### Changed

- 20 unused imports cleaned up across the package (`ruff F401`).
- Pre-release `ruff` sweep applied 180+ safe auto-fixes (UP037 quoted
  annotations, I001 import order, UP017 `datetime.UTC`).

---

## [0.2.16] — 2026-04-24

### Added

- **Built-in update checker.**  On every launch (if enabled), a background
  thread makes a single read-only HTTPS GET to the GitHub releases API
  (`api.github.com/repos/bucknova/Open-SSTV/releases/latest`) and compares
  the returned `tag_name` against the running version using semver tuple
  comparison.  If a newer release exists, a clickable link appears as a
  permanent widget in the status bar — no auto-download, no auto-install.
  The check times out after 3 seconds; any network failure is silently
  swallowed.  Uses stdlib `urllib.request` only — no new dependencies.

- **"Check for updates on startup" preference.**  The first-launch welcome
  dialog now includes an opt-in checkbox (default: on) with a transparency
  note ("Checks github.com/bucknova/Open-SSTV for new releases. No data is
  sent.").  The preference is also exposed in Settings → Images → Updates for
  returning users.

### Fixed

- **Saving Settings no longer resets `first_launch_seen`.**  `result_config()`
  in the Settings dialog now preserves `first_launch_seen` from the active
  config, so the first-launch welcome dialog no longer reappears after the
  user saves their settings.

---

## [0.2.15] — 2026-04-24

### Fixed

- **TX aborts immediately on USB unplug.**  The previous `sd.query_devices()`
  check was unreliable — macOS silently redirects the output stream to the
  built-in speakers without removing the device from PortAudio's device table,
  so the query always succeeded even after the radio was unplugged.  The output
  stream now calls `rig.get_ptt()` every ~1 s between audio write chunks
  (`periodic_check` parameter in `play_blocking`).  The serial port dies
  instantly on USB unplug, raising `RigConnectionError`, which sets the stop
  event and breaks the write loop.  `ManualRig.get_ptt()` is a no-op so the
  check is skipped when no rig is connected.

- **Radio panel shows "Disconnected" immediately during TX, not after.**
  A new `TxWorker.rig_disconnected` signal is emitted by the rig health check
  closure before re-raising, and is wired to `_on_radio_disconnected` in
  `MainWindow`.  The radio panel now updates within ~1 s of the USB unplug
  event rather than waiting for the entire TX sequence to unwind.

- **Unconditional PortAudio reset on every Start Capture.**  Removed the
  `_device_lost` flag gate from `InputStreamWorker.start()` — `_pa_reset()`
  now always runs immediately before `sd.InputStream()`.  The conditional reset
  only covered the RX watchdog and PortAudio `finished_callback` paths;
  TX-path disconnects (detected via serial health check) never set
  `_device_lost` on the `InputStreamWorker`, causing `-9986 paInvalidDevice`
  on the next Start Capture.  Always resetting eliminates the entire class of
  "forgot to set the flag" bugs regardless of which code path detected the
  disconnect.

### Added

- **Banner height scaled from image width.**  `scaled_banner_params()` now uses
  `image_width` as the scaling base (was `image_height`).  Percentages updated
  to 6 % / 8 % / 10 % of width for Small / Medium / Large (was 9 % / 12 % /
  15 % of height), with clamps (18–36) / (22–48) / (28–60) px.  Narrow modes
  like Martin M2 (160 px wide) now get proportionally thinner banners instead
  of banners sized as if the image were as tall as it is wide.

- **About dialog links GitHub Pages site.**  The Help → About Open-SSTV dialog
  now shows both the GitHub repository link and the GitHub Pages site
  (`bucknova.github.io/Open-SSTV`) as clickable hyperlinks.

- **Default test image loads automatically in TX panel.**  A classic TV test
  pattern (`testimage.jpg`) is bundled as a package asset under
  `open_sstv/assets/` and loaded into the TX panel at startup.  The TX panel
  is ready to transmit immediately on first launch without the user having to
  click Load Image.

### Docs

- **Author attribution.**  "Created by Kevin (W0AEZ)" added to README.md
  (under title), `pyproject.toml` (`authors` field), `docs/index.html` footer,
  and User Guide version line.  About dialog already carried this attribution.

---

## [0.2.14] — 2026-04-24

### Fixed

- **Audio device hot-unplug recovery — PortAudio reset at stream-open time.**
  Moving `sounddevice._terminate()` + `sounddevice._initialize()` to execute
  immediately before `sd.InputStream()` in `start()` (rather than during
  `stop()`) eliminates the `-9998 paInvalidDevice` error on reconnect.  The
  prior stop()-time reset was too early: macOS reassigns USB audio device
  indices while the user replugges the radio, so PortAudio went stale again
  between `stop` and the user clicking Start.  Resetting right before the
  `InputStream` call guarantees a fresh OS device table at the exact moment
  it is needed.  The `_device_lost` flag is unchanged — the expensive
  terminate/initialize cycle is skipped on ordinary stop/start cycles.

- **Start Capture button responsive after device reconnect.**  Device
  re-enumeration (by saved name) now occurs inside `_start_once`, which fires
  after `reset_done` — i.e. after the PortAudio reset has completed — so the
  post-replug PortAudio index is used rather than the stale pre-reset index
  captured before the worker reset.  `_suppress_rx_status_updates` is now
  also cleared in `_on_capture_requested(True)` as a defensive measure in case
  `_on_rx_started` never fired after the previous session's stream-open failure.

- **Rig-connect-thread QObjectWrapper crash (use-after-free).**  Removed
  `thread.finished.connect(worker.deleteLater)` and
  `thread.finished.connect(relay.deleteLater)` from `_start_rig_connect_thread`.
  The prior `deleteLater` calls posted a `DeferredDelete` event on the exiting
  worker thread; Qt processed it when the thread finished — racing with
  `_on_connect_thread_finished` dropping the Python ref and freeing the
  `QObjectWrapper`, causing a use-after-free crash on startup auto-connect.
  Python GC via ref-clearing in `_on_connect_thread_finished` is now the sole
  destructor.  Signals are now connected before `worker.moveToThread(thread)`,
  and `_on_connect_thread_finished` explicitly disconnects
  `thread.started → worker.run` before nulling the Python refs to remove Qt's
  internal connection record.

- **Persistent disconnect feedback after stream-open failure.**  Audio-worker
  `error` signals now route through a new `_on_rx_audio_error` slot that stores
  the message when `_capture_running` is False (start-time failure).
  `_on_rx_stopped` consults this stored message and re-displays it rather than
  overwriting with *"Not listening — click Start to begin."*, so the user sees
  the actual error instead of a silent no-op.

### Tests

- 11 new tests in `tests/ui/test_main_window.py` covering: `_on_rx_audio_error`
  storage logic, `_on_rx_stopped` error-message persistence, `_on_capture_requested`
  field clearing, two-sequential-connect lifecycle, and idempotent
  `_on_connect_thread_finished` cleanup.  Suite total: 51 passed, 2 skipped
  (headless QThread timing flakes).

---

## [0.2.13] — 2026-04-23

### Fixed

- **Audio device hot-unplug recovery — full PortAudio reset cycle.**  When the
  IC-7300 (or any USB audio device) is unplugged while capture is running,
  `InputStreamWorker` now detects the loss via PortAudio's `finished_callback`
  and a 3 s watchdog timer.  On reconnect, `sounddevice._terminate()` then
  `sounddevice._initialize()` flush the stale internal device-handle cache so
  a fresh `sd.InputStream()` succeeds instead of failing with
  `-10851 (Invalid Property Value)`.  The saved device name is re-enumerated
  on every Start click so a replug that assigns a new PortAudio index is
  transparent to the user.

- **Start button always re-enables after a failed stream open.**  Previously,
  if `InputStreamWorker.start()` raised (stale device index, device not yet
  re-enumerated), only `error` was emitted and `stopped` was not, leaving the
  Start Capture button permanently greyed.  `stopped` is now unconditionally
  emitted in every failure path of `start()`.

- **Persistent "Audio device disconnected" feedback.**  When device loss is
  detected, the status bar and RX panel label now show
  *"Audio device disconnected — replug and click Start to recover"* and remain
  visible.  Two previous races wiped the message before the user could read it:
  (a) `RxWorker.status_update` emits `"Listening… Xs buffered…"` on a periodic
  timer; these updates are now suppressed via a `_suppress_rx_status_updates`
  gate that is set on stop/disconnect and cleared only when the stream is
  confirmed running.  (b) When PortAudio's `finished_callback` fires during
  `start()`, `stream_error` arrives at the GUI before `started`; `_on_rx_started`
  now returns early if `_last_rx_disconnect_msg` is set so it cannot overwrite
  the disconnect message.

- **RX panel status correctly reflects idle state.**  After any stop (user-
  initiated or device loss), the RX panel label shows
  *"Not listening — click Start to begin."* rather than staying on the last
  decode message or "Listening…".  The periodic `RxWorker` status-update timer
  no longer overwrites this text after the stream closes.

### Tests

- 28 new tests across `tests/audio/test_input_stream.py` (PA reset, watchdog
  flag, `finished_callback` lifecycle, `_stopping` guard) and
  `tests/ui/test_main_window.py` (device-loss message persistence, signal
  ordering race, `status_update` suppress gate, Start-button re-enable,
  device re-enumeration by name).  Suite total: 760 passed, 1 skipped.

---

## [0.2.12] — 2026-04-23

### Fixed

- **Radio connect button — replaced broken lambda+QueuedConnection with
  `_RigConnectRelay` QObject.**  PySide6 resolves `AutoConnection` to
  `DirectConnection` for plain Python lambdas (they carry no `QObject` thread
  affinity), so the `on_success`/`on_error` callbacks added in v0.2.11 were
  still executing on the worker thread and being silently dropped by Qt on
  macOS.  The fix introduces `_RigConnectRelay`, a tiny `QObject` subclass
  whose slots have real thread affinity; all worker-result connections are now
  routed through it so `AutoConnection` resolves to `QueuedConnection` as
  intended.

- **TX unkey crash on USB unplug — serial I/O now catches `termios.error`.**
  `termios.error` does not inherit from `OSError` (MRO:
  `termios.error → Exception → BaseException`), so the previous
  `except (serial.SerialException, OSError)` guards in every rig backend
  (`IcomCIVRig`, `KenwoodRig`, `YaesuRig`, `SerialPttRig`) silently missed it.
  A module-level `_SERIAL_IO_ERRORS` tuple now includes `termios.error`
  explicitly on POSIX and falls back gracefully on Windows where the `termios`
  module is absent.  The TX worker's `finally` block is also hardened so that a
  `termios.error` raised during unkey never propagates as an unhandled exception.

- **Radio disconnect not detected after USB unplug — UI now auto-disconnects.**
  The poll worker accumulated `termios.error` spam after the USB cable was
  removed but never signalled the GUI, so the panel remained stuck at
  "Connected".  A consecutive-error counter (`_POLL_FAIL_THRESHOLD = 3`) now
  fires `radio_disconnected` exactly once when the threshold is reached.
  `MainWindow._on_radio_disconnected` stops the poll timer, calls
  `set_connected(False)` on the radio panel, closes the dead serial port, and
  shows *"Radio disconnected — check USB connection"* in the status bar.

### Added

- **Banner preview uses the actual TX image.**  The TX banner settings pane now
  renders its preview against whatever image is currently loaded in the TX panel
  (falls back to a neutral grey sample when no image has been loaded yet).  This
  lets you see exactly how the callsign strip will look on your specific image
  before transmitting.

- **Dynamic banner scaling — height proportional to image height.**  Banner
  height was previously a fixed pixel value (Small = 24 px, Medium = 32 px,
  Large = 40 px) that looked proportionally very different across modes with
  different pixel heights.  The Small / Medium / Large preset now defines a
  percentage of the image's pixel height (9 % / 12 % / 15 %) clamped to
  per-preset min/max bounds (`[18,32]` / `[24,44]` / `[28,56]` px).  Font size
  scales with banner height at a fixed 0.75 ratio.  The push-down resize
  behaviour is unchanged — SSTV mode pixel geometry is preserved exactly.

### Tests

- 27 new tests across `tests/radio/test_serial_rig.py` (termios wrapping),
  `tests/ui/test_main_window.py` (poll error counter, auto-disconnect),
  and `tests/core/test_banner.py` (scaled banner params for all four reference
  modes: Martin M1 320×256, Martin M2 160×256, Robot 36 320×240, PD-120
  640×496).  Suite total: 732 passed, 1 skipped.

---

## [0.2.11] — 2026-04-23

### Fixed

- **Connect button no longer sticks at "Connecting…" forever.**  The OP2-02
  fix moved `rig.open()+ping()` off the GUI thread via `_RigConnectWorker`,
  but PySide6 resolves `AutoConnection` to `DirectConnection` for plain Python
  lambdas (they have no `QObject` thread affinity).  The `on_success`/`on_error`
  callbacks were therefore running on the worker thread, where Qt silently drops
  widget mutations on macOS.  All worker signal → lambda connections now use
  explicit `Qt.ConnectionType.QueuedConnection`, guaranteeing delivery on the
  GUI event loop.

- **Rig connect now times out after 5 seconds.**  If `open()+ping()` never
  returns (unresponsive radio, serial port that exists but has no device, or a
  dead rigctld TCP endpoint), a `QTimer` fires after `_CONNECT_TIMEOUT_S = 5.0`
  seconds and surfaces the message *"Connection timed out — check that your
  radio is connected and powered on."*  Previously the app was stuck
  indefinitely with no escape.

- **Connect button becomes "Cancel" during a connect attempt.**  Clicking it
  while the worker is in-flight emits `cancel_requested`, aborts the thread,
  and returns the panel to *Disconnected* state.  Closing the window mid-connect
  also triggers the same abort path.

- **Closing the window mid-connect no longer crashes.**  `_RigConnectWorker`
  runs on a `QThread` parented to `MainWindow`.  If the window was closed while
  the thread was blocking inside `rig.open()`, Qt's `deleteChildren` would
  destroy the still-running `QThread`, triggering `QThread::~QThread() →
  fatal()`.  `closeEvent` now calls `_abort_connect()` first, which sets the
  cancel event, calls `thread.quit() + thread.wait(2000)`, and falls back to
  `thread.terminate()` if the blocking C call does not return in time.

### Tests

- 10 new tests: `tests/ui/test_radio_panel.py` (Cancel button surface) and
  `tests/ui/test_main_window.py` (timeout, cancel, close-safety, worker cancel
  suppression).  Suite total: 678 passed, 1 skipped.

---

## [0.2.10] — 2026-04-22

### Fixed

- **TX banner errors are now surfaced, not silently lost (OP2-01).**  If
  `apply_tx_banner` raises `ValueError` (e.g. image too small for the
  requested banner height), `TxWorker.transmit()` now emits `error()` and
  returns immediately rather than letting the exception vanish into the
  watchdog cancel path.  PTT is never keyed, and the status bar shows a
  clear "TX banner failed: …" message.

- **Rig open+ping no longer freezes the GUI (OP2-02).**  On unresponsive
  radios the CAT connect attempt previously blocked the event loop for up
  to 4 seconds.  Both `_connect_serial()` and `_finish_rigctld_connect()`
  now push `rig.open() + rig.ping()` to a one-shot `_RigConnectWorker`
  `QObject` on a `QThread`; the GUI remains responsive while the handshake
  is in flight.  The Connect button shows "Connecting…" (orange) and is
  disabled until the attempt resolves.

- **RX Start double-click no longer spawns two audio streams (OP2-03).**
  Clicking Start now disables the button immediately; it is re-enabled only
  when `set_capturing(True)` is received from the worker thread.  A second
  click in the interim is a no-op.

- **Gallery temp-file names no longer collide after GC (OP2-04).**
  Temp filenames were previously derived from `id(image)`, which CPython
  reuses after the object is garbage-collected.  A monotonically
  incrementing `_image_counter` is used instead, guaranteeing uniqueness
  across the session.

- **rigctld read loop has a hard byte cap (OP2-05).**  `_read_until_rprt`
  now raises `RigCommandError` if the accumulated buffer exceeds 64 KiB,
  preventing unbounded growth from a daemon that streams garbage without
  newline terminators (the existing line-count guard does not fire in that
  case).

- **`load_config` no longer swallows permission errors (OP2-06).**  The
  broad `except Exception` that fell back to defaults on any read failure
  has been narrowed to `(tomllib.TOMLDecodeError, UnicodeDecodeError)`.
  `OSError` (permission denied, directory in place of file, etc.) now
  propagates so the operator is notified of a real filesystem problem
  rather than silently starting with factory defaults.

- **Config and template saves are now atomic (OP2-07).**  `save_config()`
  and `save_templates()` write to a sibling `.tmp` file and use
  `os.replace()` for the final rename.  A `SIGKILL` mid-write no longer
  leaves a truncated or zero-length config; the previous version is
  preserved until the new one is fully written.

- **Out-of-range CW fields are now logged when clamped (OP2-08).**
  `AppConfig.__post_init__` already clamped `cw_id_wpm` and
  `cw_id_tone_hz` silently; it now logs an `INFO` message so operators
  who hand-edit the TOML file understand why their value was overridden on
  next save.

- **Rig connect callbacks guard against post-close firing (OP2-09).**
  The `_finish_rigctld_connect()` function (called via a 500 ms
  `QTimer.singleShot` after spawning rigctld) now returns immediately if
  the window is no longer visible, preventing a `RuntimeError` or
  attribute access on a destroyed widget.

- **Robot 36 fallback threshold respects non-48 kHz sample rates
  (OP2-10).**  The constant `_DETECT_FALLBACK_SAMPLES = 3 * 48_000` was
  used as a raw sample count even at 44.1 kHz, collapsing via integer
  division to only 2 seconds of budget.  Replaced with
  `_DETECT_FALLBACK_S = 3.0` (seconds) and the threshold is now computed
  as `int(fs * _DETECT_FALLBACK_S)` at runtime.

- **rigctld subprocess is process-group isolated (OP2-14).**  `Popen` in
  both `MainWindow._connect_rigctld()` and `SettingsDialog` now passes
  `start_new_session=True`.  On POSIX a GUI crash or `SIGKILL` no longer
  propagates to the rigctld child, preventing the serial port from being
  held open after the application exits.

- **CLI `decode_wav` no longer applies polyfit slant correction to
  Robot 36 (OP2-15).**  The global least-squares slant fit can be
  corrupted by sync outliers on noisy signals, producing skewed images in
  the very conditions where slant correction is most needed.  `decode_wav`
  now passes `apply_slant_correct=False` to `_decode_robot36_dispatch`,
  matching the GUI `RxWorker` path which has always skipped this step for
  Robot 36.

- **Decoder rebuild no longer contaminates new decoder with stale audio
  (OP2-16).**  `RxWorker.set_weak_signal()` and `set_incremental_decode()`
  each rebuild `self._decoder`; they now also clear `_scratch` and reset
  `_scratch_samples` so the new decoder does not process audio buffered
  during the old decoder's lifetime.

- **Settings save error message identifies running rigctld (OP2-18).**
  When `save_config()` raises `OSError` and rigctld is currently running
  (e.g. the config directory is read-only), the status bar now shows a
  message noting that rigctld is still running rather than a generic
  "could not save to disk" text.

- **`_schedule_rx_resume` QTimer cannot fire after window close (OP2-19).**
  The 50 ms `QTimer.singleShot` lambda in `_schedule_rx_resume` now checks
  `self.isVisible()` before emitting `_request_rx_gate`, preventing a
  signal dispatch onto a destroyed widget when the user closes the window
  immediately after a TX cycle.

### Tests

- 19 new tests across `tests/config/`, `tests/core/`, `tests/radio/`, and
  `tests/ui/` covering every fix above.  Suite total: 668 passed, 1 skipped.

---

## [0.2.9] — 2026-04-19

### Fixed

- **Yaesu CAT set commands no longer time out.**  `set_ptt`, `set_freq`,
  and `set_mode` on `YaesuRig` previously routed through `_command()`,
  which waited up to 1 second for a response.  Yaesu *set* commands
  (`TX1;`, `TX0;`, `FA{9d};`, `MD0{digit};`) execute silently — the radio
  sends no response — so every PTT key, every frequency QSY, and every
  mode change blocked for a full second before failing.  Confirmed on
  FT-991 and FT-991A; likely affected FT-891, FT-710, FTDX10, FTDX101,
  FT-950 as well.  All three set methods now use a write-only path
  (`_write_command`) that returns immediately after the serial write.

- **Kenwood/Elecraft CAT set commands no longer time out.**  Same root
  cause as the Yaesu fix above: `set_ptt`, `set_freq`, and `set_mode` on
  `KenwoodRig` used `_command()` for set operations.  Kenwood set commands
  (`FA{11d};`, `MD{digit};`, `TX1;`, `RX;`) produce no response on any
  tested hardware (TS-590SG, TS-2000, TS-480, K3).  All three set methods
  now use `_write_command`.  Read commands (`FA;`, `MD;`, `TX;`, `ID;`,
  `SM0;`) are unchanged and continue to use the response-reading path.

- **`?;` error response surfaced as `RigCommandError`.**  When either a
  Yaesu or Kenwood radio responds with `?;` (command rejected or
  unrecognised), `_read_response` previously discarded it as an unsolicited
  message and then timed out with a generic "command timeout" after 1 second
  — hiding the real failure.  `?;` is now detected immediately and raises
  `RigCommandError("Radio rejected command (?)")` with the command prefix
  attached, so log messages and the status bar identify the actual cause.

- **FT-991 (original) added to supported Yaesu models.**  The `YaesuRig`
  docstring previously listed only the FT-991A; the original FT-991 uses
  the same CAT protocol and is equally supported.

---

## [0.2.8] — 2026-04-17

### Added
- **Auto-save filename templates.**  The Images tab in Settings now
  exposes a *Filename template* field and a separate *File format*
  drop-down (PNG or JPG), sharing the same template between RX and
  TX auto-save so the operator gets one consistent naming
  convention across the directory.  The template understands a small
  token vocabulary — ``%d`` (date, ``YYYY-MM-DD`` UTC), ``%t`` (time,
  ``HHMMSS`` UTC), ``%ts`` (Unix epoch), ``%c`` (callsign), ``%m``
  (SSTV mode such as ``Scottie-S1``), ``%rx_tx`` (literal ``RX`` or
  ``TX``), and ``%%`` (literal ``%``); named aliases ``{date}``,
  ``{time}``, ``{callsign}``, etc. are accepted as well.  Unknown
  tokens pass through unchanged so a pattern referencing a
  future-version token never corrupts the current save.  A live
  preview label under the template field shows the concrete filename
  the current pattern would produce right now (using the active
  callsign, the default TX mode, and the current clock), with no
  filesystem touch — change the template, format, default mode, or
  callsign and the preview updates instantly.  Default pattern is
  ``%d_%t_%m`` (e.g. ``2026-04-17_213512_Scottie-S1.png``) —
  filename-sortable, unambiguous across time zones, and safe on
  Windows / macOS / Linux.  Separator collision is handled
  automatically: for a listening-only operator with an empty
  callsign, ``%d_%t_%c_%m`` collapses to
  ``2026-04-17_213512_Scottie-S1.png`` rather than producing the
  awkward double underscore.  Cross-platform filename sanitisation
  strips Windows-forbidden characters (``\\ / : * ? " < > |`` plus
  ``NUL``), collapses whitespace runs to underscore, trims edge
  separators, caps the stem length at 200 characters, and falls back
  to the literal ``sstv`` if sanitisation empties the name.
  Collisions are resolved with a deterministic ``_001`` / ``_002`` /
  ``_NNN`` suffix so a second decode in the same UTC second never
  overwrites the first.
- **Independent TX auto-save.**  A new *Auto-save transmitted images*
  checkbox (Settings → Images) is decoupled from RX auto-save, so
  operators who want to keep a log of every image they put on the air
  for station-portfolio or contest purposes can enable it without
  also auto-saving every RX decode — and vice versa.  The saved file
  is the post-banner composite (whatever was actually modulated on
  air, including any TX identification strip), captured via a new
  ``TxWorker.tx_image_prepared`` signal that fires after banner
  stamping but before encoding begins.  Save happens on
  ``transmission_complete`` so a cancelled or errored TX never
  produces a file that was never actually transmitted; the stashed
  image is cleared on every TX kickoff, on abort, and on error.
  Test tones never emit the signal, so they can never be
  auto-saved.  Both auto-save toggles and the manual save path all
  resolve the same filename template via a new shared
  ``_autosave_image`` helper — one code path, three call sites,
  identical behaviour.

### Fixed
- **TX banner no longer clips on narrow SSTV modes.**  The
  identification strip stamped across the top of transmitted images
  (opt-in, v0.1.19) assumed there was always enough room on the right
  for ``Open-SSTV v{version}`` plus the callsign flush-right.  On
  narrow modes — Martin M2 (160 × 256), Martin M4 (160 × 128),
  Scottie S2 (160 × 256) — the branding ran off the edge and, in the
  worst case, started overlapping the callsign.  A new pure helper
  ``resolve_right_side_text`` implements a three-tier fallback that
  measures the available width before drawing: (1) render the full
  ``Open-SSTV v{version}`` when it fits, (2) drop the version and
  render just ``Open-SSTV`` when the full string overflows, (3) drop
  the brand entirely when even that doesn't fit.  The callsign is
  inviolable — under FCC §97.119 and equivalent rules elsewhere, the
  station ID is the whole point of the banner, so it's the last
  thing to give up space.  On Martin M1 and everything wider the
  rendering is bit-identical to v0.2.7; the tiering only activates
  on modes that actually need it.  Six new tests in
  ``tests/core/test_banner.py`` pin down each tier at the exact
  crossover pixel width.

### Internal
- **New ``open_sstv.templates`` package** containing two pure
  modules that the UI layer depends on: ``tokens.py`` (a testable
  ``TokenContext`` frozen dataclass plus ``resolve_tokens`` with
  ``%%`` escape handling via a sentinel) and ``filename.py``
  (``sanitize_filename_component`` and ``build_autosave_filename``
  with collision resolution).  Zero Qt dependencies — 48 new unit
  tests exercise the resolver + builder headlessly and run in under
  a quarter of a second.  Designed so the v0.3 template compositor
  (docs/design/v0.3_templates.md) can share this vocabulary
  unchanged: a token that works in an auto-save filename today will
  work in a v0.3 template text layer tomorrow.
- **Config schema round-trip tests for the three new fields**
  (``autosave_tx``, ``autosave_filename_pattern``,
  ``autosave_file_format``) in ``tests/config/test_store.py``, plus
  a regression test that older pre-v0.2.8 TOMLs load with sane
  defaults, and one that a hand-edited ``"JPEG"`` or ``"bmp"`` is
  normalised on load rather than handed to the filename builder
  unchanged.

---

## [0.2.7] — 2026-04-17

### Added
- **First-launch callsign dialog.**  New ``FirstLaunchDialog`` prompts
  the operator for their callsign on a truly fresh install so they
  don't have to go hunt through Settings before their first TX.  The
  dialog explains why the callsign is needed (it's stamped on the TX
  banner and keyed as the CW station ID per FCC §97.119 and
  equivalent ID rules in other administrations) and auto-uppercases
  input up to 12 characters.  *Save* persists and populates both the
  radio panel and TX panel immediately; *Skip for now* closes the
  dialog without writing anything.  Either choice flips a new
  ``AppConfig.first_launch_seen`` flag to ``True`` so listening-only
  operators who skip aren't nagged on every launch.

  Migration for upgraders: ``store.load_config`` injects
  ``first_launch_seen = True`` when the key is absent in an existing
  TOML, so anyone upgrading from v0.2.6 or earlier is grandfathered
  in and never sees the prompt.  A missing TOML file (truly fresh
  install) keeps the default ``False`` and fires the dialog via
  ``QTimer.singleShot(0, ...)`` once the main window has painted.

- **Gallery preview on single-click and right-click "View".**
  Previously the only way to see a decoded image was save-to-disk
  and reopen in an external viewer.  Single-click on an RX gallery
  thumbnail now loads the image into the main RX preview with the
  correct mode label and save button re-enabled; a new *View* entry
  is the first item in the right-click context menu, same effect.
  Double-click still triggers the existing *activated* behaviour.

  Implementation note: PySide6's ``QMenu.exec`` is C++-backed and
  can't be monkey-patched from Python, so context-menu dispatch was
  extracted to a testable ``_dispatch_context_action(item, label)``
  helper.  ``QStandardItem.setData`` round-trips ``StrEnum`` values
  as bare ``str``, so a new ``_coerce_mode`` staticmethod rehydrates
  ``Mode`` on retrieval.

### Fixed
- **``QObject::killTimer`` cross-thread warnings on exit.**  The
  RxWorker's wall-clock watchdog ``QTimer`` was being destroyed from
  the GUI thread after the RX thread's event loop had already
  drained, emitting

      QObject::killTimer: Timers cannot be stopped from another thread

  on every clean shutdown.  Added a ``shutdown()`` slot on
  ``RxWorker`` that stops and ``deleteLater()``s the timer from the
  worker's own thread, idempotent across repeat calls and safe when
  the timer was never created.  ``MainWindow`` now emits a new
  ``_request_rx_shutdown`` signal in ``closeEvent`` before
  ``thread.quit()``, so the queued slot runs on the correct thread
  before teardown and the warning is gone.

### Docs
- **v0.3 template compositor design plan** added at
  ``docs/design/v0.3_templates.md``.  Captures the full v0.3 scope:
  layered data model (seven layer types including RX-image slot),
  token resolver supporting both ``%c`` and ``{callsign}`` syntaxes,
  visual template gallery in the TX panel with role filter, dedicated
  editor dialog, TOML persistence format with worked example,
  aspect-ratio handling across all SSTV modes, shipped Tier 1 fonts
  (DejaVu Sans Bold, Inter Bold, Press Start 2P — all open-licensed),
  migration from v0.2.x template strings, and a phased delivery plan
  across v0.3.0 / v0.3.1 / v0.3.2.  Reference material only; no v0.3
  code ships in this release.

### Internal
- Test coverage added in three new test modules
  (``test_first_launch_dialog.py``, ``test_image_gallery.py``,
  expansions to ``test_rx_worker.py``), plus a
  ``_suppress_first_launch_dialog`` fixture in
  ``test_main_window.py`` that stamps ``first_launch_seen=True`` on
  the loaded config so CI never blocks on the modal.  Full suite:
  576 pass / 1 skip.

---

## [0.2.6] — 2026-04-17

### Fixed
- **Windows TX drops the radio mid-transmission (rig-poll vs. PTT
  race).**  On Windows, the 1 Hz rig-status poll (``get_freq`` /
  ``get_mode`` / ``get_strength``) could interleave with the PTT
  write on the same serial port while the radio was mid-transmit,
  triggering a USB CODEC renegotiation that dropped both the virtual
  COM port *and* the USB audio device.  The user-visible symptom was
  "the radio dropped out and I got a connection error" partway into
  every SSTV transmission.  macOS was unaffected because its
  USB-audio stack renegotiates more gracefully on CDC-ACM control
  traffic.

  ``MainWindow._on_tx_started`` now suspends the rig-poll timer for
  the full duration of TX and ``_unlock_rig_controls`` resumes it
  only if the poll was running at TX start *and* the rig is still a
  real backend (not ``ManualRig``) — so a disconnect during TX
  doesn't get silently reinstated.  Matches the "gate CAT during TX"
  pattern used by WSJT-X, JS8Call, and MMSSTV for the same class of
  shared-port contention.

- **Test-tone TX gain slider is now live.**  Moving the TX output
  gain during the 5 s two-tone test signal had no audible effect
  because ``transmit_test_tone`` pre-scaled the entire int16 buffer
  once before playback, so later slider drags never reached the
  soundcard.  Fixed in two layers:

  - ``audio.output_stream.play_blocking`` gains an optional
    ``gain_provider: Callable[[], float] | None`` that is re-read
    once per ~0.1 s playback chunk inside the streaming
    ``sd.OutputStream`` path.  Int16 overflow is clamped per chunk
    with ``np.iinfo`` rather than wrapping to negative.
  - ``TxWorker.transmit_test_tone`` drops the upfront scale and
    passes ``live_gain=True`` through ``_run_tx``, so the test tone
    behaves as a live ALC-calibration knob (slider changes audible
    within ~100 ms).  Regular SSTV TX keeps the pre-scale path for a
    stable envelope across the image.

- **Progressive decode now paints rows as they arrive (MMSSTV-style).**
  The previous 1 s flush cadence between ``InputStreamWorker`` and
  the decoder collected ~6 Robot 36 scan lines per burst, giving the
  UI a distinctly "tick-tick-tick" rhythm.  ``RxWorker`` now selects
  its flush interval dynamically per flush:

  - **Incremental path, IDLE** (hunting VIS) — 1 s.  Keeps the
    pre-v0.2.6 VIS-hunt cadence.  Shortening this multiplied the
    unknown-VIS false-positive rate by 10×, and on that path
    ``Decoder._feed_idle`` trims the rolling buffer past
    ``vis_end``, which mutilated the real VIS arriving moments
    later.  Acoustic (speaker→mic) captures were the worst-hit
    case.
  - **Incremental path, DECODING** (painting lines) — 0.1 s.  VIS
    has already locked, so the IDLE false-positive risk no longer
    applies.  Per-line work inside ``Decoder.feed`` means total CPU
    is independent of flush rate, so the short interval is
    effectively free.
  - **Batch fallback path** — 2 s, regardless of state.  The O(N²)
    reprocessing cost on long Scottie-family receives dominates, so
    responsiveness is traded for throughput.  Responsiveness now
    lives on the incremental path.

  Behavioural impact: first Robot 36 row appears in the gallery
  preview ~10× sooner after the VIS lock, and the preview grows
  smoothly one line at a time instead of in bursts.  VIS detection
  itself is unchanged — same cadence, same thresholds as v0.2.5.

### Changed
- ``assets/icon.png`` and ``docs/icon.png`` refreshed with a
  better-resized version of the application icon.  Linux AppImage
  packaging already copies ``assets/icon.png`` into the bundle at
  build time, so no workflow change was needed.  GitHub Pages site
  (``docs/index.html``) picks up the new icon automatically.

---

## [0.2.5] — 2026-04-16

### Fixed
- **macOS binary actually loads on Apple Silicon (for real this time).**
  v0.2.4 still failed at first dlopen with the same AMFI error::

      code signature in <...> '/.../_internal/Python' not valid for use
      in process: library load disallowed by system policy

  Root cause narrowed down: PyInstaller's launcher bootloader ships
  with the hardened-runtime Mach-O flag baked in.  ``codesign --force
  --sign -`` preserves that flag, so the ad-hoc-signed launcher
  enforces library validation on its own dylibs at dlopen time — and
  since the ~200 ``.so`` files under ``_internal/`` are also ad-hoc
  signed (team ID ``-``), library validation refuses them.

  Fix in ``.github/workflows/build.yml``:

  - ``codesign --remove-signature`` every Mach-O *before* the ad-hoc
    re-sign, so stale hardened-runtime flags on child dylibs don't
    carry through.
  - Re-sign the launcher with ``--options=runtime`` plus new
    ``packaging/macos-entitlements.plist`` entitlements including
    ``com.apple.security.cs.disable-library-validation`` — AMFI then
    accepts the ad-hoc children from the same bundle.

  Users of v0.2.3 / v0.2.4 who hit the AMFI error on download can
  recover their existing bundle without redownloading by stripping the
  stale signatures and re-signing locally::

      cd /path/to/open-sstv
      xattr -cr .
      codesign --remove-signature open-sstv 2>/dev/null
      find . -type f \( -name "*.dylib" -o -name "*.so" -o -name "Python" \) \
        -exec codesign --remove-signature {} \; 2>/dev/null
      find . -type f \( -name "*.dylib" -o -name "*.so" -o -name "Python" \) \
        -exec codesign --force --sign - {} \;
      codesign --force --sign - open-sstv

### Added
- ``packaging/macos-entitlements.plist`` — entitlements file applied by
  the macOS job so ad-hoc-signed bundles survive AMFI's library
  validation pass without requiring Developer ID signing.

---

## [0.2.4] — 2026-04-16

### Fixed
- **macOS binary now actually launches on Apple Silicon.**  The v0.2.3
  ``open-sstv-macos-arm64.zip`` failed at first load with ``code
  signature in <...> not valid for use in process: library load
  disallowed by system policy`` — AMFI (the Apple Mobile File
  Integrity subsystem) was rejecting the ad-hoc signatures on every
  Python extension and dylib inside ``_internal/``.  Two fixes landed
  together:
  - ``open_sstv.spec`` had ``upx=True`` on both the ``EXE()`` and
    ``COLLECT()`` stages.  UPX rewrites Mach-O binaries *after*
    PyInstaller's ad-hoc codesign pass, which invalidates every
    affected ``.dylib`` / ``.so`` signature.  Now gated on
    ``sys.platform != "darwin"`` — UPX stays on Linux and Windows
    where it's safe, and is disabled on Darwin unconditionally.
  - ``.github/workflows/build.yml`` gains a belt-and-suspenders
    ad-hoc re-sign step for the macOS job, between the PyInstaller
    build and the zip.  ``find ... -exec codesign --force --sign -``
    over every ``.dylib``, ``.so``, the embedded ``Python`` binary,
    and the ``open-sstv`` launcher.  Mach-O signatures live in the
    binary header (not xattrs) so they survive the zip → download →
    unzip round-trip; AMFI accepts them on the user's machine.
  Users who already downloaded the broken v0.2.3 macOS bundle can
  recover it locally without redownloading by running the same
  commands: ``xattr -cr`` the extracted folder, then re-sign every
  Mach-O ad-hoc.  The next tag push produces a working macOS zip out
  of the box.

### Known issues
- macOS binaries are still **ad-hoc signed, not Developer ID signed**,
  so first launch still triggers a single Gatekeeper "unverified
  developer" prompt on the launcher.  Proper Developer ID signing +
  notarization is tracked as a separate follow-up (requires a paid
  Apple Developer account).

---

## [0.2.3] — 2026-04-16

### Added
- **Downloadable binaries published automatically on tag push.**
  The ``Build & Release`` GitHub Actions workflow now attaches
  PyInstaller-frozen portable builds to the GitHub Release created for
  each ``v*`` tag.  End users no longer need a Python install or a
  GitHub account to grab a working copy — they download the zip or
  AppImage from the Releases page and run it.
- ``docs/future_work.md`` — captures the post-v0.2.2 design
  discussion on extending ``rx_weak_signal_mode`` into a coordinated
  Weak-signal RX profile (narrower prefilter, relaxed sync
  thresholds, impulse-noise blanker) plus a Settings info-box draft
  explaining how to configure the radio's own filters for SSTV.  No
  code change; intent captured for a future milestone.

### Changed
- **Build matrix drops macOS Intel.**  GitHub's ``macos-13`` runner
  queue times were stalling release builds for tens of minutes while
  the other four targets were already green.  Apple Silicon is now
  the dominant modern Mac target; Intel-Mac users can still install
  from PyPI with ``pipx install open-sstv`` until a Universal2 build
  is wired up.  Builds produced per release: Windows x86_64, Linux
  x86_64 (zip + AppImage), Linux ARM64 (zip + AppImage), and
  macOS Apple Silicon.
- ``assets/icon.png`` is now bundled into the Linux AppImage as the
  application icon (previously a placeholder generated at build
  time).

---

## [0.2.2] — 2026-04-16

### Fixed
- **"Decode timed out" status message now stays visible long enough
  to read.**  User-reported: *"RX watchdog works, I saw the decode
  timeout message briefly.  Maybe allowing that message to stay a
  little longer would be appreciated."*  Root cause: after the
  watchdog reset the decoder to IDLE, the very next ``_flush``
  (usually within ~1 s thanks to the flush cadence) ran into the
  "no events + not decoding" branch and emitted the routine
  ``"Listening… Xs buffered, waiting for signal."`` status update,
  which overwrote the timeout message on the RX panel label before
  the user could read it.

  Fix: a 10 s post-trip cooldown
  (``_RX_POST_WATCHDOG_COOLDOWN_S``) during which the idle-state
  "Listening…" chatter is suppressed.  The timeout message stays
  visible for its full reading window, after which routine status
  updates resume.  A user-initiated ``reset()`` (Clear button)
  clears the cooldown immediately so the "Listening…" updates
  resume right away.

### Tests
- ``TestRxWatchdog.test_timeout_message_not_overwritten_by_listening_during_cooldown``
  in ``tests/ui/test_rx_worker.py`` — trips the watchdog, asserts
  the "timed out" status is emitted exactly once, then runs
  several additional idle-state flushes and confirms no
  "Listening…" updates are emitted during the cooldown window.
- ``_record_signals`` helper extended to capture ``status_update``
  so tests can assert on UI-bound status text directly.

Full suite: 549 → 550 (+1).

---

## [0.2.1] — 2026-04-16

### Fixed
- **RX decoder watchdog now fires even when audio flow stops.**
  User-reported: *"Its stuck on a decode for martin m2 at 93%.
  The image faded 5 minutes ago.  I have no user feedback stating
  the decode has been reset."*  Root cause: the v0.1.36 watchdog
  check lived inside ``_flush()``, which only runs when audio
  chunks arrive.  If PortAudio goes quiet for *any* reason — USB
  audio device sleeps, Bluetooth link drops briefly, OS suspends
  the audio subsystem during a suspend/resume, or a very deep fade
  where the driver produces long stretches of exactly-zero samples
  that don't fill a flush buffer — no flushes fire and the watchdog
  never ticks.

  Fix: an independent wall-clock ``QTimer`` on the RxWorker thread
  now calls the watchdog check every ``_RX_WATCHDOG_TICK_MS = 2 s``
  regardless of audio state.  The timer is created lazily on the
  first ``feed_chunk`` (or ``reset``) so it picks up the worker-
  thread affinity the same way ``InputStreamWorker`` does.  The
  existing flush-driven check remains in place — the two are
  redundant by design, and the timer is the belt.

### Tests
- ``TestRxWatchdog.test_wall_clock_tick_fires_watchdog_even_without_audio``
  in ``tests/ui/test_rx_worker.py`` — sets up a decoder stuck
  partway through an image, backdates the last-progress timestamp
  past the budget, and calls ``_on_watchdog_tick`` directly
  (equivalent to what QTimer does) without any further
  ``feed_chunk`` calls.  Asserts the partial image reaches the
  gallery and the decoder is reset.  Regression guard for the
  user-reported Martin M2 stuck-at-93 % case.

Full suite: 548 → 549 (+1).

---

## [0.2.0] — 2026-04-16 — 🎉 Beta release

Open-SSTV is now **Beta** and ready for user testing and feedback.
The v0.1.x series was the pre-beta stabilisation cycle; this release
closes it out.

### Journey

From the Opus 4.6 audit of v0.1.26
([`docs/audit_opus_1m_v0.1.26.md`](docs/audit_opus_1m_v0.1.26.md))
through v0.1.37, the pre-beta cycle addressed **31 findings** (20 from
the audit, 11 user-reported) and added **85 regression tests**:

- **v0.1.27..v0.1.29** — closed 20 Opus audit findings: TX watchdog,
  serial exception handling, per-line incremental decoder guards,
  banner geometry, Pillow 10.1 pin, and a full README / User Guide
  re-sync across 20 documentation discrepancies.
- **v0.1.28** — per-transmission TX watchdog (was fixed 600 s; now
  `max(30 s, (ptt_delay + samples/rate) × 1.20)` so a stuck Robot 36
  aborts in under a minute instead of ten).
- **v0.1.30..v0.1.31** — image editor crop pipeline rework (crop
  resizes to target, strict 1:1 rendering so resolution changes are
  visible, v0.1.35 added pre-shrink for oversized sources).
- **v0.1.32** — auto-shrink + clamp for text overlays so narrow
  modes (Martin M2, Scottie S2, M4, S4) don't let text spill off the
  image edge.
- **v0.1.33** — persisted settings now applied at startup, not only
  after re-opening Settings.
- **v0.1.34** — image editor strict 1:1 view with centre alignment
  + dark canvas + startup version log for install diagnostics.
- **v0.1.35** — editor pre-shrink for oversized sources; template
  editor X/Y spin boxes with "Custom" position matching the image
  editor's v0.1.23 UX.
- **v0.1.36** — RX decoder watchdog (mirror of TX watchdog
  philosophy); QSO-template Custom-position x/y fix in both
  TX-apply paths.
- **v0.1.37** — TX preview target outline + aspect match/mismatch
  status label reacting to mode changes in real time.

### Status

- **Test suite**: 548 passed, 1 deliberate skip, ~5.5 min full run
- **22 SSTV modes**: Robot 36; Martin M1/M2/M3/M4;
  Scottie S1/S2/DX/S3/S4; PD-50/90/120/160/180/240/290;
  Wraase SC2-120/180; Pasokon P3/P5/P7
- **Rig control**: rigctld (Hamlib), direct serial CAT for Icom
  CI-V / Kenwood / Yaesu / PTT-only DTR/RTS
- **Platforms**: Linux and macOS (supported); Windows has
  installation docs but has not yet been tested on real hardware

### Development status classifier

PyPI classifier bumped from `"Development Status :: 2 - Pre-Alpha"`
to `"Development Status :: 4 - Beta"`.

### What to file issues about

Beta testers — please share findings. The README's new
*Testing focus areas* section lists the surfaces we'd most like
eyes on: weak-signal RX, TX output level calibration, CW station
ID audibility, TX preview outline behaviour across modes, rig
control edge cases, macOS privacy prompts on launch, and Windows
installs.

---

## [0.1.37] — 2026-04-16

### Added
- **TX preview target outline + status label.**  User-reported:
  *"The TX window does not reflect the mode the user selected.
  User selects Martin M1 and crops image.  User moves to Martin M2.
  The TX window still shows the images for Martin M1."*  Root
  cause: the TX panel stores the edited image at the editor-time
  target's pixel dimensions and never re-preps it on mode change.
  At TX time the image IS resized to the new mode's dimensions, but
  the preview was misleading — and aspect-mismatch cases (M1 → M2,
  M1 → M3) produce a distorted output that the preview didn't warn
  about.

  Two independent cues added:

  * **Dashed outline** painted on top of the scaled preview pixmap
    showing the selected mode's aspect-ratio box, centered on the
    image.  Soft green when the source aspect matches the target,
    amber when it doesn't.  Drawn with a 2-pixel dashed pen, no
    dimming — the full source image remains visible underneath.

  * **Status label** beneath the preview showing current image
    dimensions, selected mode's target dimensions, and a match /
    mismatch verdict.  Three variants:

    * native-resolution match → green: "Image 320×240 matches
      robot_36 target — TX will encode at native resolution."
    * aspect match (different size) → green: "aspect matches;
      LANCZOS resize on TX, no distortion."
    * aspect mismatch → amber: "aspect mismatch; image will be
      stretched.  Consider re-editing for the new mode."

  The preview pixmap itself is NOT automatically re-prepped on
  mode change — that would silently distort user content behind
  their back.  The two cues keep the user informed without mutating
  their data.

### Tests
- ``TestTxTargetStatus`` in ``tests/ui/test_tx_panel.py`` (4 new):
  empty-state before image load; green status on aspect match
  (M1 source in M1 slot); amber status on aspect mismatch (M1
  source switched to M2); status-label refreshes when the mode
  combo changes (the explicit user-reported-bug regression guard).

Full suite: 544 → 548 (+4).

---

## [0.1.36] — 2026-04-16

### Added
- **RX decoder watchdog.**  User-reported: *"someone sends an image
  but the noise drops out enough to cause the decoder to get lost.
  The decoder will forever try to decode the transmission long after
  the source has stopped."*

  The ``RxWorker`` now tracks how long it's been in the DECODING
  state and how long since the last ``ImageProgress``.  If either
  budget is blown, the decoder is reset and returned to IDLE so the
  next VIS is caught normally.  Two independent trip conditions:

  * **Total-elapsed** — more than
    ``max(15 s, mode.total_duration_s × 1.5)`` since VIS detection.
    A Pasokon P7 gets ~10 minutes of headroom; a Robot 36 gets a
    15 s floor so a brief fade doesn't trip it.
  * **Per-line no-progress** — no new line for
    ``max(5 s, 5 × mode.line_time_ms)`` at a time.  Reacts to a
    mid-image fade within a few seconds on fast modes, with a
    floor so the fastest mode (Robot 36) isn't hair-triggered.

  When the watchdog trips, whatever partial image the decoder has
  accumulated is emitted as a normal ``image_complete`` so it still
  lands in the gallery (truncated to the mode's resolution with
  black rows for un-decoded lines).  A status-bar message quotes the
  trip reason: "*Decode timed out (no progress for 23 s) — kept
  partial 120/240 lines.*"  Mirrors the TX watchdog philosophy from
  v0.1.28 (per-transmission budget + safety floor).

### Fixed
- **QSO template Custom-position x/y now apply correctly.**
  User-reported: *"creating a new template with text at a custom
  location does not function properly.  The text is all overlayed at
  the top left of the image."*  Root cause: two separate code paths
  that convert ``QSOTemplateOverlay`` objects to plain ``dict``s —
  ``TxPanel._on_template_activated``'s no-user-input branch and
  ``QuickFillDialog.resolved_overlays`` — both dropped the ``x`` and
  ``y`` fields when building the dict.  When ``draw_text_overlay``
  later received a dict with ``position="Custom"`` but ``x=None``,
  it fell through to ``position_to_xy("Custom", ...)`` which returned
  ``(margin, margin)`` and the text rendered at (8, 8) — top-left.
  Both dict-builders now forward ``x`` / ``y``, so the user's
  saved coordinates take effect on every template apply.

### Tests
- ``TestRxWatchdog`` in ``tests/ui/test_rx_worker.py`` (5 new):
  trip on no-progress; trip on total-elapsed; no trip during healthy
  decode; state cleared on clean ImageComplete; state cleared on
  reset().
- ``TestCustomPositionTemplateRendering`` in
  ``tests/ui/test_tx_panel.py`` (2 new): end-to-end pixel check that
  a Custom-position template renders at its saved coordinates (and
  *not* at top-left); plus ``QuickFillDialog.resolved_overlays``
  forwards x/y through the placeholder path.

Full suite: 537 → 544 (+7 new, +0 regressions).

---

## [0.1.35] — 2026-04-16

### Changed
- **Image editor pre-shrinks oversized sources on entry.**  User
  feedback: "I'm not a fan of the new crop system, some extremely
  large images can be quite awkward to try and crop."  v0.1.34's
  strict-1:1 rendering made a 4032 × 3024 phone photo overwhelm the
  viewport — the crop rectangle lived mostly off-screen and the
  user had to pan-and-scroll constantly to select a region.

  The ``ImageEditorDialog`` now passes incoming images through a
  new ``_shrink_source_for_editor`` helper before using them as the
  working copy.  The editor working image is capped at the smaller
  of 3 × target dimensions or 1280 × 960 absolute, preserving aspect
  ratio, never upscaling.  A 4032 × 3024 phone photo editing for
  Robot 36 (320 × 240) becomes a 960 × 720 working copy — comfortable
  to interact with — while the final LANCZOS resize to target at OK
  time produces output that's visually indistinguishable from
  resizing the raw source (a Robot 36 image is 320 × 240, so the
  source pixels above ~3 × target are all discarded anyway).

### Added
- **Template editor X/Y spin boxes.**  The template editor now has
  the same pixel-precise placement UX as the image editor got in
  v0.1.23: a "Custom" entry in the Position combo, X / Y spin
  boxes (5-pixel step, ranging 0–320 × 0–240 in the editor's
  preview-canvas coordinate space), and a helper label explaining
  the portable coordinate system.  Matches the existing image editor
  pattern: manually editing X/Y flips the combo to Custom; selecting
  a named preset clears X/Y and re-seeds the spin boxes from the
  preset's computed position.  Values written out to ``templates.toml``
  are preview-relative; at TX time the renderer auto-shrinks and
  clamps via the v0.1.32 ``clamp_xy_to_image`` helper so the
  placement is portable across all 22 target modes.

### Tests
- ``TestTemplateEditorXYSpinboxes`` (4 new tests in
  ``tests/ui/test_template_editor_dialog.py``):
  - loading a Custom overlay populates the combo + spin boxes;
  - loading a named-preset overlay seeds the spin boxes from the
    preset's computed position;
  - editing X or Y flips the combo to Custom and the change
    persists through ``result_templates()``;
  - selecting a named preset clears x/y.

---

## [0.1.34] — 2026-04-16

### Fixed
- **Image editor renders at strict 1:1 pixel scale.**  v0.1.30 made
  Apply Crop resize to target dimensions; v0.1.31 tried to show the
  result without upscaling via ``resetTransform + centerOn``.  The
  user-reported outcome was still broken: the cropped image continued
  to look the same size as the source in the viewport.  Root cause:
  the "fit if bigger, 1:1 if smaller" logic relied on
  ``QGraphicsView``-level anchor and viewport semantics that didn't
  actually produce a visible size change on the user's setup.

  New approach: configure the view once in ``__init__`` with
  ``AlignCenter`` alignment, ``ScrollBarAsNeeded`` scrollbars, and a
  darker canvas background.  ``_refresh_preview`` then just resets
  the transform to identity and calls ``viewport().update()``.  The
  image now *always* occupies exactly ``image_width × image_height``
  pixels of the viewport — Apply Crop visibly shrinks an 800×600
  source to a 320×240 preview that takes up a quarter of the space,
  so the resolution change is impossible to miss.  Large images
  (PD-290 800×616, Pasokon P7 640×496) scroll via the native
  scrollbars rather than scaling down.

- **Startup version log** emitted on stderr when launching
  ``open-sstv``: ``Open-SSTV v0.1.34 starting — module loaded from
  /path/to/open_sstv/__init__.py``.  Diagnostic aid for the "About
  dialog shows an old version" class of reports, which almost always
  means the ``open-sstv`` script on ``PATH`` is pointing at a Python
  environment different from the one ``pip install -e .`` ran
  against (a stale ``site-packages/open_sstv/`` from before the
  editable install was set up).  The log makes the live module
  path unambiguous so the user can diff it against their venv.

### Tests
- ``TestRefreshPreviewSceneRect`` updated:
  - ``test_view_transform_is_always_identity`` (renamed from
    ``test_view_transform_is_identity_for_small_image``) — the view
    is now at 1:1 for every image size, before and after Apply Crop.
  - ``test_view_shows_scrollbars_for_oversized_image`` (renamed
    from ``test_view_scales_down_when_image_exceeds_viewport``) —
    large images stay at 1:1 too; oversized scenes get scrollbars
    from the built-in ``ScrollBarAsNeeded`` policy instead of
    triggering ``fitInView``.

---

## [0.1.33] — 2026-04-16

### Fixed
- **Persisted settings are now applied at startup, not only after the
  Settings dialog closes.**  User-reported: *"the app does not respect
  previously set mic gain levels."*  Every worker-owned setting in
  ``AppConfig`` (RX input gain, TX output gain, PTT delay, CW ID
  enable/WPM/tone, TX banner enable/colours/size, TX panel sample rate
  label) was loaded from TOML into ``AppConfig`` but the workers
  themselves were constructed with their hard-coded defaults
  (``_input_gain = 1.0``, ``_output_gain = 1.0``, ``_cw_id_enabled =
  False``, etc.) and never received the user's values until the user
  opened Settings and clicked OK — which invoked ``_apply_config``
  that has always pushed every field to the right worker.

  Fix: ``MainWindow.__init__`` now seeds each worker from
  ``self._config`` **before** moving it to its thread.  Direct setter
  calls (``set_output_gain``, ``set_cw_id``, ``set_tx_banner``,
  ``set_input_gain``) run while the worker is still on the GUI thread
  and write plain Python attributes — no queued cross-thread signals
  emitted from ``__init__``.  The RX input gain, TX output gain, CW
  ID, TX banner, PTT delay (now a ``TxWorker`` constructor kwarg),
  and TX panel sample-rate/default-mode labels now all take effect on
  the first launch after a Settings save.

### Tests
- Verified manually end-to-end: spinning up Open-SSTV with a
  pre-existing ``config.toml`` containing ``audio_input_gain = 0.75``,
  ``audio_output_gain = 0.13``, ``ptt_delay_s = 0.5``,
  ``cw_id_enabled = true``, and ``tx_banner_enabled = true`` now
  shows every value applied before any Settings interaction.
- No automated regression test was committed: constructing a second
  ``MainWindow`` with an explicit ``config=AppConfig(...)`` kwarg
  inside pytest-qt on macOS produces a deterministic teardown
  segfault in a worker thread that reproduces even with
  ``sounddevice`` fully monkey-patched.  Plain-Python invocation of
  the same code works perfectly.  Tracked in ``tests/ui/
  test_main_window.py`` as a block comment; the fix ships without
  the automated guard pending a pytest-qt / macOS investigation.

---

## [0.1.32] — 2026-04-16

### Fixed
- **Text overlays no longer spill off the image on narrow modes.**
  User reported that the Exchange QSO-template preset's second overlay
  (``UR {rst} {date}`` at 20 pt) rendered wider than the 160-pixel
  width of Martin M2 / Scottie S2 / M4 / S4.  ``position_to_xy``'s
  centring math produced a negative ``x`` (``(160 − 200) / 2 = −20``),
  so PIL happily drew the first 20 px of the text off the left edge
  and the remaining ~180 px trailed off to the right.  Two defences
  added to ``draw_text.py``:

  * **Auto-shrink** — ``draw_text_overlay`` now reduces the font size
    one point at a time (down to ``_MIN_FONT_SIZE = 8``) until the
    text fits inside ``image_width − 2 × _MARGIN``.  Common long-ish
    Exchange strings drop from 20 pt to ~14 pt on 160-wide modes
    and the full text is visible.
  * **Bounds clamp** — a new ``clamp_xy_to_image`` helper pins the
    final position so the 1 px drop-shadow ring always stays on-image,
    even for text that's still too wide to fit at minimum font size
    (extreme case: very long callsign on the narrowest mode).
    Applies to both named presets and Custom X/Y coordinates, so the
    image editor's manual placement also can't produce off-image text.

### Tests
- New ``tests/ui/test_draw_text.py`` with three test classes:
  - ``TestClampXYToImage`` — unit tests for the clamp helper
    (within-bounds unchanged, negative-x clamped, over-right-edge
    clamped, text-wider-than-image falls back to 1 px).
  - ``TestAutoShrinkAndClamp`` — integration tests that render to a
    real PIL canvas and verify every white pixel falls inside the
    image.  Explicitly covers the Exchange-on-160-wide case the user
    reported, plus Martin M4 (160 × 128, the smallest mode we ship)
    for both Exchange overlays.
  - ``TestPositionToXY`` — pins the raw preset math (still allowed to
    go out-of-bounds; clamping is the caller's job) so future tweaks
    don't silently break the image editor's Position → X/Y auto-fill.

---

## [0.1.31] — 2026-04-16

### Fixed
- **Image editor: preview now visibly reflects the post-crop resolution.**
  v0.1.30 fixed the underlying behaviour (Apply Crop resizes to target
  dimensions) but the rendered preview didn't visibly change because
  ``_refresh_preview`` called ``fitInView`` unconditionally — both the
  800×600 original and the 320×240 cropped result are 4:3, so both
  filled the viewport identically and the user still couldn't tell
  anything had happened.  The view now resets its transform to 1:1
  first and only falls back to ``fitInView`` when the scene genuinely
  exceeds the viewport.  Small previews (any mode whose target fits
  within the dialog's allocated view area) render at their actual
  pixel size, centred in the viewport, so a post-Apply-Crop 320×240
  image looks noticeably smaller than the pre-crop 800×600.
- **Image editor info label styled for visibility.**  The "Image: W×H"
  label now has a bold weight, padding, and a subtle bordered
  background so the pixel count is an unmissable signal that the
  working image has changed size.  Previously it rendered in the
  same weight and colour as every other label in the right panel and
  was easy to overlook.

### Tests
- ``TestRefreshPreviewSceneRect`` in ``tests/ui/test_image_editor.py``
  — 3 tests covering the rendering contract: scene rect matches the
  working image after Apply Crop; view transform is identity (1:1
  scale) when the image fits; ``fitInView`` still runs when a large
  target (PD-290 800×616) exceeds a small viewport.

---

## [0.1.30] — 2026-04-16

### Fixed
- **Image editor: Apply Crop now crops *and* resizes to the target mode's
  native dimensions in one click.**  Prior to v0.1.30 the resize happened
  silently in ``_on_accept`` when the dialog was closed.  If the loaded
  image already matched the target aspect ratio (e.g. an 800×600 photo
  into a 4:3 Robot 36 slot), Auto-fit Crop produced a full-image crop
  box and Apply Crop then cropped-to-same-size — a visual no-op that
  left the user thinking the button was broken, and required them to
  hit OK and reopen the editor to see the 320×240 result.  The crop
  now resizes to target (LANCZOS, same filter and call order as
  ``_on_accept``) so what the user sees in the preview is exactly what
  gets encoded and transmitted.  The info label updates to "…
  (resized to target)" so the operation is explicit.  Apply-Crop-
  then-OK is pixel-equivalent to the old OK-only path.

### Docs
- **README and User Guide re-synced against current behaviour.** Both documents
  had accumulated stale content across v0.1.3..v0.1.29 — the User Guide in
  particular still carried the pre-rename `sstv-app-*` command names, claimed
  the app supports "three SSTV modes", had four wrong Hamlib model numbers
  (IC-7300, TS-590SG, FT-991A, FT-817/818 — all shuffled), and described the
  TX banner as overwriting the top 24 rows (the v0.1.23 push-down behaviour
  means it never does). The README claimed a fixed 300 s TX watchdog (now
  per-transmission as of v0.1.28), described final slant correction as
  unconditional (opt-in since v0.1.18), and omitted six shipping features
  (CW station ID, Test Tone, TX output overdrive, weak-signal mode, banner
  size selector + preview, incremental decoder). Every identified discrepancy
  from the Opus audit's doc-review pass (D-01..D-20) is addressed.
- User Guide version header bumped from `Version 0.1.2` to `Version 0.1.30`.
- README `Status` line updated to `Pre-beta (v0.1.30)` with a note about the
  v0.1.27/28/29 audit-fix field-testing gate.

### Tests
- ``TestApplyCropResizesToTarget`` in ``tests/ui/test_image_editor.py``
  — 5 tests covering the common cases: same-aspect source (the
  original bug), wider source that needs cropping before resize,
  manual small crop that upscales, Apply-Crop-then-OK pixel
  equivalence, and a larger target (PD-290 800×616) from a smaller
  source.

---

## [0.1.29] — 2026-04-16

Second-pass polish on the Opus 1M audit findings that were deferred in
v0.1.27.  Five items: two P2, three P3, plus dependency pin bump and
associated tests.

### Fixed
- **OP-13 (P2) — rigctld launcher rejects leading-dash serial-port
  values.** New ``is_safe_rigctld_arg`` helper in ``radio/rigctld.py``
  returns ``False`` for values that start with ``-`` (after lstrip),
  which closes the arg-smuggling gap at the ``subprocess.Popen``
  boundary: a hand-edited config could otherwise pass
  ``rig_serial_port = "--help"`` (or worse) as a positional arg and
  rigctld would parse it as a flag.  Both launch sites
  (``MainWindow._connect_rigctld`` and
  ``SettingsDialog._launch_rigctld``) now validate before assembling
  the argv and show a user-visible error when the validation fails.
- **OP-22 (P2) — ``RxWorker._flush`` asserts at most one
  ``ImageComplete`` per feed.** The ``Decoder.feed`` contract is
  one-complete-per-call (it auto-resets to IDLE after emitting one),
  but the dispatch loop didn't enforce it.  A future change that
  violated the contract would have silently emitted the progressive
  image instead of the slant-corrected re-decode on the second and
  later completes because ``consume_last_buffer()`` drains on the
  first.  Fail loudly instead.
- **OP-28 (P3) — ``RigConnectionMode`` StrEnum.** Replaces three
  ad-hoc string literals (``"manual"`` / ``"serial"`` / ``"rigctld"``)
  that lived separately in ``config/schema.py``, ``ui/settings_dialog.py``,
  and ``ui/main_window.py``.  StrEnum preserves wire compatibility with
  existing TOML configs while giving a single source of truth.

### Changed
- **OP-32 — ``Pillow>=10.1,<12``** (was ``>=10.0,<12``).  Bumped the
  minimum so the ``ImageFont.load_default(size=...)`` kwarg is
  always available; dropped the ``TypeError`` fallback in three
  places (``core/banner.py``, ``ui/draw_text.py``,
  ``ui/image_editor.py``).  Pillow 10.1 was released in October 2023,
  so this is safely below the realistic deployment floor for a 2026
  app.

### Tests
- **OP-30 — Focused tests for ``TxWorker.emergency_unkey`` /
  ``wait_for_stop``.**  Previously exercised only indirectly by
  ``closeEvent`` integration tests.  New ``TestEmergencyUnkey``
  covers the single-PTT-call contract, ``RigError`` and arbitrary-
  exception suppression, and verifies the rig lock is held so a
  concurrent ``set_rig`` can't race.  ``TestWaitForStop`` covers
  timeout-returns-False, flag-already-set, and flag-set-during-wait
  from another thread.
- **OP-13 — ``TestIsSafeRigctldArg``** in
  ``tests/radio/test_rigctld_client.py`` covers every-case of the new
  validator: device paths accepted, empty/None accepted, leading
  dash rejected, whitespace-padded dash rejected, mid-value dash
  accepted.

Test run: 498 → 510 passed (+12 net) in the same ~5.5 minute budget.

---

## [0.1.28] — 2026-04-16

### Changed
- **TX watchdog is now per-transmission instead of a fixed 600 s.**
  Follow-up to OP-01 in v0.1.27, which raised the old 300 s constant
  to 600 s to cover Pasokon P7 (406 s).  Keeping a 600 s constant left
  short modes with up to 10 minutes of stuck-rig exposure — a
  regulatory liability on a 36 s Robot 36 that should never need
  more than ~1 minute of headroom.  The new design is a two-stage
  watchdog:

  * **Stage 1 (encode-time, fixed 30 s via ``_ENCODE_WATCHDOG_S``)**
    covers banner stamping, encoding, gain, and CW append.  Encode is
    CPU-bound and takes ~100 ms even for Pasokon P7, so 30 s is just
    a defence against a wedged encoder.
  * **Stage 2 (playback, per-transmission)** is computed after the
    encoded sample array is known, via
    ``_compute_playback_watchdog_s(samples_n, sample_rate, ptt_delay_s)``:
    ``max(_PLAYBACK_WATCHDOG_FLOOR_S, (ptt_delay_s + samples_n/sample_rate) × _PLAYBACK_WATCHDOG_MARGIN)``
    with a 30 s floor and a 1.20 multiplicative margin.  Because
    ``samples_n`` already includes the VIS leader and any appended
    CW tail, the formula scales automatically with mode duration,
    CW WPM setting, and callsign length — no per-mode tables to
    maintain.

  Result: a stuck Robot 36 transmission aborts at ~51 s instead of
  600 s; a stuck Pasokon P7 still gets its full ~500 s budget.
  Test tone (5 s tone + PTT delay) gets the 30 s floor.

- **``TxWorker.watchdog_fired`` now emits the budget that fired
  (``Signal(float)``).**  MainWindow formats the persistent status
  message from the signal payload instead of a hardcoded constant,
  so "TX watchdog: exceeded N s" always quotes the actual value —
  useful diagnostic for a user wondering why their long TX was cut
  short.

### Tests
- Replaced the v0.1.27 ``test_watchdog_covers_every_mode_with_headroom``
  constant-floor check with ``TestComputePlaybackWatchdog``:
  - Floor engages on short transmissions (5 s tone → 30 s budget).
  - Multiplicative margin on long ones (400 s → 480.24 s budget).
  - Every ``Mode`` in ``MODE_TABLE`` gets non-negative headroom over
    its worst-case TX (body + VIS + 12 s CW tail + PTT delay).
  - Robot 36 budget now < 120 s (regulatory tightening vs. the old
    600 s), regression guard against reverting the formula.
  - Defensive: ``fs=0`` returns the floor instead of dividing by zero.
- New ``TestTwoStageWatchdogIntegration`` that patches
  ``threading.Timer`` to capture construction durations and verifies
  ``transmit()`` creates both stages in the right order with the
  right budgets.
- ``watchdog_fired`` signal test confirms the duration payload is
  forwarded correctly.

---

## [0.1.27] — 2026-04-16

Fixes from the Opus 4.6 (1M ctx) audit (`docs/audit_opus_1m_v0.1.26.md`).
Two P0s, eight P1s, six P2s, two P3s.

### Fixed
- **OP-01 (P0) — TX watchdog raised from 300 s to 600 s.** Pasokon P5
  (304 s) and Pasokon P7 (406 s) used to trip the watchdog mid-image —
  the receiving station saw a truncated transmission. The new ceiling
  comfortably covers every shipping mode plus VIS leader, PTT delay,
  and a 15 WPM CW tail. New regression test
  `test_watchdog_covers_every_mode_with_headroom` asserts the invariant
  against `MODE_TABLE` so a future longer mode fails loudly.
- **OP-02 (P0) — Serial CAT backends translate `serial.SerialException`
  to `RigConnectionError`.** Icom CI-V, Kenwood, and Yaesu `_command`
  methods plus `SerialPttRig.{get,set}_ptt` now wrap pyserial exceptions.
  Previously a mid-session USB unplug leaked a raw `SerialException`
  past every `RigError` catch in the rig poll thread, killing the
  thread silently — the rig panel froze and only an app restart
  recovered. New `TestSerialExceptionWrapping` regression suite covers
  all four backends.
- **OP-03 (P1) — `TemplateEditorDialog` deep-copy preserves
  `x` / `y` overlay coordinates.** The dialog used to silently strip
  the optional pixel-position fields on every Open, then erase them
  from disk on Save. Users who hand-edited `templates.toml` to add
  precise placement lost their work the moment they opened the editor.
  Two new regression tests in `tests/ui/test_template_editor_dialog.py`.
- **OP-04 (P1) — BZ-03 regression tests are no longer skipped.** The
  `TestCropXYSpinboxUpdatesRect` class was unconditionally skipped at
  class level with the claim that it required a display, but it runs
  fine under the offscreen Qt platform pytest-qt uses. Class is
  unskipped and the four tests now actually guard the BZ-03 fix.
- **OP-05 (P1) — RX start sequences reset → start_capture deterministically.**
  Previously, `_request_rx_reset` (queued to rx_thread) and
  `_request_start_capture` (queued to audio_thread) raced: a chunk from
  an already-warm device could arrive at `feed_chunk` before the reset
  slot ran, leaving stale state in the decoder. `RxWorker.reset()` now
  emits a new `reset_done` signal; MainWindow connects a one-shot
  callback that emits `_request_start_capture` only after `reset_done`
  fires.
- **OP-06 (P1) — TX progress bar honours the configured sample rate.**
  `TxPanel.show_tx_progress` used to compute elapsed/total seconds
  with a hardcoded `/ 48000`; on 44.1 kHz a 114 s Martin M1 transmission
  showed *"124 s / 124 s"* at completion. New `TxPanel.set_sample_rate`
  method called from `MainWindow._apply_config` keeps the panel in
  sync with the active rate.
- **OP-07 (P1) — Stale incremental-decode fallthrough comment removed.**
  `Decoder._feed_idle` used to comment that an unknown mode would fall
  through to the batch path — but `make_incremental_decoder` covers
  every `Mode` value, so the path was dead. The fallthrough is replaced
  with an explicit `assert` that fails loudly if a future Mode addition
  forgets a backend.
- **OP-08 (P1) — `emergency_unkey` runs in a daemon thread with a
  bounded join.** On app shutdown, if the TX worker thread doesn't
  finish within its 3 s budget, MainWindow used to call `emergency_unkey`
  synchronously on the GUI thread — which would block for up to ~1.5 s
  (serial write_timeout + read budget) on an unresponsive radio. Now
  runs in a daemon thread with a 1.5 s join, so a dead-rig timeout can't
  freeze the GUI past the close.
- **OP-09 (P1) — All per-worker config changes flow through queued
  signals.** `set_final_slant_correction` and `set_sample_rate` (TX)
  used to be direct method calls from the GUI thread, relying on
  GIL-atomic int/bool assignment for safety. Both now have `@Slot`
  decorators and are dispatched via the new
  `_rx_final_slant_correction_changed` and `_tx_sample_rate_changed`
  signals, so every worker setting genuinely lands on its receiver's
  own event loop. Symmetry > convenience.
- **OP-11 (P1) — Audio input watchdog gets a 6 s cold-start grace.**
  The 3 s watchdog used to fire spuriously on slow-to-open USB and
  Bluetooth devices that took 1.5–2.5 s between `start()` and the
  first PortAudio callback. The watchdog now starts at
  `_DEVICE_WATCHDOG_COLD_START_MS = 6000` and switches to the
  steady-state 3 s after the first chunk drains.
- **OP-12 (P2) — `RxWorker.set_sample_rate` resets `_total_samples`.**
  The "Xs buffered" status label used to be briefly off-by-rate after
  a mid-session sample-rate change because the sample counter still
  held the old-rate count.
- **OP-15 (P2) — CW generator surfaces unsupported characters at
  WARNING level.** Characters not in the Morse table (any non
  A–Z / 0–9 / `/` / `-`) used to be silently skipped at DEBUG. The
  WARNING tells the operator their station ID may be incomplete —
  important for regulatory compliance.
- **OP-17 (P2) — Robot 36 wire-format detection is bounded.** A
  noise-locked input that never produces enough sync candidates used
  to grow `Robot36IncrementalDecoder._pending` to an entire image's
  worth of audio (~14 MB at 48 kHz). After 3 s of buffered audio
  without enough candidates, the decoder falls back to the per-line
  backend as a sane default.
- **OP-18 (P2) — Status bar surfaces missing saved audio devices.**
  Previously the app silently fell back to the system default when
  the saved input/output device wasn't found (USB unplugged since
  last run). The user now sees a 10 s status-bar message naming the
  missing device(s).
- **OP-19 (P2) — `_kill_rigctld` and `SettingsDialog._stop_rigctld`
  handle already-dead processes.** `terminate()`/`wait()`/`kill()`
  raises `ProcessLookupError` (POSIX) or `OSError` if the rigctld
  process died on its own (bad CLI args, port collision). Both
  cleanup paths now treat that as "already gone" rather than
  propagating the exception out of `closeEvent`.
- **OP-21 (P2) — `ImageGalleryWidget` uses `aboutToQuit` instead of
  `atexit` for temp-dir cleanup.** Scoped to the Qt application
  lifetime rather than the interpreter, avoiding the per-test atexit
  callback accumulation that occurred under pytest-qt.
- **OP-23 (P2) — `apply_tx_banner` raises `ValueError` for too-small
  images.** Previously, when `image.height <= banner_height` the
  resize was silently skipped and the entire output was a banner-
  coloured rectangle with no image content. Today's smallest mode
  (height 128 px) plus largest banner (40 px) leaves 88 px clearance,
  so this never fires in practice — but it would be a worst-case
  failure mode for any future small mode.
- **OP-33 (P3) — S-meter sentinel comment.** Documents that
  `strength_db == 0` is the "no reading" sentinel and that a genuine
  0 dBm reading would be ~S9+73 (off the top of the meter), so the
  collision is cosmetic. No behavioural change.

### Tooling
- **OP-34 (P3) — `pyproject.toml [tool.pytest.ini_options].pythonpath`
  now includes `"."`** alongside `"src"`, so `pytest -q` at the repo
  root works without a manual `PYTHONPATH=.` prefix
  (`tests.radio.fake_rigctld` import was failing otherwise).

### Tests
- `tests/ui/test_tx_worker.py::test_watchdog_covers_every_mode_with_headroom`
  — pins the OP-01 watchdog ≥ longest mode + 30 s slop invariant.
- `tests/radio/test_serial_rig.py::TestSerialExceptionWrapping`
  — verifies OP-02 wrapping for Icom / Kenwood / Yaesu / SerialPttRig.
- `tests/ui/test_template_editor_dialog.py` — verifies OP-03 X/Y
  round-trip and that the dialog's deep copy isolates the caller.
- `tests/ui/test_image_editor.py::TestCropXYSpinboxUpdatesRect`
  — unskipped; the four BZ-03 regression tests now actually run.

---

## [0.1.26] — 2026-04-15

### Fixed
- **BZ-01 — rigctld orphaned on Settings Cancel.** `SettingsDialog.reject()` now
  overrides `QDialog.reject()` to call `_stop_rigctld()` before delegating to
  `super()`. Previously, cancelling the dialog after clicking "Launch rigctld Now"
  left a dangling `hamlib` process running (port remained locked until the next
  dialog open or app restart). `accept()` is unchanged — `rigctld_process` is
  still transferred to `MainWindow._rigctld_proc` on OK so the connection persists.
- **BZ-02 — TX banner preview showed stale callsign.** `_refresh_banner_preview`
  now passes `self._callsign.text().strip().upper()` instead of
  `self._config.callsign`, so the live preview reflects edits made to the Callsign
  field without saving. `_callsign.textChanged` is also connected to
  `_refresh_banner_preview_if_built` so the Images tab preview updates as the user
  types in the Radio tab.
- **BZ-03 — Crop X/Y spinboxes did not move the visual crop rectangle.**
  `_crop_x.valueChanged` and `_crop_y.valueChanged` are now connected to
  `_update_crop_rect` in `ImageEditorDialog`. Manually typing a crop position now
  immediately repositions the yellow dashed crop overlay. The drag callback
  (`_on_crop_rect_dragged`) already blocks these signals on drag-sync, so there is
  no circular feedback loop.
- **BZ-04 — Watchdog abort message hardcoded `300`.** `_on_tx_aborted` in
  `MainWindow` now formats the watchdog duration from `_MAX_TX_DURATION_S`
  (imported from `workers`) so the UI message stays in sync if the constant
  is ever tuned.
- **BZ-05 — Dead code `parentWidget()` call removed.** The vestigial
  `self._civ_address_spin.parentWidget()  # trigger layout` line in
  `_on_serial_protocol_changed` was a no-op (`parentWidget()` is a pure
  getter with no side effects) and was deleted.
- **BZ-06 — `save_templates` now logs before raising `OSError`.** Matches the
  `save_config` pattern: `_log.error(...)` is emitted before re-raising so
  the failure appears in the application log, not just in the caller's dialog.
- **BZ-07 — Robot 36 wire-format detection is now O(total samples) instead of O(N²).**
  `Robot36IncrementalDecoder._try_detect` previously re-ran bandpass + Hilbert
  over the entire pending buffer on every `feed()` call during the ~450–900 ms
  detection window. It now tracks `_detection_processed` and only processes new
  audio (with a `_MIN_BP_SAMPLES` warm-up overlap), accumulating sync candidates
  across calls in `_detection_cands`. Total DSP work during detection is bounded
  by total samples + N × 256 (filter overlap) rather than N × total samples.
- **BZ-08 — Stale "v0.2" comment on `_DECODE_FLUSH_INTERVAL_S` corrected.**
  The comment now correctly attributes the revert to v0.1.25 instead of a future
  "v0.2" that had already passed.
- **BZ-09 — `_open_settings` `finally` block now disconnects all 7 signal
  connections** (previously 4). Lambda references for `output_gain_changed` and
  `rejected` are stored before connecting so they can be identified for
  disconnection. `test_tone_requested` is also disconnected for symmetry.
  Practically safe as-is (modal dialog can't emit after exec() returns), but
  eliminates the asymmetry that could trap future signal additions.

### Tests added
- `tests/ui/test_settings_dialog.py` — BZ-01: verify `reject()` terminates a
  launched rigctld process and clears `_rigctld_proc` to None; BZ-02: verify
  `_refresh_banner_preview` passes the live callsign widget value to
  `apply_tx_banner`, not the original config value.
- `tests/ui/test_image_editor.py` — BZ-03: verify that setting X/Y spinboxes
  updates the visual crop rectangle position.
- `tests/core/test_incremental_decoder.py` — BZ-07: verify total samples passed
  to `_bp_window` across N feeds is O(N × chunk) not O(N² × chunk), with the
  bound checked against 2 × total pending size.

---

## [0.1.25] — 2026-04-15

### Fixed
- **Thread safety: decoder rebuilds now happen on the worker thread.**
  `RxWorker.set_weak_signal`, `set_incremental_decode`, and `set_sample_rate`
  are now `@Slot`-decorated; `MainWindow._apply_config` dispatches them via
  queued signals instead of direct calls, so decoder reconstruction never
  races with `feed_chunk` on the RX worker thread. (H-02)
- **Robot 36 + final slant correction no longer silently swaps color pipelines.**
  The final single-pass re-decode (opt-in setting) now skips Robot 36 and
  logs a debug note. The incremental path uses the slowrx integer-matrix
  pipeline; the batch path uses median+PIL — substituting the batch result
  would degrade color quality without warning. (H-03)
- **Settings dialog signal disconnects guarded by try/finally.** If
  `dlg.exec()` raises, the four `TxWorker → SettingsDialog` connections
  are now always severed, preventing a stale-wrapper segfault during
  Python finalization. (H-04)
- **Robot 36 progressive decode no longer flickers backward.** Per-line
  back-fill re-emissions (chroma neighbour updates) are now suppressed in
  `_feed_decoding_incremental` via a high-water-mark guard; `lines_decoded`
  in `ImageProgress` events is strictly non-decreasing. (M-03)

### Changed
- **`IncrementalDecoder` Protocol added** to `incremental_decoder.py`.
  `Decoder._incremental_dec` is now annotated as
  `IncrementalDecoder | None` — covers all six concrete backends instead
  of the stale `ScottieS1IncrementalDecoder` annotation. (H-01)
- Internal field `_exp_incremental` renamed to `_incremental_decode` in
  `decoder.py` and `workers.py`; widget `_exp_incremental_check` renamed
  to `_incremental_check` in `settings_dialog.py`. (M-02)
- About dialog updated: mode count 17 → 22; mode list now includes Martin
  M3/M4, Scottie S3/S4, PD-50, and PD-160 which were missing. (M-04)
- `RxWorker` module docstring updated to describe the incremental decode
  path as the primary path since v0.1.24. (M-05)
- User guide "Three popular SSTV modes" updated to "22 SSTV modes across
  the Robot, Martin, Scottie, PD, Wraase SC2, and Pasokon families". (L-02)
- CLI `open-sstv-decode` help text now notes that Robot 36 output may
  differ slightly from the GUI (different color pipelines). (L-03)

### Tests added
- `test_set_weak_signal_rebuilds_decoder` / `test_set_incremental_decode_rebuilds_decoder`
  — verify the Decoder is replaced with correct settings after each call.
- `test_final_slant_skips_robot36_keeps_progressive` — verify `decode_wav`
  is never called for Robot 36 when final slant correction is enabled.
- `test_robot36_incremental_roundtrip_quality` — Robot 36 line-pair round-trip
  via the incremental decoder; luma MAE < 5%, chroma MAE < 15%.
- `test_robot36_incremental_progress_is_monotonic` — `lines_decoded` never
  decreases across `ImageProgress` events for per-line Robot 36 audio.

---

## [0.1.24] — 2026-04-15

### Changed
- **Progressive per-line decoding is now the default for all modes.**
  The incremental decoder (previously opt-in via "Experimental: per-line
  incremental decode") is now enabled out of the box. Covers all 22
  supported modes: Scottie, Martin, PD, Wraase SC2, Pasokon, and Robot 36.
  The legacy batch decoder remains available — uncheck "Per-line incremental
  decode (all modes)" in Settings → Audio → Receive to revert.
- Config field `experimental_incremental_decode` renamed to
  `incremental_decode`. Existing TOML configs with the old key are
  automatically migrated (a `False` setting is preserved).
- UI label updated: "Experimental: per-line incremental decode (all modes)"
  → "Per-line incremental decode (all modes)".

### Added
- **Robot 36 slowrx-port rewrite** — new line-pair decoder using linear
  (mean) chroma sampling and linear inter-row chroma upsampling for softer,
  more accurate colour rendering vs. the old median + nearest-neighbour copy.
- **Streaming decoders for Martin, PD, Wraase SC2, and Pasokon families** —
  each mode now has a dedicated incremental subclass that decodes O(1 line
  period) per sync pulse instead of reprocessing the full buffer on every
  flush (~50× CPU reduction on long modes; Martin M1 now stays ahead of
  real-time on laptop-class hardware).

---

## [0.1.23] — 2026-04-15

### Fixed
- **PD-mode autocrop offered half the real height** — the image editor used
  `spec.height` (sync-pulse count, half the pixel count for PD modes) as
  the crop target.  PD-50 offered 320×128 instead of 320×256.  Added
  `ModeSpec.display_height` property that returns the actual pixel height;
  used in the image editor and TX mode dropdown.
- **Settings dialog segfault on app exit** — four signal connections from
  `TxWorker` to the `SettingsDialog` were never disconnected after
  `exec()` returned; PySide6's C++ destructor hit a dangling Python
  wrapper during `_Py_Finalize`.  Now disconnected immediately after
  `exec()`.
- **TX banner overwrote user content** — the banner drew directly over the
  top rows of the source image.  Now the source is resized to fit below
  the banner strip (≈9% vertical compression for "small" banner) so user
  text overlays and image detail are never lost.
- **Clear Text didn't remove manually-added editor text** — the image
  editor baked text into `_base_image`, so reverting to it kept the
  editor's overlays.  The editor now returns a separate text-free base;
  Clear Text reverts to it, removing both template and editor text.
- **Crop tool ignored user's drag position** — the crop rectangle was
  draggable but the spinbox values were never updated on drag, so
  "Apply Crop" always used the auto-fit coordinates.  `_CropRect` now
  overrides `itemChange()` to sync spinboxes on every drag.

### Added
- **X/Y pixel spin boxes** in the image editor for fine text overlay
  placement.  The Position dropdown (Top Left, Bottom Center, etc.) auto-
  fills the spin boxes; manual edits flip the dropdown to "Custom."
  Coordinates persist in the template TOML alongside the existing
  position field (backward compatible).

---

## [0.1.22] — 2026-04-15

### Changed
- **TX banner default size is "small" again** (was "medium" since v0.1.20).
  The new "small" has a fuller-looking strip than the old small because
  every preset's font size was bumped +4 pt in this release — the default
  24 px strip now uses 18 pt text (was 14 pt).  Operators who had
  `tx_banner_size: "medium"` persisted to disk keep their choice; only
  fresh installs and the fallback-on-unknown path see "small" now.
- **SIZE_TABLE font sizes bumped +4 pt across the board**: small 14 → 18 pt,
  medium 20 → 24 pt, large 26 → 30 pt.  Strip heights are unchanged
  (24 / 32 / 40 px) so the banner footprint on transmitted images does not
  grow — only the text fills more of the vertical space.  Non-background
  pixel-fraction thresholds in `test_banner.py` were raised from 15 % → 20 %
  to accommodate the larger glyphs.
- **`banner_size_params()` unknown-name fallback is now "small"** (was
  "medium"), matching the new default.

### Added
- **"Preview on image…" button** in Settings → Images → TX Banner.  Opens a
  file picker, stamps the banner onto the chosen image using the current
  colour and size selections (live — no need to save settings first), and
  shows the result in a modal dialog.  Large images (PD-290 at 800×616,
  say) are scaled down to 80 % of the available screen area with
  `Qt.SmoothTransformation` so they fit on a laptop display.  The caption
  under the image reports native dimensions and the active size preset.
  Complements the strip-only live preview above it — now the operator can
  see the banner composited against a real photo before committing to TX.

---

## [0.1.21] — 2026-04-15

### Added
- **5 new SSTV modes — Martin M3/M4, Scottie S3/S4, PD-50** (22 supported modes
  total, up from 17). All five are height- or timing-only variants of existing
  families; each required a thin one-line PySSTV subclass, a `ModeSpec` entry in
  `core/modes.py`, and a `_PIXEL_DECODERS` registration in `core/decoder.py`.
  - **Martin M3** (VIS 36) — 320×128, identical line timing to M1, ~57 s.
  - **Martin M4** (VIS 32) — 160×128, identical line timing to M2, ~29 s.
  - **Scottie S3** (VIS 52) — 320×128, identical line timing to S1, ~55 s.
  - **Scottie S4** (VIS 48) — 160×128, identical line timing to S2, ~36 s.
  - **PD-50** (VIS 93) — 320×256 decoded image, pixel time 0.286 ms (half of
    PD-90's 0.532 ms), ~50 s.
- **`tests/core/test_new_modes.py`** — 13 tests covering VIS round-trip, encoder/
  decoder dispatch, spec sanity, family timing consistency, and encode→decode
  dimension checks for all 5 modes. M4 and S4 round-trips run unconditionally;
  M3, S3, and PD-50 are marked `@pytest.mark.slow` (50–57 s of audio each).

---

## [0.1.20] — 2026-04-14

### Changed
- **TX banner layout reworked — callsign left, version right.**
  Previous layout centred "Open-SSTV v{version}" and placed the callsign
  flush-right; both texts share a single horizontal axis and could collide
  on long callsigns.  New layout: callsign flush-left with 8 px padding,
  "Open-SSTV v{version}" flush-right with 8 px padding.  Empty callsign
  shows only the right column.  If text would still overlap (extremely
  narrow modes), the version text is pushed right and clipped by the image
  boundary rather than overwriting the callsign.
- **TX banner now defaults to Medium size (32 px strip, 20 pt text).**
  Previous hardcoded size was Small (24 px / 14 pt), which was hard to read.
  Existing installs with no `tx_banner_size` key in TOML get "medium" on first
  run via the `AppConfig` dataclass default.

### Added
- **TX banner size selector** — Small / Medium / Large dropdown in
  Settings → Images → TX Banner.  Drives both strip height and font size
  proportionally: Small (24 px / 14 pt), Medium (32 px / 20 pt), Large
  (40 px / 26 pt).  Persisted as `tx_banner_size: str = "medium"`.
- **`SIZE_TABLE` and `banner_size_params()`** exported from `core/banner.py`
  so callers can look up (height, font_size) by name without hard-coding values.
- **Live preview resizes** with the chosen size so the preview label always
  matches the actual strip height.

---

## [0.1.19] — 2026-04-14

### Added
- **TX banner** — optional identification strip stamped on every transmitted image
  (not the test tone). The strip is `BANNER_HEIGHT = 24` pixels tall, spans the full
  image width, and shows "Open-SSTV v{version}" centred and the callsign flush-right.
  Implemented in `core/banner.py` (`apply_tx_banner`) using Pillow `ImageDraw`; applied
  in `TxWorker.transmit()` after any image-editor crop/overlay but before `encode()`.
  Off by default. Configure via Settings → Images → TX Banner:
  - **Enable banner** checkbox.
  - **Background colour** swatch button (default `#202020`).
  - **Text colour** swatch button (default `#FFFFFF`).
  Three new `AppConfig` fields: `tx_banner_enabled`, `tx_banner_bg_color`,
  `tx_banner_text_color`.

---

## [0.1.18] — 2026-04-14

### Fixed
- **Final one-shot re-decode no longer corrupts images by default.**
  After progressive decode completed, `RxWorker._dispatch` was running
  `decode_wav` unconditionally and replacing the progressive image with the
  result.  `decode_wav` applies `slant_corrected_line_starts`, which fits a
  plain least-squares line through *all* detected sync candidates with no
  outlier rejection.  On weak or marginal signals the false-positive candidates
  corrupt the fit, shifting every line position — the re-decoded image was
  visibly worse than the progressive one.

### Added
- **`apply_final_slant_correction` setting (Settings → Audio → Receive, default Off).**
  The final re-decode is now an explicit opt-in.  When disabled (default), the
  progressive image is used as-is and `decode_wav` is never called — no extra
  sosfiltfilt / Hilbert work, and no 5–10 s gallery-appearance delay on long
  transmissions.  Enable only for clean, strong signals from stations with a
  known clock-drift problem.

---

## [0.1.17] — 2026-04-14

### Changed
- **Decode flush interval doubled from 1 s to 2 s.**
  `RxWorker` now accumulates 2 s of audio (96 000 samples at 48 kHz) between
  progressive-decode flushes instead of 1 s.  Each flush reprocesses the entire
  growing buffer (O(buffer²) cost), so halving the flush count cuts CPU load by
  roughly half during a long RX — from ~36 flushes on a Scottie S1 to ~18.
  The constant `_DECODE_FLUSH_INTERVAL_S = 2.0` in `ui/workers.py` now controls
  the interval; `_RX_FLUSH_SAMPLES_DEFAULT` is derived from it so a single edit
  keeps everything in sync.

---

## [0.1.16] — 2026-04-14

### Fixed
- **Progressive decode "breaks" after a few seconds (D-3 slant instability).**
  The live draw path in `_partial_decode` / `_partial_decode_robot36` was calling
  `slant_corrected_line_starts()`, which fits a least-squares line through *all
  currently-detected sync positions* and reprojects every line on every flush.
  As more candidates arrive the fit changes, shifting the projected offsets for
  already-decoded rows — the top of the image appeared clean, then "broke" a
  few seconds in when the slant parameters updated.

  Fix: replace `slant_corrected_line_starts` with `walk_sync_grid` in both
  progressive decode functions.  `walk_sync_grid` anchors at the first valid
  candidate pair and walks forward; adding more candidates extends the walk
  but leaves already-confirmed positions unchanged.  Slant correction is still
  applied by the final one-shot re-decode in `RxWorker._dispatch` (via
  `decode_wav → slant_corrected_line_starts`), so the saved image benefits
  from it without the mid-decode instability.

  Two new regression tests in `tests/core/test_decoder.py` lock in the
  position-stability contract and document why slant correction was removed
  from the progressive path.

---

## [0.1.15] — 2026-04-14

### Fixed
- **Stop Capture and Clear buttons unresponsive during long decodes** (bug R-2).
  `RxWorker._flush()` calls `Decoder.feed()` synchronously on the worker thread,
  blocking for 3–8 seconds on a full Scottie S1 or Martin M1 buffer.  Both the
  "Clear" and "Stop Capture" actions are queued `@Slot` calls on the same thread,
  so they wait for the decode to finish before executing — making the UI appear
  frozen.

  Fix: added a `threading.Event` cancel mechanism mirroring `TxWorker.request_stop()`.
  - `RxWorker.request_cancel()` — thread-safe method (sets a `threading.Event`),
    callable directly from the GUI thread without going through Qt's queued
    connection.
  - `Decoder.set_cancel_event(event)` — wires the event into the decode pipeline.
    Checked at five points: after bandpass filter, after Hilbert/IF demodulation,
    after sync candidate detection, and at the start of every row in each of the
    seven per-mode pixel decoders (Robot 36, Robot 36 line-pair, Martin, Scottie,
    Wraase, Pasokon, PD).
  - `MainWindow._on_rx_clear()` and the Stop Capture path now call
    `request_cancel()` before emitting the queued reset signal, so the running
    decode exits at the next checkpoint rather than completing in full.
  - `RxWorker.reset()` clears the event after resetting state, re-arming the
    decoder for the next transmission.

---

## [0.1.14] — 2026-04-14

### Added
- **CW station ID** (`core/cw.py`) — every SSTV transmission now appends a
  Morse code callsign after the image audio (500 ms gap + CW tail), keyed under
  the same PTT with no unkey between. Satisfies the Part 97 requirement that every
  transmission be identified by the station's callsign. Test Tone is exempt (it's
  a calibration aid, not a communication). Stop button and the 5-minute watchdog
  apply to the combined SSTV + CW duration.
  - New module `core/cw.py`: ITU-R M.1677-1 Morse table (A–Z, 0–9, `/`, `-`),
    windowed-sine generator with 5 ms attack/decay to suppress key clicks,
    standard PARIS timing (dit = 1.2 / WPM seconds).
  - New config fields: `cw_id_enabled: bool = True`, `cw_id_wpm: int = 20`,
    `cw_id_tone_hz: int = 800`.  Callsign is read from the existing `callsign`
    field; if empty, CW ID is skipped with a warning and TX is not blocked.
  - Settings → Radio → CW Station ID section: enabled checkbox, WPM spinbox
    (15–30), tone spinbox (400–1200 Hz, step 50), live callsign indicator.

---

## [0.1.13] — 2026-04-14

### Added
- **Package renamed `sstv_app` → `open_sstv`** — all source files, imports, entry
  points (`open-sstv`, `open-sstv-encode`, `open-sstv-decode`), config dirs
  (`~/.config/open_sstv/`, `~/Library/Application Support/open_sstv/`), and docs
  updated. PyPI package name: `open-sstv` (unchanged from v0.1.12).
- **TX output gain overdrive toggle** — default slider ceiling is now **0–100%**
  (matches a typical USB-audio rig at ~10–15% for correct ALC). An "Enable
  overdrive" checkbox below the slider expands the ceiling to 200% for setups that
  need more digital drive. Tooltip: "Most setups don't need above 100%. Enable only
  if ALC won't move at max gain." Config field: `tx_output_overdrive: bool = False`.
  Migration: configs with `audio_output_gain > 100%` auto-enable overdrive on first
  load so calibrated values are preserved, not silently clamped.
- **Weak-signal RX mode** — new "Weak-signal mode" checkbox in Settings → Audio →
  Receive. When enabled, relaxes two VIS detection thresholds: leader presence
  fraction (0.40 → 0.25) and minimum start-bit duration (20 ms → 15 ms). Use when
  a signal is audible in the static but VIS isn't triggering. Config field:
  `rx_weak_signal_mode: bool = False`. False positives remain graceful (D-1).

### Changed
- **Live TX gain slider** — TX Output Gain slider changes in Settings are now
  immediately pushed to `TxWorker` on every tick (no disk write), so adjustments
  take effect during a running Test Tone without closing the dialog. Cancelling the
  dialog reverts the gain to the previously saved value.

### Docs
- **User guide §12.1** — Output Gain updated: default ceiling 0–100%, overdrive
  toggle documented. Weak-signal mode toggle documented under Receive options.

---

## [0.1.12] — 2026-04-14

### Added
- **Test Tone in Settings dialog** — Audio tab now has a "Test Tone" button next to the
  TX output gain slider. Same 700 Hz + 1900 Hz / 5 s calibration signal as the Radio
  panel button. Enabled when a rig is connected; shows "Testing…" while active; gain slider
  remains live during the tone. Suggested workflow in §12.1 of the user guide.

### Changed
- **TX Output Gain slider reverted to 0–200%** (was 0–500% in v0.1.11). Any stored value
  above 200% is silently clamped to 200% via `AppConfig.__post_init__` on first load.

### Fixed
- **S-5 — S-meter display formula** — `RadioPanel.update_rig_status` used
  `(dBm + 73) // 6` to map dBm to S-units, which maps S9 (−73 dBm) to 0 and any signal
  weaker than S9+60 to 0 or negative. The bar appeared empty for all real-world signals.
  Root cause: the formula was hidden by the C-4 echo-byte bug (raw was always 5378 → +2534
  dBm → always showed S9 bar). After C-4 fixed the bytes, the correct −73 dBm value flowed
  through but the display ate it. Fixed: `(dBm + 127) // 6` (S0 = −127 dBm, 6 dB/unit).
- Added INFO-level diagnostic logging in `IcomCIVRig.get_strength()` (runs at 1 Hz while
  connected) to confirm BCD byte layout in field. Visible with default console log level.

---

## [0.1.11] — 2026-04-14

### Changed
- **Test Tone peak raised to −1 dBFS** — two-tone calibration signal now drives the output
  harder (was −6 dBFS), making ALC movement visible without relying on downstream gain.
- **TX Output Gain slider extended to 500%** — previous ceiling was 200%; IC-7300 and similar
  radios may need higher digital gain when the radio-side USB MOD Level is conservatively set.

### Fixed
- **ALC advice message** — status bar message after Test Tone now lists specific diagnostic
  steps: radio's USB MOD Level menu, app TX gain slider, and computer output volume.
- **D-1 — VIS false-positive no longer alarming** — when `detect_vis` decodes an unknown VIS
  code (most commonly 0x00: all-zeros, even parity, which noise can produce), the decoder
  now silently drops samples past the false header and stays in IDLE rather than emitting a
  "Unsupported VIS code" error. VIS detection is probabilistic; false positives on noise or
  RF loopback are expected and should not alarm the user. Real transmissions decode normally.
- **C-4b — S-meter BCD parsing** — `IcomCIVRig.get_strength()` was treating the two S-meter
  payload bytes as a 16-bit binary integer (`(resp[2]<<8)|resp[3]`). The IC-7300 encodes the
  reading as BCD: S9 is sent as bytes `[0x01, 0x20]` (decimal 120), not `[0x00, 0x78]`
  (binary 120). Added `_bcd_byte_to_int` helper and updated the parse; S0/S9/S9+60 now
  decode correctly.

### Docs
- **User guide §12.1** — Output Gain entry updated to show 0–500% range; added informational
  IC-7300 USB MOD Level note (factory default ~50% is fine for most setups).
- **User guide §17** — New "ALC doesn't move during Test Tone or transmission" entry covering
  Output Gain, macOS per-device system volume (System Settings → Sound → Output), and the
  IC-7300 USB MOD Level reference.

---

## [0.1.10] — 2026-04-14

### Added
- **Test Tone** — new "Test Tone" button in the Radio panel (enabled when a real rig is
  connected and idle). Transmits a 700 Hz + 1900 Hz two-tone signal for 5 s at −6 dBFS
  peak via the configured output device and rig PTT. Respects TX watchdog, output gain,
  and the Stop button. Status bar shows a per-second countdown while keyed; on completion
  shows "Adjust mic/RF gain so ALC just barely lights on peaks."

### Fixed
- **R-1** — RX sample counter did not reset when capture was stopped and restarted;
  the "Xs buffered" label kept climbing past the IDLE timeout indefinitely.
  `_on_capture_requested` now emits `_request_rx_reset` before restarting the audio
  stream so each session starts from zero.
- **R-2** — Self-decode through RF/audio loopback: `RxWorker.feed_chunk` now discards
  audio while TX is active (`_tx_active` gate set by `transmission_started`/`complete`).
  After TX ends a 50 ms gate-off delay lets trailing audio drain before the decoder
  resumes; the buffer and decoder state are reset at that point.
- **C-1** — `IcomCIVRig.get_freq()` passed the full CI-V response payload (including
  the command-echo byte 0x03) to `_bcd_to_freq`, corrupting the frequency result.
  Fixed: strip echo byte (`resp[1:]`), update length check to `>= 6`.
- **C-2** — `IcomCIVRig.get_mode()` read `resp[0]` (command echo 0x04 = RTTY in the
  mode map) as the mode byte, so the mode display always showed "RTTY" regardless of
  the radio's actual mode. Fixed: use `resp[1]` for mode, `resp[2]` for passband.
- **C-3** — `IcomCIVRig.get_ptt()` read `resp[0]` (command echo 0x1C ≠ 0x00) as the
  PTT state, so the rig always appeared keyed. Fixed: use `resp[2]`.
- **C-4** — `IcomCIVRig.get_strength()` built `raw` from `resp[0]` and `resp[1]`
  (command echo + subcmd = constant 0x1502 = 5378), so the S-meter never changed.
  Fixed: use `resp[2]` and `resp[3]`.

---

## [0.1.9] — 2026-04-14

### Fixed
- Emit "Closing…" status bar message before TX teardown wait so the window does not appear frozen (A-08)
- Cache serial port list for 5 s to avoid repeated USB enumeration on every Settings open (A-09)
- `IcomCIVRig._freq_to_bcd()` now raises `ValueError` on negative input instead of silently producing a corrupt BCD sequence (A-10)
- Add module-level logger to `workers.py`; replace silent `except: pass` on re-decode fallback with `log.debug(exc_info=True)` for debugging visibility (A-11)
- Wrap `output.parent.mkdir()` in `cli/decode.py` inside the existing `try/except` block so a bad output path produces a clean error message and exit code 1 instead of a raw traceback (A-12)

---

## [0.1.8] — 2026-04-14

### Fixed
- Replace bare `assert self._sock is not None` in `rigctld.py` with explicit `RigConnectionError` raises — bare asserts are no-ops under `python -O` (A-04)
- Add public `TxWorker.wait_for_stop(timeout)` method; `closeEvent` now calls it instead of accessing `_stop_event` directly across object boundaries (A-05)
- Add module-level completeness assertion on `_PYSSTV_CLASSES` vs `set(Mode)` in `encoder.py` — missing encoder caught at import time, not at first TX (A-06)
- Add matching completeness assertion on `_PIXEL_DECODERS` in `decoder.py` — covers all 17 modes including Robot 36 (A-07)

---

## [0.1.7] — 2026-04-14

### Fixed
- Wrap both `serial.tools.list_ports.comports()` calls in `settings_dialog.py` with try/except; fall back to empty list and log warning so Settings dialog opens cleanly on serial enumeration failure (A-01)
- `save_config()` now catches `OSError`, logs it, and re-raises; `_open_settings` surfaces the failure in the status bar and still applies the in-memory config for the session (A-02)
- Move `tempfile.mkdtemp()` from module scope into `ImageGalleryWidget.__init__()`; fall back to in-memory PIL image storage if temp directory creation fails (A-03)

---

## [0.1.6] — 2026-04-14

### Changed
- Consolidate `_pil_to_pixmap` into `ui/utils.py`; remove duplicate copies from `image_gallery.py`, `image_editor.py`, and the delegation shim in `tx_panel.py` (S-16)
- Replace `serial.tools.list_ports.comports()` calls in `settings_dialog.py` for cross-platform serial port detection on Windows, Linux, and macOS (F-10)

### Fixed
- Fix `dict[Mode, callable]` type annotation in `decoder.py` — `callable` is the built-in function, not a type; replace with `Callable[..., Image.Image | None]` (S-17)
- Add friendly `ImportError` handler in `app.py:main()` that prints install instructions instead of a raw traceback when `PySide6` or other dependencies are missing (S-18)
- Add `TxWorker._stop_event.wait(timeout=1.0)` in `closeEvent` after `request_stop()` to make TX shutdown ordering explicit (S-19)
- Wire `default_tx_mode` config field to `TxPanel` mode selector — previously always started at index 0 regardless of saved preference (F-11)

---

## [0.1.5] — 2026-04-14

### Added
- Waterfall display scope document (`docs/waterfall_scope.md`) planning the v1.1 waterfall feature

### Fixed
- Capture `rig = self._rig` snapshot at top of `TxWorker.transmit()` so a mid-TX `set_rig()` call cannot swap PTT backends between key-up and key-down (S-10)
- Replace bare `assert` with explicit `RigConnectionError` raises in `IcomRig`, `KenwoodRig`, and `YaesuRig` serial backends (S-11)
- Catch `sd.PortAudioError` specifically before broad `Exception` in `TxWorker.transmit()`; emit "Audio device disconnected during transmission." (S-12)
- Route `RxPanel.clear_requested` → `RxWorker.reset()` through a `_request_rx_reset` signal so reset runs on the decode thread, not the GUI thread (S-13)
- Add `Decoder.consume_last_buffer()` to free raw audio after the re-decode pass; persist gallery images to a per-instance temp directory to release PIL objects from memory immediately after thumbnail render (S-14)
- Filter unsolicited messages in `KenwoodRig._read_response()` and `YaesuRig._read_response()` by looping until a response with the expected command prefix is found (S-15)
- Add `TxWorker.set_ptt_delay()` and call it from `_open_settings` on every save so the configured PTT delay takes effect without restart (F-09)

---

## [0.1.4] — 2026-04-14

### Fixed
- Run rigctld connection in a deferred callback (`QTimer.singleShot`) so the GUI thread does not block during TCP connect (S-04)
- Wrap `load_config()` body in try/except returning `AppConfig()` defaults on any parse error; same for `load_templates()` (S-05, S-06)
- Move rig polling off the GUI thread onto a dedicated `_RigPollWorker` QThread; replace direct `_poll_rig` call with queued signal/slot (S-07)
- Fix `_all_devices()` fallback index in `audio/devices.py` — was using `len(out)` (wrong) instead of the PortAudio enumeration index (S-08)
- Rename CLI entry points to `open-sstv`, `open-sstv-decode`, `open-sstv-encode` (F-02)

---

## [0.1.3] — 2026-04-14

### Added
- 14 additional SSTV modes: Martin M2, Scottie S2, Scottie DX, PD-90/120/160/180/240/290, Wraase SC2-120/180, Pasokon P3/P5/P7 (encoder + decoder + VIS table + mode selector)
- TX watchdog timer (`threading.Timer`, 300 s hard limit) — forces PTT off and aborts playback if encode + playback exceed the limit (S-01)
- Rig swap lockout — `RadioPanel.set_tx_active(True)` disables connect/disconnect controls for the duration of a transmission (S-02)

### Fixed
- Bound decoder IDLE buffer to a 3-second rolling window to prevent unbounded memory growth during long listening sessions (S-03)
- Recover uncommitted work from `amazing-raman` worktree branch and merge to main

---

## [0.1.2] — 2026-04-07

### Added
- QSO template system for rapid callsign/locator image exchange

---

## [0.1.1] — 2026-04-07

### Fixed
- Fix Robot 36 TX (line-pair format for compatibility with MMSSTV, SimpleSSTV, slowrx)

### Added
- Direct serial rig control (Icom CI-V, Kenwood, Yaesu)
- Image editor dialog (crop, resize, text overlay)

---

## [0.1.0] — 2026-04-07

### Added
- Initial alpha release: TX and RX end-to-end for Robot 36, Martin M1, and Scottie S1
- Settings dialog, auto-save decoded images, rig polling via rigctld
- CLI tools: `sstv-app-encode`, `sstv-app-decode`
- Weak-signal robustness (bandpass filter, adaptive sync, slant correction)

---

[Unreleased]: https://github.com/bucknova/Open-SSTV/compare/v0.2.7...HEAD
[0.2.7]: https://github.com/bucknova/Open-SSTV/compare/v0.2.6...v0.2.7
[0.1.9]: https://github.com/bucknova/Open-SSTV/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/bucknova/Open-SSTV/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/bucknova/Open-SSTV/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/bucknova/Open-SSTV/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/bucknova/Open-SSTV/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/bucknova/Open-SSTV/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/bucknova/Open-SSTV/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/bucknova/Open-SSTV/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/bucknova/Open-SSTV/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bucknova/Open-SSTV/releases/tag/v0.1.0
