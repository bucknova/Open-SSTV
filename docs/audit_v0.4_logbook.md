# v0.4 Logbook — pre-release audit findings

Audited 2026-06-11 on branch `v0.4-logbook` at `7819086` (diff scope
`main...HEAD`, 30 files, ~4,900 insertions).  Method: seven
independent finder passes (line-by-line, removed-behavior,
cross-file tracing, reuse, simplification, efficiency, altitude),
~36 candidates deduplicated, each survivor adversarially verified.
Three findings were reproduced empirically during the audit.

**Status: ALL TEN FIXED** (2026-06-12, same branch, commit after this
doc update).  Every fix carries a regression test; the per-finding
"Fix:" notes below describe what was implemented.  The cleanup-debt
list at the bottom remains open for a later /simplify pass.

Severity key: 🔴 must fix before release · 🟡 should fix · 🔵 ride-along.

---

## 🔴 1. One bad date aborts an entire ADIF import

`src/open_sstv/logbook/adif.py:185` — `parse_qso_date_time` builds
`datetime(year, month, day, …)` from digit-checked strings, but
digit-valid out-of-range values (`20260231`, `TIME_ON 250000`) raise
plain `ValueError`, which escapes `_build_qso`'s
`except AdifParseError` (line ~376) and aborts `import_adif`
entirely.  **Reproduced**: a 2-record document with one Feb-31 record
imports zero rows.  Violates the module's own lenient-parse contract
("malformed records are skipped").

**Fix:** wrap the `datetime(...)` construction and raise
`AdifParseError` (or catch `ValueError` alongside `AdifParseError`
in `_build_qso`).  Regression test: bad-date record + good record →
good record imports, bad one skipped with warning.

## 🔴 2. Silently-drafted QSOs lose their image forever

`src/open_sstv/ui/main_window.py:952` (`_capture_qso` silent branch)
— the deferred write-image-at-log-time step exists only in
`_on_capture_dialog_finished` (lines ~1011-1014).  The silent paths
(`auto_log_qsos=True`, or dialog-busy + engaged partner image) call
`save_draft` with `image_path=None` and never write the PIL image to
disk when `auto_save`/`autosave_tx` are off (the defaults).  TX is
worst: `_last_tx_image` is cleared immediately after and TX images
never enter the gallery — instant loss.  RX images survive only
until gallery eviction (20 items) or app exit.  **Confirmed**
reachable with default config + one supported checkbox.

**Fix:** move the "ensure image on disk" step into the silent path
too — e.g. a `_persist_draft(draft, preview_image, mode)` helper
used by both branches.  Tests: auto-log RX and busy-path TX drafts
end up with an on-disk `image_path`.

## 🔴 3. Capture flow can run mid-shutdown against a closed store

`src/open_sstv/ui/main_window.py:2936` (`closeEvent`) — the
coordinator's store is closed near the top, but
`RxWorker.image_complete` is never disconnected, and
`audio_worker.stopped → rx_worker.flush` can emit a (possibly
watchdog-truncated) `image_complete` that the nested
`_close_loop.exec()` (≤2 s, AllEvents) delivers mid-teardown.  With
auto-log it re-opens the just-closed DB via the lazy `store`
property (connection never closed); with defaults it constructs and
`open()`s a window-modal dialog on a dying window.  The RX-thread
watchdog QTimer also stays live until `_request_rx_shutdown`
(line ~3028).  **Confirmed** from ordering + signal graph.

**Fix:** in the early-disconnect block (~2948), also disconnect
`image_complete`/`rx_audio_ready` from the capture handlers (or set
a `self._closing` flag checked at the top of
`_on_rx_image_complete`/`_capture_qso`), and move
`_logbook_coordinator.close()` after the thread joins.

## 🔴 4. Offline WAV decodes logged with live frequency + today's time

`src/open_sstv/ui/main_window.py:2006` — `_on_rx_image_complete`
serves live RX, offline decode, and watchdog truncation alike, and
stamps `frequency_hz=self._last_rig_freq_hz` + `time_utc=now()`.
Decoding an old recording with the rig tuned to 14.230 MHz produces
a draft claiming 14.230/today — silently written under auto-log,
shown read-only (uncorrectable) under prompting.  LoTW/eQSL match on
timestamp, so such rows can never confirm.  docs/logbook.md
currently *recommends* the off-air-recording workflow with no
caveat.  **Confirmed.**

