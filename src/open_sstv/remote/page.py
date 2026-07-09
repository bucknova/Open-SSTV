# SPDX-License-Identifier: GPL-3.0-or-later
"""The read-only gallery viewer page served at ``/``.

A single self-contained HTML document (inline CSS + JS, no external
resources).  It reads the token from its own URL query string and uses it
for every API call, so the one URL the app logs
(``http://host:port/?token=…``) is all the operator needs to open.

Phase 2 adds the **live view plane**: an ``EventSource`` on ``/api/events``
(Server-Sent Events) drives a live panel that paints an in-progress decode
as it arrives and auto-refreshes the gallery when a new image lands — no
manual Refresh needed.  Kept as a Python string constant rather than a
bundled asset so it adds nothing to the PyInstaller data surface.  The
palette echoes the ``design/remote/mockup.html`` "remote head unit" look.
"""
from __future__ import annotations

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Open-SSTV — Remote Gallery</title>
<style>
  :root {
    --bg:#0c1416; --panel:#111e21; --line:#22343a; --ink:#e8f2ef;
    --muted:#7f9aa1; --accent:#34e39a; --amber:#f2b544; --tx:#ff5b48;
    --mono:"SFMono-Regular",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:5; display:flex; align-items:center;
    gap:14px; padding:12px 18px; background:rgba(12,20,22,.92);
    backdrop-filter:blur(8px); border-bottom:1px solid var(--line); }
  .statuspill { display:inline-flex; align-items:center; gap:7px; padding:4px 11px;
    border-radius:20px; border:1px solid var(--line); font-family:var(--mono);
    font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted);
    transition:color .3s, border-color .3s; white-space:nowrap; }
  .statuspill .d { width:8px; height:8px; border-radius:50%; background:var(--muted);
    transition:background .3s, box-shadow .3s; }
  .statuspill[data-state="live"] { color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 45%,var(--line)); }
  .statuspill[data-state="live"] .d { background:var(--accent);
    box-shadow:0 0 9px var(--accent); }
  .statuspill[data-state="receiving"] { color:var(--amber);
    border-color:color-mix(in srgb,var(--amber) 45%,var(--line)); }
  .statuspill[data-state="receiving"] .d { background:var(--amber);
    box-shadow:0 0 9px var(--amber); animation:pulse 1.1s ease-in-out infinite; }
  .statuspill[data-state="offline"] { color:var(--tx);
    border-color:color-mix(in srgb,var(--tx) 45%,var(--line)); }
  .statuspill[data-state="offline"] .d { background:var(--tx); }
  header b { letter-spacing:.02em; }
  header .sub { color:var(--muted); font-family:var(--mono); font-size:12px; }
  header .spacer { flex:1; }
  button { font:inherit; color:var(--ink); background:var(--panel);
    border:1px solid var(--line); border-radius:8px; padding:7px 13px; cursor:pointer; }
  button:hover { border-color:var(--muted); }
  #count { color:var(--muted); font-family:var(--mono); font-size:13px; }
  main { padding:18px; }
  /* live RX panel */
  .live { display:none; margin-bottom:20px; background:var(--panel);
    border:1px solid color-mix(in srgb,var(--accent) 40%,var(--line));
    border-radius:14px; overflow:hidden; }
  .live.show { display:grid; grid-template-columns:minmax(0,340px) 1fr; }
  @media (max-width:560px) { .live.show { grid-template-columns:1fr; } }
  .live .screen { background:#05090a; aspect-ratio:4/3; display:grid;
    place-items:center; }
  .live .screen img { width:100%; height:100%; object-fit:contain; display:block; }
  .live .screen .ph { color:var(--muted); font-family:var(--mono); font-size:12px; }
  .live .info { padding:16px 18px; display:flex; flex-direction:column; gap:10px;
    justify-content:center; }
  .live .info .eyebrow { font-family:var(--mono); font-size:11px; letter-spacing:.16em;
    text-transform:uppercase; color:var(--accent); display:flex; align-items:center;
    gap:8px; }
  .live .info .eyebrow .pulse { width:8px; height:8px; border-radius:50%;
    background:var(--accent); animation:pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
  .live .info h2 { margin:0; font-size:20px; }
  .live .bar { height:8px; background:#05090a; border-radius:6px; overflow:hidden; }
  .live .bar span { display:block; height:100%; width:0%; background:var(--accent);
    transition:width .3s ease; }
  .live .pct { font-family:var(--mono); font-size:12px; color:var(--muted); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
    gap:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
    overflow:hidden; cursor:pointer; transition:border-color .15s,transform .15s; }
  .card:hover { border-color:var(--accent); transform:translateY(-2px); }
  .card.fresh { animation:fresh 1.4s ease; }
  @keyframes fresh { 0%{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
    100%{border-color:var(--line)} }
  .card .thumb { aspect-ratio:4/3; width:100%; background:#05090a; object-fit:cover;
    display:block; }
  .card .meta { padding:9px 11px; }
  .card .name { font-family:var(--mono); font-size:11.5px; color:var(--muted);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .card .row { display:flex; align-items:center; gap:8px; margin-top:5px; }
  .badge { font-family:var(--mono); font-size:10.5px; letter-spacing:.04em;
    padding:2px 7px; border-radius:20px; border:1px solid var(--line); color:var(--muted); }
  .badge.call { color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 40%,var(--line)); }
  .badge.tx { color:var(--tx);
    border-color:color-mix(in srgb,var(--tx) 40%,var(--line)); }
  .empty,.err { color:var(--muted); font-family:var(--mono); padding:40px 8px;
    text-align:center; }
  .err { color:var(--tx); }
  /* view tabs */
  .tabs { display:flex; gap:8px; padding:14px 18px 0; }
  .tabs button { background:transparent; border:1px solid var(--line); color:var(--muted);
    border-radius:20px; padding:6px 16px; font-size:13px; }
  .tabs button[aria-selected="true"] { color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 45%,var(--line));
    background:color-mix(in srgb,var(--accent) 10%,transparent); }
  /* logbook table */
  .logwrap { overflow-x:auto; }
  .logtable { width:100%; border-collapse:collapse; font-size:13px; }
  .logtable th { text-align:left; color:var(--muted); font-family:var(--mono);
    font-size:10.5px; text-transform:uppercase; letter-spacing:.07em;
    padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
  .logtable td { padding:10px 12px; border-bottom:1px solid var(--line);
    font-variant-numeric:tabular-nums; white-space:nowrap; }
  .logtable tr[data-img] { cursor:pointer; }
  .logtable tr[data-img]:hover td { background:var(--panel); }
  .logtable .call { font-weight:600; color:var(--ink); }
  .logtable .thumb { width:44px; height:33px; object-fit:cover; border-radius:4px;
    background:#05090a; display:block; }
  .dirbadge { font-family:var(--mono); font-size:10px; letter-spacing:.05em;
    padding:2px 7px; border-radius:20px; border:1px solid var(--line); color:var(--muted); }
  .dirbadge.tx { color:var(--tx);
    border-color:color-mix(in srgb,var(--tx) 40%,var(--line)); }
  .dirbadge.rx { color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 40%,var(--line)); }
  /* compose */
  .cwrap { display:grid; grid-template-columns:minmax(0,1fr) 260px; gap:18px; }
  @media (max-width:640px) { .cwrap { grid-template-columns:1fr; } }
  .ccol { padding:14px; }
  .cshot { position:relative; aspect-ratio:4/3; max-height:min(58vh,460px);
    background:#05090a; border:1px solid var(--line); border-radius:10px;
    overflow:hidden; display:grid; place-items:center; }
  .cshot #cPreview, .cshot video { width:100%; height:100%; object-fit:contain; display:block; }
  /* Crop/adjust: the source image is positioned + transformed by JS, so it
     must escape the reset's max-width and the object-fit rule above. */
  #cCropImg { position:absolute; top:0; left:0; max-width:none; display:block;
    transform-origin:0 0; touch-action:none; user-select:none;
    -webkit-user-drag:none; cursor:grab; }
  .cshot.grabbing #cCropImg { cursor:grabbing; }
  .czoom { flex:1 1 90px; align-self:center; accent-color:var(--accent); }
  .cshot .ph { color:var(--muted); font-family:var(--mono); font-size:12px;
    padding:20px; text-align:center; }
  .crow { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  .chd { font-family:var(--mono); font-size:11px; text-transform:uppercase;
    letter-spacing:.06em; color:var(--muted); margin:16px 0 8px; }
  .ctpl { display:flex; gap:8px; overflow-x:auto; padding-bottom:4px; }
  .ctpl button { flex:none; padding:8px 12px; border-radius:8px; border:1px solid var(--line);
    background:transparent; color:var(--muted); font-size:12px; white-space:nowrap; cursor:pointer; }
  .ctpl button[aria-pressed="true"] { color:var(--accent);
    border-color:color-mix(in srgb,var(--accent) 45%,var(--line));
    background:color-mix(in srgb,var(--accent) 10%,transparent); }
  .field { margin-bottom:12px; }
  .field label { display:block; font-family:var(--mono); font-size:11px; color:var(--muted);
    margin-bottom:5px; letter-spacing:.04em; }
  .field input, #composeView select { width:100%; background:#05090a; color:var(--ink);
    border:1px solid var(--line); border-radius:8px; padding:9px; font:inherit; }
  .field2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .chint { color:var(--muted); font-size:11px; margin-top:10px; line-height:1.5; }
  .chint.err { color:var(--tx); }
  #box { position:fixed; inset:0; background:rgba(3,6,7,.9); display:none;
    align-items:center; justify-content:center; padding:24px; z-index:20; }
  #box.show { display:flex; }
  #box img { max-width:100%; max-height:86vh; border-radius:10px;
    border:1px solid var(--line); }
  #box .cap { position:fixed; bottom:16px; left:0; right:0; text-align:center;
    font-family:var(--mono); font-size:12px; color:var(--muted); }
  #box .boxinner { display:flex; flex-direction:column; align-items:center; gap:14px; }
  /* control plane (TX) */
  .txchip { display:none; align-items:center; gap:8px; }
  .txchip.on { display:inline-flex; }
  .txchip .muted { color:var(--muted); font-family:var(--mono); font-size:12px; }
  .btn { font:inherit; color:var(--ink); background:var(--panel);
    border:1px solid var(--line); border-radius:8px; padding:6px 13px; cursor:pointer; }
  .btn:hover { border-color:var(--muted); }
  .btn.tx { color:var(--tx); background:color-mix(in srgb,var(--tx) 16%,var(--panel));
    border-color:color-mix(in srgb,var(--tx) 45%,var(--line)); }
  .btn.tx:hover { border-color:var(--tx); }
  .onair { display:inline-flex; align-items:center; gap:8px; font-family:var(--mono);
    font-size:12px; text-transform:uppercase; letter-spacing:.09em; color:var(--tx); }
  .onair.ctl { color:var(--accent); }
  .onair .d { width:9px; height:9px; border-radius:50%; background:var(--tx);
    box-shadow:0 0 10px var(--tx); animation:pulse 1s ease-in-out infinite; }
  .onair.ctl .d { background:var(--accent); box-shadow:none; animation:none; }
  .scrim { position:fixed; inset:0; background:rgba(3,6,7,.9); display:none;
    align-items:center; justify-content:center; padding:24px; z-index:30; }
  .scrim.show { display:flex; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:14px;
    padding:22px; max-width:420px; width:100%; }
  .panel h3 { margin:0 0 6px; }
  .panel .sub { color:var(--muted); font-size:13px; }
  .panel select { width:100%; margin-top:14px; background:#05090a; color:var(--ink);
    border:1px solid var(--line); border-radius:8px; padding:9px; font:inherit; }
  .panel .actions { display:flex; gap:10px; margin-top:20px; justify-content:flex-end; }
  /* full-window ON AIR takeover while transmitting */
  #onair { position:fixed; inset:0; z-index:40; display:none;
    flex-direction:column; align-items:center; justify-content:center; gap:22px;
    text-align:center; padding:28px;
    background:radial-gradient(120% 90% at 50% 30%,
      color-mix(in srgb,var(--tx) 22%,#0a0405) 0%, #0a0405 70%); }
  #onair.show { display:flex; }
  #onair .lamp { width:120px; height:120px; border-radius:50%; background:var(--tx);
    box-shadow:0 0 60px var(--tx), 0 0 120px color-mix(in srgb,var(--tx) 60%,transparent);
    animation:onairpulse 1.05s ease-in-out infinite; }
  @keyframes onairpulse { 0%,100%{transform:scale(1);opacity:1}
    50%{transform:scale(.82);opacity:.55} }
  #onair .big { font-family:var(--mono); font-size:44px; font-weight:700;
    letter-spacing:.18em; color:var(--tx); text-transform:uppercase; }
  #onair .what { font-family:var(--mono); font-size:14px; color:var(--ink); }
  #onair .hb { font-family:var(--mono); font-size:12px; color:var(--muted);
    display:inline-flex; align-items:center; gap:8px; }
  #onair .hb .d { width:8px; height:8px; border-radius:50%; background:var(--accent);
    box-shadow:0 0 8px var(--accent); transition:opacity .15s; }
  #onair .hb.stale .d { background:var(--amber); box-shadow:0 0 8px var(--amber); }
  #onair .warn { max-width:420px; font-size:12px; color:var(--muted); line-height:1.5; }
  #onair .abort { font-size:17px; padding:14px 40px; border-radius:12px;
    color:#fff; background:var(--tx); border:1px solid var(--tx); cursor:pointer;
    font-weight:600; letter-spacing:.02em; }
  #onair .abort:hover { filter:brightness(1.12); }
