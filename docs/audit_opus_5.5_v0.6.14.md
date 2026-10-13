# Open-SSTV v0.6.14 — full code & security review (Opus 5.5)

Date: 2026-10-13 · Commit: `b02231d` · Scope: all of `src/open_sstv/`, CI workflows, packaging.

Method: six parallel read-only reviewers (2 security, 4 correctness, split by
subsystem), with the top findings re-verified by hand. "Confirmed" = traced end
to end and/or reproduced with a snippet; "Plausible" = traced but not reproduced.

Test baseline: 1935 passed, 6 skipped, 1 failed (`test_qr_rendered_when_enabled_with_token`
— local venv missing `segno`; environmental, not a code bug).

---

## Fix-first list

| # | Area | Finding | Sev | Conf |
|---|------|---------|-----|------|
| 1 | TX safety | `set_ptt(True)` outside the try/finally: key times out but rig keyed → never unkeyed | High | Confirmed |
| 2 | TX safety | rigctld launched from Settings has `stderr=PIPE` never drained → rigctld blocks, CAT/PTT time out | High | Confirmed |
| 3 | TX safety | `_on_tx_error` re-enables Disconnect while worker is still unkeying; disconnect kills rigctld mid-retry | Med | Confirmed |
| 4 | TX safety | `emergency_unkey` targets `ManualRig` after involuntary disconnect mid-TX | Med | Confirmed |
| 5 | TX safety | Playback watchdog only `request_stop()`s, never unkeys | Med | Plausible |
| 6 | Startup | One wrong-typed string field in config.toml (`log_level = 5`) crashes every launch | High | Confirmed |
| 7 | Startup | Double `load_config()` hides the "corrupt settings" warning and shows Welcome → defaults overwrite user config | High | Confirmed |
| 8 | Rig UI | Second Connect after timeout is wrecked by the first thread's `finished` handler (leaked open rig) | High | Confirmed |
| 9 | Decoder | Missed sync on line 0/1 loses the whole image (live + file) | High | Confirmed |
| 10 | Decoder | Live Robot 36 picks the line-pair backend if one of first 3 syncs is missed | High | Confirmed |
| 11 | Decoder | Slant fit polluted by candidates after the image → file decodes of real recordings skewed | High | Confirmed |
| 12 | Remote | Token logged in plaintext at DEBUG via `log_message` request line | Med | Confirmed |
| 13 | Remote | Weak operator-chosen tokens accepted, no rate limiting; UI nudges toward fixed token | Med | Confirmed |
| 14 | CI | `contents: write` on the build matrix + unpinned `pip install` + persisted checkout creds | Med | Confirmed |

---

## 1. Security

**Overall: no Critical/High.** Without the token, a malicious website, DNS-rebinding
page, or LAN host cannot key the rig. Path traversal, XSS, SQL, subprocess and
deserialization are all handled well. Risk concentrates in token handling when
"LAN access" + "remote TX" are both enabled.

### Medium
- **S-M1 Token in DEBUG log** — `remote/server.py:225`. `log_message` logs `requestline`, which contains `?token=` (page builds every URL with it, `page.py:395`). Shared logs/diagnostics leak the station token. Fix: redact `token=` in `log_message`; move fetches to `Authorization: Bearer`.
- **S-M2 Weak tokens / no brute-force limit** — `server.py:228-243`, `settings_dialog.py:594-596,707-708`, `schema.py:277`. QR only shows for a fixed token, pushing users to set one; no min entropy, no backoff; LAN mode binds `0.0.0.0`. Fix: always generate (offer Regenerate), show QR for generated token, per-IP 401 backoff.
- **S-M3 No TLS** — token in cleartext on every request; static, no revocation. Short-term: warn in Settings when LAN + remote TX are on. Long-term: architecture.md §7 TLS/TOFU + per-device pairing.
- **S-M4 Slowloris** — `server.py:213`, `timeout=15` is per-recv, `ThreadingHTTPServer` uncapped. Unauthenticated LAN host can hold unlimited threads/FDs (PoC confirmed). FD exhaustion can hit audio/serial opens in-process. Fix: per-request header deadline + connection semaphore (~32).
- **S-M5 Template TOML → unbounded `Image.new`** — `templates/toml_io.py:102-112,279-311`; `renderer.py:400-433`. Shadow `offset_x=4000` → 1.9 GB RSS on a 320×256 frame (confirmed); quadratic. Gallery renders all templates at launch → can recur every start. Fix: extend `_LAYER_NUMERIC_BOUNDS` to stroke/shadow/font_size_pct/line_height_mult/angle_deg/cell_size_pct, reject NaN/inf, renderer guard on intermediate size.
- **S-M6 CI supply chain** — `.github/workflows/build.yml:82-83,151,198-200`. Workflow-wide `contents: write`, default `persist-credentials`, unpinned `pip install pyinstaller …`. Compromised dependency could push/retag/tamper releases or backdoor binaries. Fix: top-level `contents: read`, write only on `release` job, `persist-credentials: false`, hash-pinned build requirements, SHA-pin actions.