**Fix options (pick at fix time):** pass `frequency_hz=None` (and
ideally the WAV file's mtime as a better-than-now timestamp) for the
offline-decode path — the offline worker's completions are
distinguishable at connect time (`_on_decode_audio_file_requested`
wires its own worker, main_window.py:~1284) — plus a docs caveat.

## 🔴 5. Date filters off by one UTC day

`src/open_sstv/ui/logbook_dialog.py:310` (`_query_kwargs`) — local
calendar dates from the QDateEdits are converted to UTC midnight
bounds for the UTC `time_utc` column.  Operator at UTC-5 with
"Until: <local today>" silently loses QSOs logged this evening
(stored tomorrow UTC).  **Reproduced** via store query.

**Fix:** interpret the picked dates in *local* time and convert:
`datetime(d.year(), d.month(), d.day(), tzinfo=<local>)
.astimezone(UTC)` for `since`, same +1 day for `until`.  Test with a
fixed non-UTC tz via `time_utc` fixtures (don't depend on the
machine tz — construct bounds explicitly).

## 🔴 6. SQL LIKE wildcards unescaped in filters

`src/open_sstv/logbook/store.py:329` (callsign) and `:337` (mode) —
user text is interpolated into `LIKE '%…%'` patterns without
escaping `%`/`_`.  Typing `_` matches every row; `W0_EZ` matches
`W0AEZ`.  **Reproduced.**

**Fix:** escape `\`, `%`, `_` in the user text and add
`ESCAPE '\'` to both LIKE clauses.  Tests: literal-underscore
callsign filtering, `%` matches nothing unless present literally.

## 🟡 7. Failed edits display as applied

`src/open_sstv/ui/logbook_dialog.py:424` (`_on_edit`) —
`LogQsoDialog` receives the model's own row object and
`result_qso()` mutates it in place *before* `store.update()` can
fail.  A persistently broken store (locked/corrupt/unmounted) fails
both `update` and the corrective `refresh()` → table, detail panel
(on re-select), and even ADIF export show the never-persisted edit.
**Confirmed** (double-failure, single root cause fails both).

**Fix:** pass a copy into the dialog (`dataclasses.replace(qso)`)
and only refresh on success — or snapshot/restore on failure.
Note `store.insert` already avoids aliasing; `update` path should
match.

## 🟡 8. Filter bar: no debounce + modal error per keystroke

`src/open_sstv/ui/logbook_dialog.py:171/178` — `textChanged` wires
straight to `refresh()`: full query + model reset per keystroke
(selection wiped mid-typing), and with an unreadable DB each
keystroke costs a ~5 s sqlite busy-timeout freeze followed by a
modal "Logbook unavailable" — serial whack-a-mole.  Cloud-synced DBs
(endorsed by docs) make this reachable.  **Confirmed.**

**Fix:** single-shot 300 ms QTimer debounce (mirror
`qso_state_widget._DEBOUNCE_MS`), and replace the per-call modal
with a once-per-failure inline state (e.g. disable + status label
until a refresh succeeds).

## 🟡 9. Diagnostics zips a potentially torn/hot-journal DB copy

`src/open_sstv/ui/diagnostics.py:114` — raw `read_bytes()` takes no
lock and ignores a hot `-journal` sidecar.  Strongest case needs no
concurrency: prior crash mid-commit leaves a hot journal; lazy store
means relaunch may never roll it back; export ships a silently
corrupt DB in the bug-report zip.  **Plausible** (narrow windows).

**Fix:** snapshot via `sqlite3.Connection.backup()` into a temp file
(triggers journal recovery, consistent snapshot), then
`zf.write(tmp, "logbook.db")` — also fixes the whole-file-in-memory
inefficiency.

## 🔵 10. ADIF import: per-row transactions + full-table dedupe load

`src/open_sstv/logbook/coordinator.py:229` — dedupe seen-set
materializes every row as a full QSO (3 datetime parses each), and
inserts run one autocommit transaction (fsync) per row, on the GUI
thread.  10k-record import ≈ minutes frozen vs sub-second batched.

**Fix:** key-only `SELECT callsign, time_utc, mode FROM qsos WHERE
callsign != ''` for the seen-set; wrap inserts in one explicit
transaction (or `store.insert_many`).

---

## Cleanup debt (non-blocking, for a later /simplify pass)

- **Duplicated helpers, already drifting:** thumbnail/preview
  loading trio (`log_qso_dialog._set_thumbnail` vs
  `logbook_dialog._set_preview` — fallback texts already differ);
  uppercase-on-type (`log_qso_dialog` vs `qso_state_widget`);
  `_RSV_PRESETS` vs `_RST_PRESETS`; `format_frequency` vs the inline
  MHz formatting in `_on_tune_requested`.  Home for all: `ui/utils.py`.
- **schema.py** now has four copies of the normalise-with-fallback
  block — extract one `_normalise_choice` helper.
- **`_capture_context` 3-tuple** → store preview image + source mode
  on the dialog itself; collapse the duplicated busy-check
  (`_capture_qso` + `_open_capture_dialog`).
- **Capture policy** (prompt/draft/skip) is split across three
  main_window layers; a pure
  `coordinator.decide_capture(source, engaged, busy)` would make it
  unit-testable and single-owner.
- **`store_is_open`** has no production callers (test-only).
- **store.py ISO helpers** re-implement
  `isoformat(timespec="seconds")` / `fromisoformat` (py≥3.11 accepts
  `Z`).
- **Stringly-typed "Log QSO…"** menu label matched via
  `startswith` in three modules — share a constant.
- **Lazy stdlib imports** (`pathlib` in logbook_dialog handlers,
  `QPixmap` in log_qso_dialog) — move to module top.
- **conftest hygiene**: consider a per-test DeferredDelete drain
  instead of (or alongside) the session-end sweep.