</style>
</head>
<body>
<header>
  <b>Open-SSTV</b>
  <span class="spacer"></span>
  <span class="txchip" id="txchip"></span>
  <span class="statuspill" id="status" data-state="offline" title="live link status">
    <span class="d"></span><span class="t">Connecting…</span></span>
  <span id="count"></span>
  <button id="refresh">Refresh</button>
</header>
<nav class="tabs" role="tablist">
  <button role="tab" aria-selected="true" data-view="gallery">Gallery</button>
  <button role="tab" aria-selected="false" data-view="logbook">Logbook</button>
  <button role="tab" aria-selected="false" data-view="compose">Compose</button>
</nav>
<main>
  <section id="galleryView">
    <section class="live" id="live">
      <div class="screen"><img id="liveImg" alt="in-progress decode" style="display:none" />
        <span class="ph" id="livePh">waiting for lines…</span></div>
      <div class="info">
        <div class="eyebrow"><span class="pulse"></span> Receiving</div>
        <h2 id="liveMode">SSTV</h2>
        <div class="bar"><span id="liveBar"></span></div>
        <div class="pct" id="livePct">0%</div>
      </div>
    </section>
    <div class="grid" id="grid"></div>
  </section>
  <section id="logbookView" hidden>
    <div class="logwrap">
      <table class="logtable">
        <thead><tr>
          <th></th><th>Time (UTC)</th><th>Call</th><th></th><th>Mode</th>
          <th>Freq</th><th>RST S/R</th><th>Name</th>
        </tr></thead>
        <tbody id="logBody"></tbody>
      </table>
    </div>
    <div class="empty" id="logEmpty" hidden>No logged QSOs yet.</div>
  </section>
  <section id="composeView" hidden>
    <div class="cwrap">
      <div class="card ccol">
        <div class="cshot" id="cShot">
          <video id="cVideo" autoplay playsinline muted style="display:none"></video>
          <img id="cCropImg" alt="adjust framing" style="display:none" />
          <img id="cPreview" alt="composed preview" style="display:none" />
          <span class="ph" id="cPh">Take or upload a photo to compose a card</span>
        </div>
        <div class="crow">
          <button class="btn" id="cCamera">📷 Camera</button>
          <button class="btn" id="cCapture" style="display:none">◉ Capture</button>
          <button class="btn" id="cUpload">⬆ Upload</button>
          <button class="btn" id="cAdjust" style="display:none">✂ Adjust</button>
          <input type="file" id="cFile" accept="image/*" style="display:none" />
          <input type="file" id="cCapFile" accept="image/*" capture="environment"
            style="display:none" />
        </div>
        <div class="crow" id="cCropTools" style="display:none">
          <input type="range" id="cZoom" class="czoom" min="1" max="4" step="0.01" value="1"
            aria-label="Zoom" />
          <button class="btn tx" id="cCropUse">✓ Use photo</button>
          <button class="btn" id="cCropCancel">↺ Retake</button>
        </div>
        <div class="chd">Template — composited by the station</div>
        <div class="ctpl" id="cTpl"></div>
      </div>
      <div class="card ccol">
        <div class="field"><label>To call {tocall}</label>
          <input id="cTocall" maxlength="12" placeholder="K1ABC" /></div>
        <div class="field2">
          <div class="field"><label>RST {rst}</label><input id="cRst" maxlength="5" value="595" /></div>
          <div class="field"><label>Name {name}</label><input id="cName" maxlength="16" placeholder="Sam" /></div>
        </div>
        <div class="field"><label>Note {note}</label>
          <input id="cNote" maxlength="32" placeholder="73!" /></div>
        <div class="field"><label>SSTV mode</label><select id="cMode"></select></div>
        <button class="btn tx" id="cTransmit"
          style="width:100%; justify-content:center; margin-top:4px">📡 Transmit</button>
        <div class="chint" id="cHint">The station renders the exact bytes it sends.</div>
      </div>
    </div>
  </section>