### Low
- **S-L1 No Host-header validation** (`server.py:342-351`); Origin compared to attacker-controlled Host. Harmless today (all `/api` needs token) but CSRF defense rests solely on the token. Allowlist Host; reject POST with neither Origin nor Bearer.
- **S-L2 LAN binds `0.0.0.0`** (`settings_dialog.py:2293`) vs design's "bind LAN interface". Bind detected LAN IP or offer picker.
- **S-L3 Compose unthrottled** (`server.py:460`, `compose.py:121-141`): parallel 18 MB / 32 MP decodes can OOM mid-TX. Semaphore 1–2; consider gating on `tx_enabled`.
- **S-L4 Token in URL/history, no `Referrer-Policy`** — add `no-referrer`; `history.replaceState` token out.
- **S-L5 One client can take all 16 SSE slots** — per-IP cap.
- **S-L6 macOS entitlements** — `allow-dyld-environment-variables` + `disable-library-validation` + `audio-input` = DYLD-injection TCC mic bypass pattern. Drop `allow-dyld-environment-variables` and `allow-jit`.
- **S-L7 Update link unvalidated on network path** — `update_checker.py:201-207` skips the `_RELEASES_PREFIX` check used on the cache path; `main_window.py:1859` puts url/version unescaped into a link-opening rich-text QLabel. Validate + `html.escape`.
- **S-L8 ADIF `FREQ=inf`/`1e300`** — `adif.py:121-126` misses `OverflowError`; aborts whole import. Catch, range-check, wrap per record.
- **S-L9 Flex reader buffer unbounded** (`flex.py:339-354`); **TCI frames unbounded** (`tci.py:381`). Cap at ~64 KB.

### Info
- Rich-text injection of operator's own token in Qt label (`main_window.py:1334`) — `html.escape`.
- `Server: BaseHTTP/0.6 Python/3.x` banner.
- No rate limit on back-to-back remote transmissions (design lists it as TODO).
- Lease client IDs via `Math.random` — use `crypto.randomUUID()`.
- Pillow bomb cap effectively 64 MP (only errors at 2× `MAX_IMAGE_PIXELS`).
- Windows `Popen(["rigctld"])` searches CWD first — resolve with `shutil.which`.
- Rig `set_mode` strings not newline-sanitized (safe today: static band plan only).
- Releases ship no checksums/attestations; Windows unsigned, macOS ad-hoc.

### Verified solid
Remote off + loopback by default; remote TX separately gated; 128-bit token, constant-time compare; CSRF Origin check, no OPTIONS handler; opaque IDs + `resolve(strict)`/`is_relative_to` path fencing; `esc()` on every server string, strict CSP, `X-Frame-Options: DENY`, `nosniff`; body caps + auth-before-read; lease + per-transmit confirm token, gates re-checked at keying edge, independent dead-man thread; Qt thread crossing via QueuedConnection. All SQL parameterized; no pickle/eval/yaml; subprocess list-form only with arg validation; config/template files 0600 via mkstemp; token redacted from diagnostics zip; `test.yml` uses `pull_request` with read perms; downloaded build tools sha256-verified. Over-the-air data is pixels + VIS only — never reaches paths/SQL/HTML/shell.

---

## 2. TX safety / audio / radio

Mapped to the owed v0.6.14 hardware tests.

| Owed test | Findings |
|---|---|
| Unkey on quit/SIGTERM | T-M2, T-M3, T-L1, T-L2 |
| USB audio replug | T-M1, T-M2, T-M5, T-M6, T-L4 |
| Rig control | T-H1, T-M1, T-M3, T-M4, T-M7, T-L3, T-L5, T-L9, UI-H1, UI-H2 |

