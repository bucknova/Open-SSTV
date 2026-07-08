# Remote Web Access — Architecture

> **Status:** Design. No implementation yet. This is the spec the mockup
> is a front-end for.
>
> **Mockup:** [`design/remote/mockup.html`](mockup.html) — interactive,
> self-contained. Poke the TX gate, the confirm modal, and the
> dead-man's-switch.

Let a web browser remotely **view** decoded SSTV images, **compose &
send** images (including camera "selfies"), over the local network — while
the desktop app remains the single point of control and responsibility for
the radio.

---

## 1. Principles (ratified)

1. **The web is a request surface; the app is the sole authority.** The
   browser can only *ask*. The desktop app *decides and acts*. The web has
   no direct control over the rig — there is no wire command that keys the
   transmitter, sets frequency, or touches CAT/PTT.
2. **Two planes.** A *view plane* (read-only, safe, high value) and a
   *control plane* (keys the transmitter — dangerous, regulated). They are
   authorized differently and fail differently.
3. **The request vocabulary is the security boundary.** Capability is
   expressed by *which typed commands exist*, not by trusting the client.
4. **Reuse, don't reinvent, the safety primitives.** TX gating,
   watchdog/unkey, and the compositor already exist. Remote access routes
   through them; it does not add parallel rig-control paths.
5. **LAN-first.** WAN is explicitly out of scope (§10).

---

## 2. Server-inside-the-app

The server is **embedded in the desktop app** (not a headless-engine
refactor, not a standalone sidecar). It runs so the Qt event loop is never
blocked:

- **A stdlib `ThreadingHTTPServer` on a dedicated daemon thread** — no
  async framework, no new dependency (Phase 1). Qt owns the main thread.
- **Transport: SSE + POST, not WebSocket** (decided during Phase 2, see
  §3a). The whole feature is server-push + discrete client requests;
  nothing needs true bidirectional streaming, so it stays on the stdlib
  server end to end.
- **Inbound rig-touching requests marshal onto the Qt main thread via the
  app's existing internal signals** — `_request_transmit` /
  `_request_test_tone`. A `tx.confirm` handler emits the *same signal the
  GUI Send button emits*. The web is just another button on the same
  panel, so there is exactly one code path that can key the rig.
- **A broadcast hub goes the other way:** `EventHub` — one in-app
  subscriber to `RxWorker` / `TxWorker` Qt signals (via GUI-thread bridge
  slots) fans events out to all connected SSE clients.

```
  Browser ──POST──────▶ Server thread ──Qt queued signal──▶ Qt main thread
                        (stdlib http)    (_request_transmit)    (rig I/O)
  Browser ◀──SSE───────  EventHub  ◀────Qt signal──────────  RxWorker/TxWorker
```

**Load-bearing invariant:** web requests enter the rig *only* through the
existing internal signals. This is what makes "sole authority" real.

---

## 3. Protocol — the two planes

The request vocabulary is the API *and* the authorization model. There is
no `rig.setFrequency`, no `ptt.on`; the web cannot express "key the rig,"
only "request a transmit the app may refuse."

| Plane | Commands | Gate |
|---|---|---|
| **View** | `rig.subscribe`, `rx.stream`, `gallery.list` / `gallery.thumb` / `gallery.image`, `logbook.list` | authenticated |
| **Compose** | `compose.upload(photo)`, `compose.render(template, tokens)` → preview | authenticated |
| **Control** | `tx.request` → `tx.confirm(token)` → `tx.abort` | authenticated **+ TX-enable gate + per-TX confirm + live heartbeat** |

---

## 3a. Transport — SSE + POST, not WebSocket

**Decision (Phase 2):** the live stream is **Server-Sent Events**, and
client actions are **plain POSTs** — no WebSocket, no async framework,
ever.

The reflex choice was a per-client WebSocket. But walking through what the
feature actually does, nothing needs true bidirectional streaming:

- **Server → client** (RX progress, live preview, gallery updates; later
  TX progress, awaiting-confirm, on-air state, lease changes) — that's
  server-push, which is exactly **SSE**. One long-lived `GET /api/events`
  per client, fed by `EventHub`.
