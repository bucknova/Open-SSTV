# Changelog

All notable changes to Open-SSTV are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

[Unreleased]: https://github.com/bucknova/Open-SSTV/compare/v0.1.9...HEAD
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