### High
- **T-H1 Key-failure never unkeys** — `ui/workers.py:1215-1222`. `rig.set_ptt(True)` raises after the rig actually keyed (rigctld `T 1` > 2 s, missed CI-V OK frame, Flex reply timeout) → `return None`, no PTT-off ever sent; non-`RigError` (`OSError`) escapes entirely. Fake-rig repro: calls `[True]` only. Fix: `except Exception: emit error; self._unkey_with_retry(rig); return None`.

### Medium
- **T-M1 GUI treats TX as finished on `error` while worker still unkeying** — `main_window.py:2616-2627`, `_on_rig_disconnect` `:3865`. Disconnect clickable during health-monitor stop + 3 unkey retries; `_on_rig_disconnect` closes rig and kills rigctld immediately → retries 2–3 fail. Fix: unlock only when `wait_for_idle(0)`; defer teardown like `_on_radio_disconnected`.
- **T-M2 Watchdog never unkeys** — `workers.py:1467-1505`. Call `emergency_unkey()` before `request_stop()` (as the remote dead-man does).
- **T-M3 `emergency_unkey` → ManualRig after involuntary disconnect** — `main_window.py:3900` + `workers.py:679`. Record `_keyed_rig` in `_run_tx` and target it.
- **T-M4 One health-ping failure aborts TX + disconnects** — `workers.py:323-337`. Clean `RPRT -5/-9` or one missed CI-V read during TX counts. Require 2–3 consecutive failures; don't treat `rprt`-bearing `RigCommandError` as link-dead.
- **T-M5 Stale TX output device index after USB replug** — `main_window.py:2313`, `devices.py:229`. Re-resolve output device (and PipeWire sink) by name at each TX start — otherwise may key rig and play to speakers.
- **T-M6 No playback stall detection** — `output_stream.py:433`. Abort if `position` hasn't advanced for ~2 s (else wait up to watchdog, ~8 min on P7).
- **T-M7 TCI `vfo:` / `rx_smeter` ignore TRX index** — `radio/tci.py:675,710`. RX2 overwrites freq/S-meter; false "tune failed", wrong logbook freq. Confirmed.

### Low
- T-L1 Unkey fallback in `closeEvent` can run ~19 s late if worker is stuck; call `emergency_unkey` as soon as `wait_for_idle(0.5)` fails.
- T-L2 Stop arriving before the queued transmit slot runs is lost (`_stop_event` cleared on entry, `workers.py:749,942`). Use a generation counter.
- T-L3 Disconnect enabled during the `set_ptt(True)` window.
- T-L4 Output device lookup name-only, ignores host API (WASAPI→MME on Windows); missing device → system default output while keyed.
- T-L5 Flex `open()` leaks socket/reader on partial failure; later `open()` thinks it's connected.
- T-L6 Icom S-meter logs at INFO twice a second (`serial_rig.py:377-380`) → DEBUG.
- T-L7 Logging + allocation inside the real-time audio callback (`output_stream.py:358`).
- T-L8 TCI reconnect during unkey retry drops RX `audio_start` subscription.
- T-L9 rigctld keys with `T 1` (mic) vs Kenwood CAT `TX1` (data) — consider `T 3` or a setting.
- Nit: band plan 7.171 LSB and 3.733/3.740 outside US General phone; 3.845 missing. Linux DTR/RTS blip on tty open (document).

---

## 3. UI / main window / workers

### High
- **UI-H1 Settings-launched rigctld stderr never drained** — `settings_dialog.py:1838-1843`; adopted at `main_window.py:1990-1997`; `_drain_subprocess_stderr` only called at `:3493`. ~64 KB of hamlib errors → rigctld blocks → PTT/CAT timeouts, possibly mid-TX. Recreates v0.4.0 audit-high #2. Fix: one shared launch helper.
- **UI-H2 Connect race** — `main_window.py:3315-3335`. Timed-out thread 1's `finished` clears attempt 2's worker/relay/thread/timer → stuck "Connecting…", leaked open port/socket, worker GC'd while running. Repro in scratchpad. Fix: bind cleanup to the specific thread (`partial(..., thread)`), or refuse Connect until old thread exits.