- **Client → server** (`tx.request`, `tx.confirm`, `tx.abort`, and the TX
  **heartbeat**) — discrete messages. The heartbeat is a `POST` once a
  second; the dead-man's-switch is "no heartbeat POST in 2.5 s → unkey."
  Ordinary requests on the stdlib server.

So SSE + POST covers **both planes** and keeps the whole surface on the
Phase 1 stdlib `ThreadingHTTPServer` — no aiohttp/Starlette, nothing to
re-port at the control-plane phase. SSE also auto-reconnects and rides the
token gate for free. WebSocket buys only lower-latency bidirectional
streaming, which a 1 s heartbeat and push-progress simply don't need.

Latency note: SSE server-push is effectively instant; a 1 s heartbeat
POST is well inside the 2.5 s dead-man window. No meaningful cost.

---

## 3b. Ordering — control before compose

**Decision:** build the **control plane before the compose plane**
(reversing the original phasing).

The dependency runs one way: **compose depends on control, not the
reverse.** Compose (camera → template → an image) exists *to transmit the
result* — its "send" action is control's `tx.request`. Built first, it's a
staging screen whose only button does nothing. Control, by contrast, needs
no compose: the view plane already surfaces a gallery full of images, and
the desktop already re-sends a gallery image to TX, so "remotely transmit
an existing image" is fully buildable and testable today.

The original ordering was a risk gradient — do the safe, no-rig work
(compose) before the dangerous, regulated work (control). But deferring
control doesn't make it safer; it just postpones the crux, since *all* the
hard safety design (lease, per-TX confirm, dead-man's-switch, enable gate,
Part 97) lives in control. Control-first is in fact more conservative: it
gets the transmitter-keying path onto real hardware sooner, with more time
to shake it down before compose complexity is layered on top.

---

## 4. Authentication — QR pairing + device tokens

**Decision: QR pairing with per-device tokens.** No passwords typed on a
phone.

- The app's Settings → Remote shows a **QR code / link containing a
  one-time pairing code** (short TTL).
- The browser opens it once and is issued a **long-lived per-device
  token**, stored client-side, sent on every subsequent request.
- **Tokens are per-device and individually revocable** in Settings
  (name, last-seen, revoke). Compromise of one device never exposes
  others.
- A token authenticates the *device*; it does **not** by itself confer TX
  authority (see §5).
- Pairing codes and tokens are generated and validated **in the app**; the
  browser never sees a shared secret it could leak.

> Never route real credentials through chat or the browser's URL; pairing
> is code-based and app-issued. (Consistent with the project's credential
> policy.)

---

## 5. TX authority — single-writer lease

**Decision: single-writer lease.** Being paired lets you *view* and
*compose*; transmitting requires holding the lease.

