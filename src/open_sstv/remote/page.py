# SPDX-License-Identifier: GPL-3.0-or-later
"""The read-only gallery viewer page served at ``/``.

A single self-contained HTML document (inline CSS + JS, no external
resources) — the Phase 1 front end.  It reads the dev token from its own
URL query string and uses it for every API call, so the one URL the app
logs (``http://host:port/?token=…``) is all the operator needs to open.

Kept as a Python string constant rather than a bundled asset so Phase 1
adds nothing to the PyInstaller data-collection surface; when the view
plane grows in Phase 2 it can move to a proper asset.  The palette echoes
the ``design/remote/mockup.html`` "remote head unit" look, trimmed to the
gallery only.
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
    --muted:#7f9aa1; --accent:#34e39a; --tx:#ff5b48;
    --mono:"SFMono-Regular",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:5; display:flex; align-items:center;
    gap:14px; padding:12px 18px; background:rgba(12,20,22,.92);
    backdrop-filter:blur(8px); border-bottom:1px solid var(--line); }
  header .dot { width:9px; height:9px; border-radius:50%; background:var(--accent);
    box-shadow:0 0 10px var(--accent); }
  header b { letter-spacing:.02em; }
  header .sub { color:var(--muted); font-family:var(--mono); font-size:12px; }
  header .spacer { flex:1; }
  button { font:inherit; color:var(--ink); background:var(--panel);
    border:1px solid var(--line); border-radius:8px; padding:7px 13px; cursor:pointer; }
  button:hover { border-color:var(--muted); }
  #count { color:var(--muted); font-family:var(--mono); font-size:13px; }
  main { padding:18px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
    gap:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px;
    overflow:hidden; cursor:pointer; transition:border-color .15s,transform .15s; }
  .card:hover { border-color:var(--accent); transform:translateY(-2px); }
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
  <span class="dot"></span>
  <b>Open-SSTV</b><span class="sub">remote gallery · read-only</span>
  <span class="spacer"></span>
  <span id="count"></span>
  <button id="refresh">Refresh</button>
</header>
<main><div class="grid" id="grid"></div></main>
<div id="box"><img id="boxImg" alt="" /><div class="cap" id="boxCap"></div></div>
<script>
  const token = new URLSearchParams(location.search).get("token") || "";
  const q = (p) => p + (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
  const grid = document.getElementById("grid");
  const countEl = document.getElementById("count");

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function card(item) {
    const el = document.createElement("div");
    el.className = "card";
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
    document.getElementById("boxImg").src = q("/api/image/" + item.id);
    const when = new Date(item.timestamp).toLocaleString();
    document.getElementById("boxCap").textContent =
      `${item.name} · ${item.mode} · ${when} · ${fmtBytes(item.size_bytes)}`;
    document.getElementById("box").classList.add("show");
  }
  document.getElementById("box").addEventListener("click", () =>
    document.getElementById("box").classList.remove("show"));

  async function load() {
    grid.innerHTML = "";
    countEl.textContent = "loading…";
    try {
      const res = await fetch(q("/api/gallery"), { headers: { Authorization: "Bearer " + token } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const items = await res.json();
      countEl.textContent = items.length + (items.length === 1 ? " image" : " images");
      if (!items.length) {
        grid.innerHTML = '<div class="empty">No images in the gallery yet.</div>';
        return;
      }
      for (const it of items) grid.appendChild(card(it));
    } catch (e) {
      countEl.textContent = "";
      grid.innerHTML = '<div class="err">Could not load gallery — ' + e.message +
        '. Check the token in the URL.</div>';
    }
  }
  document.getElementById("refresh").addEventListener("click", load);
  load();
</script>
</body>
</html>
"""


def render_page() -> str:
    """Return the self-contained read-only gallery viewer HTML."""
    return _PAGE


__all__ = ["render_page"]