### Medium
- UI-M1 One failed poll flips panel to "Connect Rig / Connection lost" while still connected; clicking it disconnects (`main_window.py:1181`, `radio_panel.py:208`).
- UI-M2 Settings OK always rebuilds the RX decoder → destroys in-progress decode; watchdog then emits a bogus "complete" partial (autosave + logbook prompt). Sample-rate change desyncs decoder vs. stream. Emit only on change; reset `_decoding`.
- UI-M3 Settings OK with unplugged USB codec silently overwrites saved device names with System default. Add "<name> (not connected)" entry.
- UI-M4 Deleting selected template leaves composite in TX preview but TX sends bare photo (`tx_panel.py:690-696`). Template render failure silently sends raw photo.
- UI-M5 Empty/corrupt audio file permanently disables Decode Audio for the session (`offline_workers.py:96-108`: `EOFError` uncaught, `finished` never emitted).
- UI-M6 Swap/quit waits a full 2 s and logs false "wedged worker" warnings when capture is idle (`stop()` emits nothing if not running).
- UI-M7 Remote TX that fails before keying leaves the control plane stuck TRANSMITTING (`_remote_tx_ended` not connected to `error`, `main_window.py:1061`).

### Low
- RX-audio save failures pop a modal per decode (`main_window.py:3081`); soundfile `RuntimeError` escapes.
- Manual save `ValueError` on unknown extension escapes; save not atomic (`:3147`).
- `int(gain*100)` ratchets saved gain down each OK (`settings_dialog.py:779,791`) — use `round()`.
- Failed TX before `transmission_started` still resets RX decode (`_schedule_rx_resume`).
- Rig polls can pile up (1 Hz queued, slow rig) and run during TX (`:1168`) — re-arm single-shot.
- `_on_rig_disconnect` catches only `RigError` on close (`OSError`/`termios.error` leave half-torn state).
- Double-click Connect with auto-rigctld starts two connects (`:3503`).
- Waterfall sample rate never updated after Settings change (`:3812`).
- `_pending_rx_audio` single slot can pair live audio with an offline-decoded image.
- Nits: Settings "Test" buttons block GUI thread 2–8 s; HiDPI blurry previews; int16 silence heuristic; settings `_stop_rigctld` leaves a zombie.

---

## 4. Config / templates / logbook / gallery

### High
- **C-H1 Wrong-typed string field crashes startup** — `config/schema.py:459,473,487,617,627` (`(x or "").strip()`), `store.py:172` doesn't catch `AttributeError`; `main_window.py:625` reloads unguarded. Verified: `AppConfig(log_level=5)` → `AttributeError`. `callsign = 123` crashes later in `.upper()`. Fix: coerce per field to annotated type; reset only the bad field.
- **C-H2 Corrupt-config warning never shown** — `app.py:205` loads once (moves file to `.corrupt`), `main_window.py:625` loads again → no file, `first_launch_seen=False` → Welcome dialog → save overwrites with defaults. Fix: load once in `main()` and pass in.

