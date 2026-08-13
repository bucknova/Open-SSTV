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

### Not every decode is your QSO

SSTV calling frequencies are party lines: when you monitor 14.230,
most of what you decode is *other people's* exchanges — someone else
answered that CQ, and their contact doesn't belong in your logbook.
**Settings → General → Logbook → RX capture** controls when a
finished reception offers the dialog:

- **Ask after every reception** *(default)* — the classic behaviour;
  Esc dismisses the ones that aren't yours.
- **Ask only while in a QSO** — the dialog appears only when the TX
  panel's **ToCall** is filled in (you're working someone).  Pure
  monitoring never interrupts you.
- **Never ask** — no dialog at all; you log deliberately from the
  gallery (below).

Whatever the setting, **any decoded image can be logged from the RX
gallery**: right-click a thumbnail → **Log QSO…** opens the same
pre-filled form.  That's the natural workflow for a monitoring
station — decode freely all afternoon, then log only the exchange
that was actually with you.  (Logged that way, the frequency and
timestamp are taken at log time, so log promptly after the QSO if
those details matter.)

Back-to-back images from your partner while the dialog is already
open are saved silently as drafts so nothing of *your* QSO is lost;
third-party traffic in the same situation is simply left in the
gallery.

Your own **transmissions always offer the dialog** — what you put on
the air is always yours to log.

Notes for the dialog:

- RSV reports use the SSTV convention (Readability / Strength /
  Video, e.g. `595`).  ADIF export writes them to `RST_SENT` /
  `RST_RCVD`, which is where every logger expects signal reports.
- If image auto-save is off, the image is written to your images
  folder at the moment you save the QSO, so the logbook entry keeps
  its picture.  Dismissing the dialog writes neither a row nor a file.
- Decoding a WAV through **Decode Audio** also offers a log entry —
  useful for logging a contact you recorded off-air.  File decodes
  are stamped with the *recording's* modified time and no frequency
  (the rig's current dial says nothing about an old recording), so
  fill in what you know in the notes.

### Silent logging

Prefer no dialogs at all but want everything kept?  Enable
**Settings → General → Logbook → Log QSOs silently**.  Every
completion — TX and RX alike — is saved as a draft entry (no
dialog), and you fill in callsigns later from the Logbook window.
Draft rows are shown in grey as `(draft)`.  Note this overrides the
RX-capture setting above and *will* hoover up third-party traffic on
a monitored frequency — it suits active operating sessions, not
unattended monitoring.

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

## Broadcasting to a companion logger (UDP)

*New in v0.7.*  Besides the SQLite logbook and file-based ADIF export
above, Open-SSTV can also broadcast a single QSO over UDP the instant
you finish working it — the same "UDP logging" convention WSJT-X
popularized, so QLog, JTAlert, GridTracker, Log4OM, N1MM, and similar
companion loggers can pick the contact up automatically without you
ever touching a file.

### Why this is a separate button

An SSTV QSO is usually several images (yours, theirs, maybe a
follow-up), but the capture flow above writes one row **per image** —
right for the local logbook (which wants every picture), wrong for a
one-shot broadcast to another program (which wants exactly one
contact). So this is a deliberately separate, manual action:

- **[External Log]** — a button on the TX panel's QSO bar, right next
  to **[Logbook…]**.
- Click it once, when the QSO is actually done, and it sends exactly
  one UDP datagram built from whatever is currently in the bar.
- It **never touches the local SQLite logbook** — sending (or failing
  to send) has no effect on your logbook rows, and vice versa. Use
  both, either, or neither per QSO as you like.

### What goes into the datagram

The QSO bar now has two rows of fields:

- **ToCall**, **RSTs** (sent), **RSTr** (received) — top row.
- **Name**, **QTH**, **Grid** — second row. Grid is upper-cased as you
  type, same as ToCall.
- **Note** — third row, same free-form field the local logbook uses.

Everything else is filled in automatically at the moment you click:
**time** is the click's UTC timestamp, **mode** is always `SSTV`, and
**frequency** is whatever the Radio panel's **Freq:** field currently
shows (blank/`—` when no rig is connected, in which case the record
simply omits BAND and FREQ rather than sending a bogus `0`). Your
station identity (callsign, grid, QTH, name from Settings → General)
rides along the same way it does in ADIF export.

### Format and destination

**Settings → Logging → UDP QSO log** configures where the datagram
goes:

- **Host** / **Port** — default `127.0.0.1:2237`, which is WSJT-X's
  own default UDP port; most companion loggers already listen there
  out of the box.
- **Format** — two wire formats, because two incompatible real-world
  conventions exist:
  - **WSJT-X protocol** *(default)* — the same framed binary "Logged
    ADIF" message WSJT-X itself sends. QLog, JTAlert, GridTracker, and
    N1MM all expect this specifically.
  - **Raw ADIF** — a bare ADIF record with no framing, the format
    Log4OM-style listeners expect.

Each click opens a fresh UDP socket, sends one datagram, and closes it
— fire-and-forget, no acknowledgement, no persistent connection to
manage. If the send fails (nothing listening, bad host), the status
bar reports it; nothing else in the app is affected.

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
