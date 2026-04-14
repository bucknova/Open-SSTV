# Changelog

All notable changes to Open-SSTV are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