### Medium
- C-M1 Template saved in editor isn't what's transmitted until the card is re-clicked (`template_gallery.py:542-561` doesn't re-emit `template_selected`).
- C-M2 Image editor OK without Apply Crop squashes; text/shapes drawn in target coords on the working image → wrong place/size (`image_editor.py:778,797`). Confirmed headless.
- C-M3 TX pre-crops photo (`tx_panel.py:679`) but editor/gallery/remote don't → preview ≠ TX for `contain`/sized photo layers. Build `TXContext` in one helper.
- C-M4 `%f`/`{freq}` and `%b`/`{band}` always blank — no `TXContext` passes `frequency_hz`.
- C-M5 Save / Save As / New / Rename silently overwrite other templates by slug collision (`manager.py:226-233`, `tx_panel.py:497-525`).
- C-M6 Template Editor discards unsaved edits on close/Esc (no dirty guard).
- C-M7 Gallery Delete writes back a stale QSO snapshot, reverting Logbook edits (`gallery_dialog.py:570`).
- C-M8 ADIF export: MODE hard-coded SSTV with SUBMODE=anything (FT8 → `MODE=SSTV SUBMODE=FT8`); BAND dropped on import (`adif.py:237-239,409`). Breaks LoTW/Club Log.
- C-M9 Logbook "New…" can't set date/time/mode/freq (paper-log back-entry impossible); failed save loses edits.

### Low
- Literal `\n` in template text doesn't break lines though the cheat sheet says it does.
- `%q`/`{qso_serial}` always 0.
- Tokens expanded twice — a note/name of `{callsign}` expands (incl. remote-supplied text).
- Orbitron lacks Ø → W0AEZ with slashed zero renders a tofu box on air.
- Filename sanitizer: Windows reserved names, control chars, 200-*char* cap can exceed 255-byte NAME_MAX.
- Stale-tmp sweeps are dead (look for `name.toml.tmp`, fsutil writes `.name.toml.XXXX.tmp`).
- `scan_dirs` doesn't dedupe by resolved path; gallery date filter local vs UTC labels; thumbnail prune stat race.
- UDP log: no SO_BROADCAST, AF_INET only.
- `_hz_to_band` maps out-of-band freqs to band below; two band tables disagree.
- One bad numeric field resets the entire config.
- ADIF non-ASCII in ADI String fields (interop varies).

---

## 5. Core DSP / decoder

### High
- **D-H1 Missed sync on line 0/1 loses the image** — `core/sync.py:348-357`, `core/slant.py:118-125`. Anchor requires next gap ≈ 1 period; one missing sync jumps anchor 2 rows → only height−2 slots → never completes (live stalls until watchdog; `decode_wav` → `None`). Robot 36 also swaps Cr/Cb. Fix: accept k·P gaps and extrapolate back from `vis_end`.
- **D-H2 Live Robot 36 format misdetection** — `incremental_decoder.py:1368,1511-1531`. Only 2 gaps; one missed sync → median 225 ms → line-pair backend → garbage (MAE 32/255) or never completes. Vote per gap or require ≥5 candidates; default per-line. Consider dropping the 290.5 ms layout.
- **D-H3 Slant fit corrupted by trailing candidates** — `slant.py:158-177`. All candidates after anchor (CW ID, noise, next QSO) go into an unweighted LSQ fit. 10–60 s trailing noise → 32–35/255 MAE vs. clean. Hits CLI and GUI "Decode audio file". Fix: drop idx ≥ `max_lines`; one MAD outlier-rejection refit.

### Medium
- D-M1 `Decoder.feed` re-concatenates whole buffer every 0.1 s (O(N²); ~31 ms + 100 MB alloc per feed late in PD-290/P7) — `decoder.py:1274,1514-1519,1208`.
- D-M2 WAV loader rejects 24-bit PCM and 32-bit float (common from SDR software) — `audio/file_io.py:82-111`.
- D-M3 Per-pixel Python loop with `np.median` (~1M calls on PD-290/P7) — vectorize via strided view.

### Low
- CLI `decode` crashes with traceback on empty `.wav` (`EOFError`) or missing soundfile (`ImportError`).
- VIS leader check skipped when start-bit run is within 5 ms of buffer start (`vis.py:192-200`) → more false VIS after unknown-VIS trim.
- Scottie S1/S2/DX use PySSTV's off-spec scan split (`modes.py:152`): 1.1% short pixel dwell → ~3.5 px dark bar in MMSSTV/slowrx, RX stretches real Scottie.
- Live line 0 offset ~0.3 ms (~1 px M4).
- `Decoder.reset()` on completion discards rest of chunk (multi-image single feed).
- `cw.make_cw` with `peak_dbfs > 0` wraps int16 — clip.

### Nits / cleanup
Stale docs on Robot 36 batch path (so the stated reason to skip slant re-decode is obsolete); false "byte-identical" claim (live has 3-tap boxcar); `banner.py` docstring layout wrong; dead `find_leader`/`find_line_starts`/`freq_to_luma`/`resample_to`; duplicates: `_sample_pixels{,_inc}`, `_bandpass`/`_bp_window`, `_find_runs`, Robot 36 constants ×4 and format detection ×3.

### Verified solid
Mode table VIS codes/timings correct for Martin, Robot 36, PD, Wraase, Pasokon; VIS parity/LSB/stop-bit; NaN/inf + empty-input guards; YCbCr int32 promotion; phase-continuous encoder; buffer caps in all states.

---

## 6. Maintainability (brief)
- `MainWindow` ~4.3k lines; extract rig-connection and file-saving controllers.
- Four near-copy `_connect_*` methods; rigctld launch/stop duplicated (root cause of UI-H1).
- Two copies of the stop-and-wait nested event loop (share UI-M6).
- 3–4 image/audio save implementations; only autosave is atomic.
- `TXContext` built in four places (root of C-M3/C-M4).
- Dead v0.2 template system (`config/templates.py`, `template_editor_dialog.py`, `qso_template_bar.py`, `quick_fill_dialog.py`) slated for removal in v0.4.
- Inconsistent exception breadth in slots — a shared "slot exception → status bar" helper.