</main>
<div id="box">
  <div class="boxinner">
    <img id="boxImg" alt="" />
    <button class="btn tx" id="txSend" style="display:none">📡 Transmit this image</button>
  </div>
  <div class="cap" id="boxCap"></div>
</div>
<div class="scrim" id="cfScrim">
  <div class="panel">
    <h3>Transmit image?</h3>
    <div class="sub" id="cfName"></div>
    <select id="cfMode" aria-label="SSTV mode"></select>
    <div class="sub" style="margin-top:12px">This keys the station's transmitter.</div>
    <div class="actions">
      <button class="btn" id="cfCancel">Cancel</button>
      <button class="btn tx" id="cfConfirm">Confirm transmit</button>
    </div>
  </div>
</div>
<div id="onair">
  <div class="lamp"></div>
  <div class="big">On Air</div>
  <div class="what" id="onairWhat">Transmitting…</div>
  <div class="hb" id="onairHb"><span class="d"></span> Link alive</div>
  <div class="warn">Keep this page open — if the connection drops, the station
    unkeys automatically (dead-man's-switch).</div>
  <button class="abort" id="onairAbort">■ Abort transmission</button>
</div>
<script>
  const token = new URLSearchParams(location.search).get("token") || "";
  const q = (p) => p + (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
  const $ = (id) => document.getElementById(id);
  const grid = $("grid"), countEl = $("count"), live = $("live");
  let activeView = "gallery", galleryCount = "", logCount = "";

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function fmtFreq(hz) { return hz ? (hz / 1e6).toFixed(3) + " MHz" : "—"; }
  function fmtUTC(iso) {
    const d = new Date(iso), p = (n) => String(n).padStart(2, "0");
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
      " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
  }

  function card(item, fresh) {
    const el = document.createElement("div");
    el.className = "card" + (fresh ? " fresh" : "");
    // Escape every server-provided value — a filename / callsign on disk is
    // attacker-controllable (gallery_extra_dirs may watch a shared folder).
    const name = esc(item.name);
    const badges = [`<span class="badge">${esc(item.mode)}</span>`];
    if (item.callsign) {
      const cls = item.direction === "TX" ? "badge call tx" : "badge call";
      badges.push(`<span class="${cls}">${esc(item.callsign)}</span>`);
    }
    el.innerHTML =
      `<img class="thumb" loading="lazy" alt="${name}" ` +
        `src="${q("/api/thumb/" + item.id)}" />` +
      `<div class="meta"><div class="name" title="${name}">${name}</div>` +
      `<div class="row">${badges.join("")}</div></div>`;
    el.addEventListener("click", () => open_(item));
    return el;
  }

  let currentImage = null;  // {id, name, mode} of the open lightbox image
  function openImage(id, caption, meta) {
    $("boxImg").src = q("/api/image/" + id);
    $("boxCap").textContent = caption || "";
    currentImage = meta || null;
    $("box").classList.add("show");
    renderTx();
  }
  function open_(item) {
    const when = new Date(item.timestamp).toLocaleString();
    openImage(item.id, `${item.name} · ${item.mode} · ${when} · ${fmtBytes(item.size_bytes)}`,
      { id: item.id, name: item.name, mode: item.mode });
  }
  // Close only on the backdrop, so the Transmit button stays clickable.
  $("box").addEventListener("click", (e) => {
    if (e.target === $("box")) { $("box").classList.remove("show"); currentImage = null; }
  });

  function errMsg(e) {
    return e.message === "unauthorized"
      ? "Not authorized — check the token in the URL."
      : "Could not reach the station — is Open-SSTV running?";
  }

  async function load() {
    if (activeView === "gallery") countEl.textContent = "loading…";
    try {
      const res = await fetch(q("/api/gallery"));
      if (res.status === 401) throw new Error("unauthorized");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const items = await res.json();
      galleryCount = items.length + (items.length === 1 ? " image" : " images");
      if (activeView === "gallery") countEl.textContent = galleryCount;
      grid.innerHTML = "";
      if (!items.length) {
        grid.innerHTML = '<div class="empty">No images in the gallery yet.</div>';
        return;
      }
      for (const it of items) grid.appendChild(card(it, false));
    } catch (e) {
      if (activeView === "gallery") countEl.textContent = "";
      grid.innerHTML = '<div class="err">' + errMsg(e) + "</div>";
    }
  }

  async function loadLogbook() {
    const body = $("logBody"), empty = $("logEmpty");
    if (activeView === "logbook") countEl.textContent = "loading…";
    try {
      const res = await fetch(q("/api/logbook"));
      if (res.status === 401) throw new Error("unauthorized");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const rows = await res.json();
      logCount = rows.length + (rows.length === 1 ? " QSO" : " QSOs");
      if (activeView === "logbook") countEl.textContent = logCount;
      body.innerHTML = "";
      empty.hidden = rows.length > 0;
      empty.textContent = "No logged QSOs yet.";
      for (const r of rows) {
        const tr = document.createElement("tr");
        const dcls = r.direction === "TX" ? "dirbadge tx"
          : (r.direction === "RX" ? "dirbadge rx" : "dirbadge");
        const thumb = r.image_id
          ? `<img class="thumb" loading="lazy" alt="" src="${q("/api/thumb/" + r.image_id)}" />`
          : "";
        const rst = [r.rst_sent, r.rst_received].filter(Boolean).join(" / ") || "—";
        tr.innerHTML =
          `<td>${thumb}</td><td>${fmtUTC(r.time)}</td>` +
          `<td class="call">${esc(r.callsign) || "—"}</td>` +
          `<td><span class="${dcls}">${esc(r.direction)}</span></td>` +
          `<td>${esc(r.mode) || "—"}</td><td>${fmtFreq(r.frequency_hz)}</td>` +
          `<td>${esc(rst)}</td><td>${esc(r.name) || "—"}</td>`;
        if (r.image_id) {
          tr.dataset.img = r.image_id;
          tr.addEventListener("click", () => openImage(
            r.image_id, esc(r.callsign) + " · " + esc(r.mode),
            { id: r.image_id, name: r.callsign || r.image_id, mode: r.mode }));
        }
        body.appendChild(tr);
      }
    } catch (e) {
      if (activeView === "logbook") countEl.textContent = "";
      body.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = '<span class="err">' + errMsg(e) + "</span>";
    }
  }

  function showView(name) {
    if (activeView === "compose" && name !== "compose") {
      stopCamera();
      // Don't leave a live camera / crop stage frozen on screen for next time.
      if (cStage === "camera" || cStage === "crop") {
        composeStage(cPhoto ? "preview" : "empty");
      }
    }
    activeView = name;
    $("galleryView").hidden = name !== "gallery";
    $("logbookView").hidden = name !== "logbook";
    $("composeView").hidden = name !== "compose";
    document.querySelectorAll(".tabs button").forEach(
      b => b.setAttribute("aria-selected", b.dataset.view === name ? "true" : "false"));
    countEl.textContent =
      name === "gallery" ? galleryCount : (name === "logbook" ? logCount : "");
    if (name === "logbook") loadLogbook();
    else if (name === "compose") initCompose();
  }
  document.querySelectorAll(".tabs button").forEach(
    b => b.addEventListener("click", () => showView(b.dataset.view)));
  $("refresh").addEventListener("click",
    () => (activeView === "gallery" ? load() : loadLogbook()));

  /* ---- live view plane: Server-Sent Events ---- */
  let seq = 0;
  const statusEl = $("status");
  function setStatus(state, label) {
    statusEl.dataset.state = state;
    statusEl.querySelector(".t").textContent = label;
  }
  function showLive(mode) {
    setStatus("receiving", "Receiving");
    $("liveMode").textContent = mode || "SSTV";
    $("liveBar").style.width = "0%"; $("livePct").textContent = "0%";
    $("liveImg").style.display = "none"; $("livePh").style.display = "";
    live.classList.add("show");
  }
  function progressLive(ev) {
    if (!live.classList.contains("show")) showLive(ev.mode);
    $("liveBar").style.width = (ev.pct || 0) + "%";
    $("livePct").textContent = (ev.pct || 0) + "%  ·  line " + ev.lines + "/" + ev.total;
    const img = $("liveImg");
    img.src = q("/api/rx/preview") + "&t=" + (++seq);
    img.style.display = ""; $("livePh").style.display = "none";
  }
  function hideLive() { live.classList.remove("show"); setStatus("live", "Live"); }

  function connect() {
    const es = new EventSource(q("/api/events"));
    es.onopen = () => setStatus("live", "Live");
    // EventSource auto-reconnects; reflect the gap instead of hiding it.
    es.onerror = () => setStatus("offline", "Reconnecting…");
    es.onmessage = (m) => {
      let ev; try { ev = JSON.parse(m.data); } catch { return; }
      if (ev.type === "rx.started") showLive(ev.mode);
      else if (ev.type === "rx.progress") progressLive(ev);
      else if (ev.type === "rx.complete") hideLive();
      else if (ev.type === "gallery.new") load();
      else if (ev.type === "tx.state") onTxState(ev);
    };
  }

  /* ---- control plane: remote transmit ---- */
  const MODES = [
    { v: "scottie_s1", n: "Scottie S1" }, { v: "scottie_s2", n: "Scottie S2" },
    { v: "martin_m1", n: "Martin M1" }, { v: "martin_m2", n: "Martin M2" },
    { v: "pd_120", n: "PD-120" }, { v: "pd_180", n: "PD-180" },
    { v: "robot_36", n: "Robot 36" },
  ];
  let clientId = sessionStorage.getItem("sstv_client");
  if (!clientId) {
    clientId = "c-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    sessionStorage.setItem("sstv_client", clientId);
  }
  let txInfo = { state: "idle", lease_held: false, tx_enabled: false };
  let iHold = false, hbTimer = null, cfImage = null, txSendingLabel = "";

  async function txPost(action, extra) {
    try {
      const res = await fetch(q("/api/tx/" + action), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ client: clientId }, extra || {})),
      });
      let body = {}; try { body = await res.json(); } catch {}
      return { ok: res.ok, body };
    } catch { return { ok: false, body: {} }; }
  }
  function startHb() {
    if (hbTimer) return;
    // 1.5 s keeps the lease alive (< 15 s) and satisfies the dead-man's-
    // switch during TX (< 2.5 s). Stop heartbeating → the station unkeys.
    hbTimer = setInterval(async () => {
      const r = await txPost("heartbeat");
      if (!r.ok) { iHold = false; stopHb(); renderTx(); return; }
      // Flash the on-air heartbeat indicator so the operator sees the link
      // is alive (and the dead-man's-switch is being fed).
      const d = $("onairHb").querySelector(".d");
      d.style.opacity = "0.25"; setTimeout(() => { d.style.opacity = "1"; }, 160);
    }, 1500);
  }
  function stopHb() { if (hbTimer) { clearInterval(hbTimer); hbTimer = null; } }

  async function takeControl() {
    const r = await txPost("lease");
    if (r.ok) { iHold = true; startHb(); }
    renderTx();
  }
  async function releaseControl() { await txPost("release"); iHold = false; stopHb(); renderTx(); }
  async function abortTx() { await txPost("abort"); }

  function askTransmit() {
    if (!iHold || txInfo.state !== "idle" || !currentImage) return;
    cfImage = currentImage;
    $("cfMode").innerHTML = MODES.map(m =>
      `<option value="${m.v}"${m.v === cfImage.mode ? " selected" : ""}>${m.n}</option>`
    ).join("");
    $("cfName").textContent = cfImage.name || "";
    $("cfScrim").classList.add("show");
  }
  $("txSend").addEventListener("click", askTransmit);
  $("cfCancel").addEventListener("click", () => $("cfScrim").classList.remove("show"));
  $("cfConfirm").addEventListener("click", async () => {
    $("cfScrim").classList.remove("show");
    const modeName = $("cfMode").selectedOptions[0] ? $("cfMode").selectedOptions[0].text : "";
    txSendingLabel = (cfImage && cfImage.name ? cfImage.name + " · " : "") + modeName;
    const req = await txPost("request", { image_id: cfImage.id, mode: $("cfMode").value });
    if (!req.ok || !req.body.token) { renderTx(); return; }
    await txPost("confirm", { token: req.body.token });  // tx.state SSE flips to on-air
  });
  $("onairAbort").addEventListener("click", abortTx);

  function mkbtn(text, cls, fn) {
    const b = document.createElement("button");
    b.className = cls; b.textContent = text; b.addEventListener("click", fn);
    return b;
  }
  function renderTx() {
    const chip = $("txchip");
    chip.replaceChildren();
    if (!txInfo.tx_enabled) { chip.className = "txchip"; }
    else {
      chip.className = "txchip on";
      if (txInfo.state === "transmitting" && iHold) {
        chip.insertAdjacentHTML("beforeend",
          '<span class="onair"><span class="d"></span> On air</span>');
        chip.appendChild(mkbtn("Abort", "btn tx", abortTx));
      } else if (iHold) {
        chip.insertAdjacentHTML("beforeend",
          '<span class="onair ctl"><span class="d"></span> You have control</span>');
        chip.appendChild(mkbtn("Release", "btn", releaseControl));
      } else if (txInfo.lease_held) {
        chip.insertAdjacentHTML("beforeend",
          '<span class="muted">in use by another station</span>');
      } else {
        chip.appendChild(mkbtn("Take control", "btn", takeControl));
      }
    }
    $("txSend").style.display =
      (iHold && txInfo.state === "idle" && currentImage) ? "" : "none";
    // Full-window ON AIR takeover while *we* are transmitting.
    const onAir = txInfo.state === "transmitting" && iHold;
    if (onAir) $("onairWhat").textContent = "Transmitting " + (txSendingLabel || "image");
    $("onair").classList.toggle("show", onAir);
  }
  function onTxState(ev) {
    const wasTx = txInfo.state === "transmitting";
    txInfo = { state: ev.state, lease_held: !!ev.lease_held, tx_enabled: !!ev.tx_enabled };
    if (!txInfo.lease_held) { iHold = false; stopHb(); }  // server released/lapsed it
    // TX ended (finished or aborted): clear the stale compose progress hint.
    if (wasTx && ev.state !== "transmitting" && /^(Transmitting|Staging)/.test($("cHint").textContent)) {
      cHint("Preview approximates the on-air image — the station renders the exact bytes.");
    }
    renderTx();
  }

  /* ---- compose (camera / upload → crop → template → transmit) ---- */
  let cPhoto = null, cTemplateId = null, cStream = null, cRenderTimer = null,
      composeInited = false, cStage = "empty";
  // Crop/adjust state: cRaw is the full-resolution source (so re-adjust starts
  // from the original, not an already-cropped copy).  cCover is the scale that
  // makes the image exactly fill the box; cZoom multiplies it; cPanX/Y is the
  // image's top-left offset within the box, in CSS px.
  let cRaw = null, cImgW = 0, cImgH = 0, cCover = 1, cZoom = 1, cPanX = 0, cPanY = 0;
  const cPtrs = new Map();       // active pointers, for drag-pan / pinch-zoom
  let cPinch = null;             // {dist, zoom} captured when a 2nd finger lands
  function cHint(msg, err) {
    const h = $("cHint"); h.textContent = msg; h.classList.toggle("err", !!err);
  }
  // Show exactly the widgets that belong to the current compose stage.
  function composeStage(s) {
    cStage = s;
    $("cVideo").style.display   = s === "camera"  ? "block" : "none";
    $("cCropImg").style.display = s === "crop"    ? "block" : "none";
    $("cPreview").style.display = s === "preview" ? "block" : "none";
    $("cPh").style.display      = s === "empty"   ? "" : "none";
    $("cCamera").style.display  = (s === "empty" || s === "preview") ? "" : "none";
    $("cUpload").style.display  = (s === "empty" || s === "preview") ? "" : "none";
    $("cCapture").style.display = s === "camera"  ? "" : "none";
    $("cAdjust").style.display  = s === "preview" ? "" : "none";
    // Inline display (not the `hidden` attr): `.crow{display:flex}` would
    // otherwise override `[hidden]` and leak the crop tools into other stages.
    $("cCropTools").style.display = s === "crop" ? "flex" : "none";
  }
  function blobToDataUrl(blob) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result); r.onerror = () => rej(r.error);
      r.readAsDataURL(blob);
    });
  }
  function composeTokens() {
    return { tocall: $("cTocall").value, rst: $("cRst").value,
             name: $("cName").value, note: $("cNote").value };
  }
  function initCompose() {
    if (composeInited) return;
    composeInited = true;
    $("cMode").innerHTML = MODES.map(m => `<option value="${m.v}">${m.n}</option>`).join("");
    loadComposeTemplates();
  }
  async function loadComposeTemplates() {
    try {
      const res = await fetch(q("/api/compose/templates"));
      if (!res.ok) { cHint("Could not load templates.", true); return; }
      const tpls = await res.json();
      const strip = $("cTpl"); strip.innerHTML = "";
      tpls.forEach((t, i) => {
        const b = document.createElement("button");
        b.textContent = t.name;
        b.setAttribute("aria-pressed", i === 0 ? "true" : "false");
        if (i === 0) cTemplateId = t.id;
        b.addEventListener("click", () => {
          cTemplateId = t.id;
          strip.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", "false"));
          b.setAttribute("aria-pressed", "true");
          renderComposePreview();
        });
        strip.appendChild(b);
      });
    } catch { cHint("Could not reach the station.", true); }
  }
  function stopCamera() {
    if (cStream) { cStream.getTracks().forEach(t => t.stop()); cStream = null; }
  }
  async function startCamera() {
    // getUserMedia needs a secure context; over plain http on a LAN (the
    // usual phone case) navigator.mediaDevices is undefined.  Detect that
    // synchronously — while we still have the click's user-activation — and
    // fall back to the OS camera via a capture file input, which works over
    // http and gives the native (higher-quality) camera UI on a phone.
    const md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) { $("cCapFile").click(); return; }
    try {
      cStream = await md.getUserMedia(
        { video: { facingMode: "environment" }, audio: false });
      $("cVideo").srcObject = cStream;
      composeStage("camera");
    } catch { cHint("Camera blocked — allow it in the browser, or use Upload.", true); }
  }
  function capture() {
    const v = $("cVideo"); if (!v.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext("2d").drawImage(v, 0, 0);
    enterCrop(c.toDataURL("image/jpeg", 0.9));
  }

  /* --- crop / adjust: pan + pinch + zoom, output a WYSIWYG framed JPEG --- */
  function applyCropTransform() {
    const dw = cImgW * cCover * cZoom, dh = cImgH * cCover * cZoom;
    const img = $("cCropImg");
    img.style.width = dw + "px"; img.style.height = dh + "px";
    img.style.transform = "translate(" + cPanX + "px," + cPanY + "px)";
  }
  function clampPan() {
    const box = $("cShot"), bw = box.clientWidth, bh = box.clientHeight;
    const dw = cImgW * cCover * cZoom, dh = cImgH * cCover * cZoom;
    cPanX = Math.min(0, Math.max(bw - dw, cPanX));   // image must always cover
    cPanY = Math.min(0, Math.max(bh - dh, cPanY));   // the box — no empty gaps
  }
  // Zoom to *nz*, keeping the image point under box-relative (ax,ay) fixed.
  function zoomAround(nz, ax, ay) {
    nz = Math.min(4, Math.max(1, nz));
    const s = cCover * cZoom;
    const ix = (ax - cPanX) / s, iy = (ay - cPanY) / s;   // image coord under anchor
    cZoom = nz;
    const s2 = cCover * cZoom;
    cPanX = ax - ix * s2; cPanY = ay - iy * s2;
    clampPan(); applyCropTransform();
    $("cZoom").value = String(cZoom);
  }
  function enterCrop(dataUrl) {
    stopCamera();
    cRaw = dataUrl;
    const img = $("cCropImg");
    const init = () => {
      cImgW = img.naturalWidth; cImgH = img.naturalHeight;
      if (!cImgW || !cImgH) return;
      const box = $("cShot"), bw = box.clientWidth, bh = box.clientHeight;
      cCover = Math.max(bw / cImgW, bh / cImgH);   // fill the box at zoom 1
      cZoom = 1;
      const dw = cImgW * cCover, dh = cImgH * cCover;
      cPanX = (bw - dw) / 2; cPanY = (bh - dh) / 2;   // centred
      $("cZoom").value = "1";
      applyCropTransform();
      composeStage("crop");
      cHint("Drag to reposition, pinch or use the slider to zoom, then Use photo.");
    };
    img.onload = init;
    img.src = dataUrl;
    if (img.complete && img.naturalWidth) init();   // cached: onload may not fire
  }
  function applyCrop() {
    const box = $("cShot"), bw = box.clientWidth, bh = box.clientHeight;
    const s = cCover * cZoom;
    // Box → source-image rectangle, in natural pixels.
    const sx = -cPanX / s, sy = -cPanY / s, sw = bw / s, sh = bh / s;
    const outW = 800, outH = Math.round(800 * bh / bw);
    const c = document.createElement("canvas");
    c.width = outW; c.height = outH;
    c.getContext("2d").drawImage($("cCropImg"), sx, sy, sw, sh, 0, 0, outW, outH);
    setPhoto(c.toDataURL("image/jpeg", 0.85));
  }
  function setPhoto(dataUrl) {
    cPhoto = (dataUrl.split(",")[1]) || null;   // strip the data:...;base64, prefix
    $("cPreview").src = dataUrl;                 // instant feedback until render
    composeStage("preview");
    renderComposePreview();
  }
  function scheduleRender() {
    clearTimeout(cRenderTimer); cRenderTimer = setTimeout(renderComposePreview, 400);
  }
  async function renderComposePreview() {
    if (!cPhoto || !cTemplateId) return;
    try {
      const res = await fetch(q("/api/compose/render"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo: cPhoto, template_id: cTemplateId,
          mode: $("cMode").value, tokens: composeTokens() }),
      });
      if (!res.ok) { cHint("Preview failed (HTTP " + res.status + ").", true); return; }
      const url = await blobToDataUrl(await res.blob());
      $("cPreview").src = url;   // composeStage("preview") owns visibility
      cHint("Preview approximates the on-air image — the station renders the exact bytes.");
    } catch { cHint("Could not reach the station.", true); }
  }
  async function composeTransmit() {
    if (!cPhoto || !cTemplateId) { cHint("Take or upload a photo first.", true); return; }
    if (!txInfo.tx_enabled) {
      cHint("Remote transmit is off in the station's settings.", true); return;
    }
    if (!iHold) { await takeControl(); if (!iHold) {
      cHint("Couldn't take control — another station may hold it.", true); return; } }
    cHint("Staging…");
    let stage;
    try {
      stage = await fetch(q("/api/compose/stage"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo: cPhoto, template_id: cTemplateId,
          mode: $("cMode").value, tokens: composeTokens() }),
      });
    } catch { cHint("Could not reach the station.", true); return; }
    if (!stage.ok) { cHint("Stage failed (HTTP " + stage.status + ").", true); return; }
    const { image_id } = await stage.json();
    const req = await txPost("request", { image_id, mode: $("cMode").value });
    if (!req.ok || !req.body.token) { cHint("Transmit request refused.", true); return; }
    const mn = $("cMode").selectedOptions[0] ? $("cMode").selectedOptions[0].text : "";
    txSendingLabel = "composed image · " + mn;
    await txPost("confirm", { token: req.body.token });   // → ON AIR takeover
    cHint("Transmitting…");
  }
  $("cCamera").addEventListener("click", startCamera);
  $("cCapture").addEventListener("click", capture);
  $("cUpload").addEventListener("click", () => $("cFile").click());
  function fileToCrop(input) {
    const f = input.files && input.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = () => enterCrop(r.result);
    r.readAsDataURL(f);
    input.value = "";   // allow re-selecting the same file / re-shooting
  }
  $("cFile").addEventListener("change", (e) => fileToCrop(e.target));
  $("cCapFile").addEventListener("change", (e) => fileToCrop(e.target));
  $("cAdjust").addEventListener("click", () => { if (cRaw) enterCrop(cRaw); });
  $("cCropUse").addEventListener("click", applyCrop);
  $("cCropCancel").addEventListener("click",
    () => composeStage(cPhoto ? "preview" : "empty"));
  $("cZoom").addEventListener("input", () => {
    const box = $("cShot");
    zoomAround(parseFloat($("cZoom").value), box.clientWidth / 2, box.clientHeight / 2);
  });
  // Drag to pan, two fingers to pinch-zoom.  Pointer events cover mouse + touch.
  const cbox = $("cShot");
  cbox.addEventListener("pointerdown", (e) => {
    if (cStage !== "crop") return;
    cbox.setPointerCapture(e.pointerId);
    cPtrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    cbox.classList.add("grabbing");
    if (cPtrs.size === 2) {
      const p = [...cPtrs.values()];
      cPinch = { dist: Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y), zoom: cZoom };
    }
  });
  cbox.addEventListener("pointermove", (e) => {
    const prev = cPtrs.get(e.pointerId); if (!prev) return;
    const nx = e.clientX, ny = e.clientY;
    if (cPtrs.size >= 2 && cPinch) {
      cPtrs.set(e.pointerId, { x: nx, y: ny });
      const p = [...cPtrs.values()];
      const dist = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      const r = cbox.getBoundingClientRect();
      const mx = (p[0].x + p[1].x) / 2 - r.left, my = (p[0].y + p[1].y) / 2 - r.top;
      zoomAround(cPinch.zoom * (dist / cPinch.dist), mx, my);
    } else {
      cPanX += nx - prev.x; cPanY += ny - prev.y;
      cPtrs.set(e.pointerId, { x: nx, y: ny });
      clampPan(); applyCropTransform();
    }
  });
  const cptrUp = (e) => {
    cPtrs.delete(e.pointerId);
    if (cPtrs.size < 2) cPinch = null;
    if (cPtrs.size === 0) cbox.classList.remove("grabbing");
  };
  cbox.addEventListener("pointerup", cptrUp);
  cbox.addEventListener("pointercancel", cptrUp);
  ["cTocall", "cRst", "cName", "cNote"].forEach(
    id => $(id).addEventListener("input", scheduleRender));
  $("cMode").addEventListener("change", renderComposePreview);
  $("cTransmit").addEventListener("click", composeTransmit);

  load();
  connect();
</script>
</body>
</html>
"""


def render_page() -> str:
    """Return the self-contained read-only gallery viewer HTML."""
    return _PAGE


__all__ = ["render_page"]
