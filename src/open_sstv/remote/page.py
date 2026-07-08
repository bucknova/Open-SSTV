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
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--muted);
    transition:background .3s, box-shadow .3s; }
  header .dot.live { background:var(--accent); box-shadow:0 0 10px var(--accent); }
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
  <span class="dot" id="conn" title="live link"></span>
  <b>Open-SSTV</b><span class="sub">remote gallery · read-only</span>
  <span class="spacer"></span>
  <span id="count"></span>
  <button id="refresh">Refresh</button>
</header>
<main>
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
</main>
<div id="box"><img id="boxImg" alt="" /><div class="cap" id="boxCap"></div></div>
<script>
  const token = new URLSearchParams(location.search).get("token") || "";
  const q = (p) => p + (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
  const $ = (id) => document.getElementById(id);
  const grid = $("grid"), countEl = $("count"), live = $("live");

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
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

  function open_(item) {
    $("boxImg").src = q("/api/image/" + item.id);
    const when = new Date(item.timestamp).toLocaleString();
    $("boxCap").textContent =
      `${item.name} · ${item.mode} · ${when} · ${fmtBytes(item.size_bytes)}`;
    $("box").classList.add("show");
  }
  $("box").addEventListener("click", () => $("box").classList.remove("show"));

  async function load() {
    countEl.textContent = "loading…";
    try {
      const res = await fetch(q("/api/gallery"));
      if (res.status === 401) throw new Error("unauthorized");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const items = await res.json();
      countEl.textContent = items.length + (items.length === 1 ? " image" : " images");
      grid.innerHTML = "";
      if (!items.length) {
        grid.innerHTML = '<div class="empty">No images in the gallery yet.</div>';
        return;
      }
      for (const it of items) grid.appendChild(card(it, false));
    } catch (e) {
      countEl.textContent = "";
      const msg = e.message === "unauthorized"
        ? "Not authorized — check the token in the URL."
        : "Could not reach the station — is Open-SSTV running?";
      grid.innerHTML = '<div class="err">' + msg + "</div>";
    }
  }
  $("refresh").addEventListener("click", load);

  /* ---- live view plane: Server-Sent Events ---- */
  let seq = 0;
  function showLive(mode) {
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
  function hideLive() { live.classList.remove("show"); }

  function connect() {
    const es = new EventSource(q("/api/events"));
    es.onopen = () => $("conn").classList.add("live");
    es.onerror = () => $("conn").classList.remove("live");  // browser auto-reconnects
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
