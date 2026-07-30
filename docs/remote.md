# Remote Station (v0.6)

Open-SSTV can serve a small web page from the machine running the app, so
a phone or laptop on the same network can watch receptions live, browse
the gallery and logbook, and — if you explicitly allow it — compose and
transmit an image.

Everything here is **off by default**. Nothing is reachable until you turn
it on, and transmitting requires a *second*, separate opt-in.

---

## Turning it on

**Settings → Remote → Enable the remote gallery server.**

| Setting | Default | What it does |
|---|---|---|
| Enable the remote gallery server | **off** | Master switch. Off means nothing is listening. |
| Host | `127.0.0.1` | `127.0.0.1` is this machine only. Use `0.0.0.0` to allow other devices on your network. |
| Port | `8730` | TCP port the page is served on. |
| Access token | *(generated)* | Required on every request. Treat it like a password. |

With the server running, the Remote tab shows the URL and a **QR code** —
scan it with your phone's camera and the token comes along in the link.
The desktop app also shows a persistent "Remote on" indicator so you never
forget it's listening.

> **Only put this on a network you trust.** The token keeps out casual
> visitors on your LAN, but this is plain HTTP with no encryption. Don't
> forward the port to the open internet.

---

## What you can do from the browser

**Gallery** — every received image (newest first), tap to view full size.

**Live** — while a signal is decoding, the picture paints in line by line,
the same as the desktop RX panel.

**Logbook** — your logged contacts, read-only.

**Compose** — take a photo with the phone's camera or pick one from your
library, frame it (drag to move, pinch or use the slider to zoom), choose
a station template and SSTV mode, then transmit. The crop box matches the
selected mode's frame shape, so what you frame is what goes out — including
the narrow modes like Scottie S2 and Martin M2.

The station renders the final image, not the browser: your phone sends the
photo and the text, and the desktop composites the exact bytes it
transmits.

---

## Transmitting — the safety model

Remote transmit is a separate switch: **Settings → Remote → Remote
transmit**, off by default even when the server is on.

You also need a **connected CAT rig**. Remote TX is refused on the
manual/VOX backend, because there PTT isn't actually commanded — the
safety net below could stop the audio but not drop your transmitter.

Once enabled, every transmission passes through these:

- **One operator at a time.** A browser must *take control* before it can
  transmit; a second device is refused while the first holds it. Your
  desktop always wins — pressing Send or Stop locally takes control back.
- **Confirm before key-down.** Requesting a transmission returns a
  single-use token that expires in 30 seconds; the rig isn't keyed until
  you confirm.
- **A dead-man's-switch.** While transmitting, the browser sends a
  heartbeat. If it stops — you close the tab, the phone drops off Wi-Fi,
  the battery dies — the station **unkeys the rig automatically**.
- **A full-screen ON AIR takeover** with a progress bar, elapsed/remaining
  time, a live "link alive" indicator, and a big **Abort** button.

> **Keep the page open and awake while transmitting.** If your phone locks
> its screen mid-transmission, the heartbeat can stop and the station will
> unkey — cutting the image off partway. This is the safety net doing its
> job, but it does mean a half-sent picture.

As with any unattended-capable setup, enabling your rig's own **TX
time-out timer** is worth doing. It's an independent backstop that doesn't
depend on this software at all.

---

## Troubleshooting

**The page won't load from my phone.** Host is probably still
`127.0.0.1`, which only serves the machine itself. Set it to `0.0.0.0`,
save, and use the address shown in the Remote tab.

**"Unauthorized".** The token is missing or wrong — rescan the QR rather
than typing the URL by hand.

**The Camera button does nothing / no camera appears.** Browsers only give
web pages camera access over HTTPS or on `localhost`. Over a plain LAN
address the in-page camera is blocked, so Open-SSTV falls back to your
phone's normal camera app — if that doesn't appear either, use **Upload**
and pick a photo from your library instead.

**"No rig connected at the station."** Remote transmit needs a CAT
connection (see Settings → Radio). Connect the rig, then retry.

**"Remote transmit is off in the station's settings."** The master server
switch is on but the transmit switch isn't — they're deliberately separate.

**The transmission stopped partway.** The dead-man's-switch fired because
the heartbeat lapsed — usually a locked phone screen or a Wi-Fi drop. Keep
the page open and the screen awake for the length of the transmission
(around two minutes for Scottie S1).

---

## See also

- [docs/gallery.md](gallery.md) — the desktop image browser
- [docs/logbook.md](logbook.md) — the QSO log the Logbook tab reads from
- [design/remote/architecture.md](../design/remote/architecture.md) —
  design notes and the safety rationale, for the curious