- **Exactly one client holds TX authority at a time**, via an explicit,
  revocable lease surfaced in the UI ("You have the key" / "Held by
  <device>").
- Other clients are **view+compose only** until they explicitly *take the
  key* (which the app may require the current holder to release, or
  timeout).
- **The local GUI can always reclaim** the lease instantly — the physical
  operator at the machine is never locked out by a remote.
- The lease is **liveness-bound**: if the holder's heartbeat POSTs stop
  (or its SSE stream drops), the lease lapses — and any in-flight TX hits
  the dead-man's-switch (§6).

This prevents two operators racing for the transmitter while keeping the
mental model simple.

---

## 6. Control plane lifecycle

Reuses the v0.4.1 watchdog / unkey-retry primitives; adds no new keying
logic.

```
tx.request        → app checks TX-enable gate AND lease holder
                  → app renders composite (§7), returns preview + token
tx.confirm(token) → marshal _request_transmit onto the Qt thread
                  → TxWorker keys rig, streams tx.progress over SSE
heartbeat lapses  → dead-man's-switch fires
  OR socket drops → v0.4.1 unkey-retry path unkeys the rig
tx.abort          → operator-initiated stop → same unkey path
```

The browser POSTs a **heartbeat** each second during TX (and holds the SSE
stream open for progress). Miss the window → the app assumes the operator
lost the link and **unkeys automatically**. This is the single most
important safety behavior and it lives entirely in the app.

**3c wiring invariants** (the `ControlPlane` state machine from 3a already
enforces its side; these are the app-side obligations when it is wired):

- **`transmit` and `unkey` must execute in dispatch order** — route both
  through the *same* ordered channel (e.g. both marshalled onto the Qt
  thread), or a stale key can outlive the unkey that supersedes it. The
  state machine dispatches them in the correct order under its lock; the
  app must not reorder them.
- **`tick()` must run on a reliable short timer** (a QTimer, ≪ 2.5 s) —
  the dead-man's-switch only fires when `tick()` runs. The `TxWorker`'s
  own v0.4.1 watchdog is the independent backstop if the GUI loop stalls.
- **`unkey` should be the thread-safe stop** (`TxWorker.request_stop`,
  already safe from any thread) so a lost-heartbeat unkey doesn't depend
  on the event loop being responsive.
- **Disabling `remote_tx_enabled` mid-TX** already unkeys via `tick()`'s
  continuous gate check; the config-apply path should additionally
  `reclaim_local()` for immediacy.
- **`client_id` must be a stable per-browser id**, distinct from the
  shared access token (two tabs share the token but must be different
  lease clients) — the browser generates one and sends it.

---

## 7. Media pipeline — camera → app → air

The selfie path is the one place bytes travel *up*:

1. Browser captures via `getUserMedia`; does only **lossless pre-upload
   framing** (rotate / crop) client-side.
2. Browser `compose.upload`s the photo bytes; app stages them.
3. App runs the photo through the **existing v0.3 template compositor**
   (`templates/renderer.py`) with the token values (`{tocall}`, `{rst}`,
   `{name}`, `{note}`, `{callsign}`, …).
4. **That composited image is what `tx.confirm` transmits.** The browser
   preview was always an approximation; the on-air bytes come from one
   renderer. WYSIWYG fidelity, zero duplicated layout logic.

**Editing scope (deliberately narrow):** rotate + crop + pick-a-template
covers ~95% of the value. Filters / paint / text placement belong as
**template layers on the server**, never as a JS image editor — otherwise
the compositor forks.

---

## 8. Logging — draft entry, operator confirms

**Decision: remote TX opens a pre-filled logbook draft the control op
confirms.**

- A remote send pre-fills a v0.4 QSO draft from the compose tokens
  (`{tocall}`, `{rst}`, name, freq/mode from rig state) and surfaces it for
  confirmation.
- Consistent with the **party-line rule**: we don't auto-log by default;
  the human decides what counts as a worked QSO.
- Nothing is written silently.

---

## 9. Regulatory spine

Part 97 makes a licensed **control operator** responsible for every
emission. The design already respects this because the app *is* the control
point:

- **Station ID timer, TX time limits, and the enable-gate live in the app**
  and fire regardless of what the browser does — or whether a browser is
  even connected.
- The browser is a convenience surface for a control operator who remains
  legally the operator. This is the honest framing for user docs.

---

## 10. Scope boundary — LAN only

**WAN is out of scope for v1.** No NAT traversal, port-forwarding, dynamic
DNS, or relay. Exposing a transmitter to the open internet multiplies the
security and reliability surface for little gain.

**Recommended answer to remote-over-internet:** run **Tailscale or a VPN**
— the app then serves over the existing LAN design with no new code. One
sentence of documentation instead of a subsystem.

---

## 11. Configuration surface

New **Settings → Remote** tab (all default-off):

- Enable remote access (master switch).
- Bind address + port; default bind to LAN interface, not `0.0.0.0`
  blindly.
- TLS (self-signed cert generated by the app; browser trust-on-first-use,
  or documented import). Even on LAN, the control plane should not run
  cleartext.
- Paired devices list (name, last-seen, revoke).
- TX-from-remote master enable (separate from the master switch — you can
  allow remote *viewing* while forbidding remote *transmit*).
- Show pairing QR.

---

## 12. Security hardening (checklist)

- TLS on the control plane; TOFU or documented cert import.
- Origin / host header checks on state-changing POSTs.
- Rate-limit auth and `tx.request`.
- Bind to the intended interface; never assume LAN = trusted for TX.
- Per-device token revocation; short pairing-code TTL.
- Audit trail: the request log (already in the mockup) persists who
  transmitted what, when.

---

## 13. Phasing

1. **Read-only spike** ✅ *(shipped, branch)* — embedded stdlib server
   streams the real gallery to a browser; opaque-id path fence; dev token;
   no TX. Proved the threading model on real hardware (Mac + phone).
2. **View plane** — *2a* ✅ *(shipped, branch)*: live RX stream via SSE
   (`EventHub` + `/api/events`), live in-progress preview
   (`/api/rx/preview`), auto-updating gallery. *2b* ✅ *(shipped, branch)*:
   Settings → Remote tab (enable / LAN bind / port / token + live browse
   URL **and a scannable pairing QR** via `segno`); `result_config` now
   round-trips the `remote_*` fields so a save no longer resets them, and
   Save restarts the server live. *2c* ✅ *(shipped, branch)*: read-only
   logbook view in the browser (`GET /api/logbook`, Gallery/Logbook tabs,
   RX/TX badges, logbook→image cross-link). **View plane complete.**
   Web header shows a live status pill; the desktop app shows a
   persistent "Remote on" status-bar indicator.
3. **Control plane** *(next — was #4; moved ahead of compose, see §3b)* —
   remotely transmit an **existing gallery image**. Single-writer lease,
   `tx.request → confirm → transmit`, heartbeat + dead-man's-switch,
   remote-TX enable gate (default off, separate from view), logbook draft.
   Reuses the desktop `_request_transmit` seam and the v0.4.1 unkey path;
   the only new image source is "re-send a gallery image", so it needs
   nothing from compose.
   - *3a* — foundations, **no rig contact**: config gate
     `remote_tx_enabled`, the pure lease + TX state machine +
     dead-man's-switch (injected clock + transmit/unkey callbacks), fully
     unit-tested headless.
   - *3b* — POST endpoints (`do_POST`, Origin/Host checks) + control-plane
     SSE events + browser UI, still stubbed away from the rig.
   - *3c* ✅ *(built, branch — awaiting real-HW shakedown)* — `ControlPlane`
     in `main_window` with real callbacks: `transmit` marshals onto the Qt
     thread (`_remote_tx_request`) which **re-checks the state** before
     keying via the same `_request_transmit` as local Send; `unkey` is the
     thread-safe `TxWorker.request_stop` (works even if the GUI stalls).
     `on_tx_finished` fed from TX-complete/aborted; `reclaim_local` on
     local Send/test-tone, gate-off, and closeEvent. Remote TX reuses the
     existing auto-save + logbook-draft path. Settings "Allow remote
     transmit" checkbox (default off). **First real key-down still needs
     explicit opt-in + a careful real-hardware shakedown + its own
     adversarial review** before a browser keys the rig live.
4. **Compose plane** *(was #3)* — camera upload, server-side
   `compose.render`, template strip. Additive: gives the (already-built)
   control plane new image sources; its "send" endpoint is control's
   `tx.request`.
5. **Hardening** — TLS, revocation UI, rate limits, audit persistence,
   docs (incl. Part 97 framing + Tailscale for WAN).

---

## References

- Mockup: [`mockup.html`](mockup.html) ·
  [artifact](https://claude.ai/code/artifact/5364a6d0-f8e3-4372-9d4f-fa9a360295c4)
- Reused seams: `gallery/` and `logbook/` (Qt-free, cheap to serve),
  `templates/renderer.py` (compositor), `ui/workers.py`
  (`RxWorker`/`TxWorker`), the `_request_transmit` / `_request_test_tone`
  internal signals, and the v0.4.1 watchdog / unkey-retry path.
