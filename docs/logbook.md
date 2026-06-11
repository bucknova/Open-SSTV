# Logbook — capturing and exporting your SSTV contacts

*New in v0.4.*  Every transmission and reception can be captured as a
logbook entry — image, mode, frequency, time, callsign, RSV report,
and notes — and exported as ADIF for the loggers and award services
the rest of your station already uses.

## The capture flow

After a transmission finishes or an image finishes decoding, a **Log
QSO** dialog opens with everything Open-SSTV already knows filled in:

- **Mode** and **time (UTC)** — always present.
- **Frequency** — when a rig is connected (rigctld, direct serial CAT,
  or TCI), the dialog shows the rig's dial frequency.  With no rig
  control ("Manual" mode), the field is blank; you can't log what the
  radio didn't tell us.
- **Image** — the picture you sent or received, as a thumbnail.
- **TX extras** — whatever you typed in the TX panel's QSO bar
  (ToCall, RST, Name, Note) is pre-filled, so a contact you already
  described there needs zero retyping.

Type the callsign and signal reports, then **Save**.  Press **Esc**
(or Cancel) and nothing is written — a noise-triggered decode or a
test costs you one keypress.

Notes for the dialog:

- RSV reports use the SSTV convention (Readability / Strength /
  Video, e.g. `595`).  ADIF export writes them to `RST_SENT` /
  `RST_RCVD`, which is where every logger expects signal reports.
- If image auto-save is off, the image is written to your images
  folder at the moment you save the QSO, so the logbook entry keeps
  its picture.  Dismissing the dialog writes neither a row nor a file.
- Decoding a WAV through **Decode Audio** also offers a log entry —
  useful for logging a contact you recorded off-air.

### Silent logging

Prefer not to be interrupted mid-pileup?  Enable **Settings →
General → Logbook → Log QSOs silently**.  Completions are then saved
as draft entries (no dialog), and you fill in callsigns later from
the Logbook window.  Draft rows are shown in grey as `(draft)`.

## The Logbook window

**Tools → Logbook…** (Cmd/Ctrl+L) opens the log in its own window —
the same detached-logbook convention as MMSSTV, fldigi, and WSJT-X,
so the main TX/RX panels stay untouched while you browse.

- **Filter bar** — callsign substring, mode substring, RX/TX, and a
  date range.  Filters apply as you type.
- **Table** — newest first.  Double-click a row (or press *Edit…*) to
  open it in the same form the capture flow uses.
- **Detail panel** — image preview plus the full entry.  A row whose
  image file you've moved or deleted shows a *Missing image*
  indicator; the logbook never touches your image files, in either
  direction.
- **New…** — manual entry, with *Save & New* for keying in a stack of
  paper-log contacts.
- **Delete** — asks first, removes the row only.  Image and audio
  files on disk are left alone.

## ADIF export and import

The logbook's storage is a SQLite database; **ADIF 3.1.5** is the
interchange format.

**Export ADIF…** writes the *currently filtered* rows to a `.adi`
file.  Entries without a callsign (drafts) are skipped.  Your station
identity from Settings → General (callsign, name, grid, QTH) is
stamped into every record as `STATION_CALLSIGN` / `OPERATOR` /
`MY_GRIDSQUARE` / `MY_CITY`.

The specific SSTV mode travels in `SUBMODE` using the compact MMSSTV
convention (`MartinM1`, `Scottie1`, `PD120`); `MODE` is always
`SSTV`.  Band and `FREQ` (MHz) are derived from the logged frequency.

Interop, tested against the common consumers:

- **Ham Radio Deluxe / N1MM+** — File → Import ADIF; fields land in
  their standard columns.
- **LoTW** — sign the exported `.adi` with TQSL and upload.  (Direct
  TQSL integration is planned for a v0.4.x patch.)
- **eQSL / Club Log / QRZ.com** — all accept ADIF uploads of the
  exported file.

**Import ADIF…** reads `.adi`/`.adif` files from other loggers.
Imports are deduplicated on **(callsign, time, mode)** — re-importing
your own export, or overlapping exports from another logger, won't
create duplicate rows.  Mode names are normalised back to Open-SSTV's
display forms where they're recognised; unknown modes (RTTY, a future
SSTV mode) are kept verbatim rather than dropped.

## Where the data lives

The logbook is a single SQLite file:

| OS | Default location |
|---|---|
| Windows | `%LOCALAPPDATA%\open_sstv\open_sstv\logbook.db` |
| macOS | `~/Library/Application Support/open_sstv/logbook.db` |
| Linux | `~/.local/share/open_sstv/logbook.db` |

Back it up like any file; syncing it yourself (Dropbox, iCloud
folder) works because nothing else writes to it.  An advanced
override exists in the config TOML (`logbook_db_path`) for operators
who want the file somewhere specific — there's deliberately no UI for
it.

Privacy: the logbook is your list of worked callsigns.  **Settings →
Export Diagnostics…** therefore *excludes* it from bug-report zips
unless you tick the explicit *Include logbook* checkbox.

## Logging (the other kind)

v0.4 also adds **Settings → Logging**:

- **Log level** — DEBUG/INFO/WARNING/ERROR for the app's own log
  output (next launch).  `OPEN_SSTV_DEBUG=1` still forces DEBUG.
- **Open Log Folder** — jump straight to the rotating log files.
- **Export Diagnostics…** — same exporter as the General tab.
