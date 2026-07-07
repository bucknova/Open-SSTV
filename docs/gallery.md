# Gallery — browsing your SSTV images

*New in v0.5.*  A built-in image browser for the pictures you've
received (and, if you enable it, transmitted) — grouped and searchable,
linked to the contacts in your logbook, with one click to re-send an
image or jump to its QSO.

## Opening the gallery

**Tools → Gallery…** (Cmd/Ctrl+G) opens the gallery in its own window,
alongside the Logbook.  The two are companion windows over the same
data: the Logbook is the table of contacts, the Gallery is the wall of
pictures.

## What the gallery shows

The gallery is a **join between your image folder and your logbook**.
Every image file in your save directory appears as a thumbnail; if a
logbook contact links that image, the thumbnail carries the contact's
details too.  So you'll see:

- **Logged images** — full callsign / mode / frequency / time in the
  detail panel, and a **→ QSO** button that jumps to the row in the
  Logbook.
- **Unlogged images** — auto-saved receptions you never logged still
  show up, with their date and (from the filename) mode.

Sources scanned:

- Your **images save directory** (Settings → Images), where received
  images auto-save and where the capture flow writes logged images.
- **Transmitted images** — only if you've turned on TX auto-save
  (Settings → Images → *Auto-save transmitted images*); the gallery
  doesn't retain anything the app wasn't already saving.
- Any extra folders listed in the advanced ``gallery_extra_dirs``
  config key (no UI; edit the config TOML).

## Finding images

- **Filter** by callsign, mode, or date range — the same controls as
  the Logbook, applied live as you type.
- **Sort / group** by date (newest first), callsign, or mode.  The
  caption under each thumbnail follows the sort so the grid is easy to
  scan — dates when sorting by date, callsigns when sorting by
  callsign, and so on.

Dates throughout the gallery are shown in **UTC**, matching the
logbook and the rest of the app.

## What you can do

Select a thumbnail, then:

- **Re-send to TX** — loads the image into the TX panel as a new
  outgoing image, ready to transmit.  Handy for answering a CQ with
  the same picture you sent last time.
- **Export…** — saves a *copy* to a location you choose; the original
  is never moved.
- **Delete** — removes the image file from disk (with confirmation).
  If a logbook contact linked that image, **the contact is kept** —
  only its picture link is cleared.  This mirrors the Logbook's
  "delete a QSO keeps the image file": deleting from either side never
  silently destroys the other.
- **→ QSO** — for a logged image, opens the Logbook focused on that
  contact.

From the Logbook side, a contact with a saved image gets a **Show in
Gallery** button in its detail panel — the return trip.

## Performance & the thumbnail cache

Thumbnails are generated on demand as you scroll and cached on disk
(under the platform cache directory), so reopening the gallery is
instant.  Editing or replacing an image regenerates its thumbnail
automatically.  The gallery stays smooth into the tens of thousands of
images; the cache is pruned on close so it never grows without bound.

Nothing in the gallery is destructive except **Delete** (which always
confirms first): browsing, sorting, filtering, exporting, and
re-sending never change your files.
