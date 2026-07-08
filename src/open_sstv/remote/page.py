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
  #box { position:fixed; inset:0; background:rgba(3,6,7,.9); display:none;
    align-items:center; justify-content:center; padding:24px; z-index:20; }
  #box.show { display:flex; }
  #box img { max-width:100%; max-height:86vh; border-radius:10px;
    border:1px solid var(--line); }
  #box .cap { position:fixed; bottom:16px; left:0; right:0; text-align:center;
    font-family:var(--mono); font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<header>
  <b>Open-SSTV</b><span class="sub">remote gallery · read-only</span>
  <span class="spacer"></span>
  <span class="statuspill" id="status" data-state="offline" title="live link status">
    <span class="d"></span><span class="t">Connecting…</span></span>
  <span id="count"></span>
  <button id="refresh">Refresh</button>
</header>
<nav class="tabs" role="tablist">
  <button role="tab" aria-selected="true" data-view="gallery">Gallery</button>
  <button role="tab" aria-selected="false" data-view="logbook">Logbook</button>
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
</main>
<div id="box"><img id="boxImg" alt="" /><div class="cap" id="boxCap"></div></div>
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
    const badges = [`<span class="badge">${item.mode}</span>`];
    if (item.callsign) {
      const cls = item.direction === "TX" ? "badge call tx" : "badge call";
      badges.push(`<span class="${cls}">${item.callsign}</span>`);
    }
    el.innerHTML =
      `<img class="thumb" loading="lazy" alt="${item.name}" ` +
        `src="${q("/api/thumb/" + item.id)}" />` +
      `<div class="meta"><div class="name" title="${item.name}">${item.name}</div>` +
      `<div class="row">${badges.join("")}</div></div>`;
    el.addEventListener("click", () => open_(item));
    return el;
  }

  function openImage(id, caption) {
    $("boxImg").src = q("/api/image/" + id);
    $("boxCap").textContent = caption || "";
    $("box").classList.add("show");
  }
  function open_(item) {
    const when = new Date(item.timestamp).toLocaleString();
    openImage(item.id, `${item.name} · ${item.mode} · ${when} · ${fmtBytes(item.size_bytes)}`);
  }
  $("box").addEventListener("click", () => $("box").classList.remove("show"));

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
          tr.addEventListener("click",
            () => openImage(r.image_id, esc(r.callsign) + " · " + esc(r.mode)));
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
    activeView = name;
    $("galleryView").hidden = name !== "gallery";
    $("logbookView").hidden = name !== "logbook";
    document.querySelectorAll(".tabs button").forEach(
      b => b.setAttribute("aria-selected", b.dataset.view === name ? "true" : "false"));
    countEl.textContent = name === "gallery" ? galleryCount : logCount;
    if (name === "logbook") loadLogbook();
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
    };
  }

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
