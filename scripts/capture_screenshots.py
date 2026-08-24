#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the documentation screenshots in ``docs/screenshots``.

Everything is rendered headlessly against **synthetic** data — invented
contacts, generated testcards, a throwaway logbook in a temp directory — so a
capture can never leak real log entries, images, callsigns, or remote tokens
into a public repository.

Two halves:

* **Desktop** — Qt's offscreen platform plugin plus ``QWidget.grab()``.  No
  display, no window manager, and deliberately *not* the host OS's native
  chrome: Open-SSTV runs on Linux, macOS and Windows, so the docs show the
  cross-platform Qt style rather than any one platform's title bars.
* **Remote** — the real ``RemoteServer`` driven through Chrome's DevTools
  Protocol with mobile emulation, so the phone screenshots are the actual
  served page at an actual phone viewport.  Needs Google Chrome installed;
  skipped with a warning if it isn't.

    python3 scripts/capture_screenshots.py                 # everything
    python3 scripts/capture_screenshots.py --only desktop
    python3 scripts/capture_screenshots.py --out /tmp/shots

The remote half runs the control plane with log-only stub transmit/unkey
callbacks and no CAT backend in the process, so it cannot key a radio.
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "screenshots"

#: Invented contacts.  Never use real log data here.
CONTACTS = [
    ("W1ABC", "Sam",   "Boston, MA",  "FN42", "Martin M1", "599", "579", 14_230_000),
    ("K9XYZ", "Dana",  "Chicago, IL", "EN61", "Scottie 1", "595", "589", 14_230_000),
    ("VE3QRP", "Alex",  "Toronto, ON", "FN03", "PD 120",    "589", "575",  7_171_000),
    ("DL2HAM", "Jonas", "Bremen",      "JO43", "Robot 36",  "559", "569", 14_230_000),
    ("JA1SST", "Hiro",  "Tokyo",       "PM95", "Scottie 2", "579", "579", 21_340_000),
    ("G0PIC", "Ellie", "Bristol",     "IO81", "PD 180",    "599", "599", 14_230_000),
]

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def testcard(path: Path | None, call: str, mode: str, i: int, size=(320, 256)):
    """A synthetic SSTV-ish testcard, so no real received image is ever used."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, (18, 22, 28))
    dr = ImageDraw.Draw(im)
    for y in range(0, size[1], 4):
        hue = (y / size[1] + i / len(CONTACTS)) % 1.0
        r, g, b = (int(v * 255) for v in colorsys.hsv_to_rgb(hue, 0.45, 0.85))
        dr.line([(0, y), (size[0], y)], fill=(r, g, b), width=2)
    dr.rectangle(
        [size[0] * 0.18, size[1] * 0.36, size[0] * 0.82, size[1] * 0.64],
        fill=(10, 12, 18),
    )
    dr.text((size[0] * 0.29, size[1] * 0.43), call, fill="white")
    dr.text((size[0] * 0.29, size[1] * 0.53), mode, fill="#8fe8c0")
    if path is not None:
        im.save(path)
    return im


def seed(data: Path):
    """Build a throwaway config + gallery + logbook under *data*."""
    from open_sstv.config.schema import AppConfig
    from open_sstv.logbook.coordinator import LogbookCoordinator
    from open_sstv.logbook.model import QSO

    imgs = data / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        callsign="W0AEZ", operator_name="Kevin", grid_square="EN34",
        qth="Kansas City, MO",
        images_save_dir=str(imgs),
        logbook_db_path=str(data / "logbook.db"),
        remote_enabled=False, remote_tx_enabled=False,
        check_for_updates=False, first_launch_seen=True,
        default_tx_mode="robot_36",
    )
    coord = LogbookCoordinator(lambda: cfg)
    base = datetime(2026, 8, 22, 14, 5, tzinfo=UTC)
    for i, (call, name, qth, grid, mode, snt, rcv, hz) in enumerate(CONTACTS):
        p = imgs / f"2026-08-22_{140500 + i * 1130}_{mode.lower().replace(' ', '_')}.png"
        testcard(p, call, mode, i)
        coord.store.insert(QSO(
            direction="TX" if i % 2 == 0 else "RX", callsign=call,
            time_utc=base + timedelta(minutes=17 * i), mode=mode,
            frequency_hz=hz, rsv_sent=snt, rsv_received=rcv, name=name,
            qth=qth, grid=grid, comment="", image_path=p,
        ))
    return cfg, coord


# --------------------------------------------------------------------------
# Desktop
# --------------------------------------------------------------------------

def capture_desktop(out: Path) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox, QTableView, QTabWidget

    from open_sstv import __version__
    from open_sstv.core.modes import Mode
    from open_sstv.logbook.model import QSO

    data = Path(tempfile.mkdtemp(prefix="opensstv_shots_"))
    app = QApplication(sys.argv)          # noqa: F841 — must outlive the widgets
    cfg, coord = seed(data)
    n = 0

    def grab(w, name: str, size=None, settle: int = 4) -> None:
        nonlocal n
        if size:
            w.resize(*size)
        w.show()
        for _ in range(settle):
            QApplication.processEvents()
        if w.grab().save(str(out / f"{name}.png")):
            print(f"  ok   {name}.png")
            n += 1
        else:
            print(f"  FAIL {name}.png")
        w.close()
        QApplication.processEvents()

    from open_sstv.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(cfg)
    tabs = dlg.findChildren(QTabWidget)[0]
    for idx, label in enumerate(
        ("general", "audio", "radio", "images", "logging", "remote")
    ):
        tabs.setCurrentIndex(idx)
        grab(dlg, f"settings-{label}", size=(900, 720))

    from open_sstv.ui.logbook_dialog import LogbookDialog
    lb = LogbookDialog(coord)
    lb.refresh()
    lb.resize(1000, 600)
    lb.show()
    for _ in range(10):
        QApplication.processEvents()
    for view in lb.findChildren(QTableView):
        if view.model() and view.model().rowCount():
            view.selectRow(0)
            break
    for _ in range(10):
        QApplication.processEvents()
    grab(lb, "logbook", settle=8)

    from open_sstv.ui.gallery_dialog import GalleryDialog
    g = GalleryDialog(coord, lambda: cfg)
    g.resize(1000, 640)
    g.show()
    for _ in range(30):
        QApplication.processEvents()
    # The detail pane fills from selectedIndexes(), so the index has to go
    # through the selection model — setCurrentIndex alone leaves it empty.
    if g._model.rowCount():
        g._view.selectionModel().select(
            g._model.index(0, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        g._view.setCurrentIndex(g._model.index(0, 0))
        g._on_selection_changed()
    for _ in range(20):
        QApplication.processEvents()
    grab(g, "gallery", settle=20)

    from open_sstv.ui.log_qso_dialog import LogQsoDialog
    q = QSO(direction="TX", callsign="W1ABC", time_utc=datetime.now(UTC),
            mode="Martin M1", frequency_hz=14_230_000, rsv_sent="599",
            rsv_received="579", name="Sam", qth="Boston, MA", grid="FN42",
            comment="great signal")
    grab(LogQsoDialog(q, preview_image=testcard(None, "W1ABC", "Martin M1", 0)),
         "log-qso-dialog", size=(520, 300), settle=6)

    from open_sstv.config.templates import load_templates
    from open_sstv.ui.template_editor_dialog import TemplateEditorDialog
    grab(TemplateEditorDialog(load_templates(), mycall="W0AEZ"),
         "template-editor", size=(980, 620), settle=8)

    from open_sstv.ui.image_editor import ImageEditorDialog
    grab(ImageEditorDialog(testcard(None, "W0AEZ", "Martin M1", 2, size=(640, 496)),
                           Mode.MARTIN_M1, callsign="W0AEZ"),
         "image-editor", size=(1000, 700), settle=8)

    from open_sstv.ui.first_launch_dialog import FirstLaunchDialog
    grab(FirstLaunchDialog(), "first-launch", settle=6)

    mb = QMessageBox()
    mb.setWindowTitle("About Open-SSTV")
    mb.setTextFormat(Qt.RichText)
    mb.setText(
        f"<h3>Open-SSTV v{__version__}</h3>"
        "<p>Open-source SSTV transceiver for amateur radio.</p>"
        "<p>22 modes: Robot 36, Martin M1/M2/M3/M4, Scottie S1/S2/S3/S4/DX, "
        "PD-50/90/120/160/180/240/290, Wraase SC2-120/SC2-180, Pasokon P3/P5/P7.</p>"
        "<p>Created by Kevin &mdash; W0AEZ</p>"
        '<p><a href="https://bucknova.github.io/Open-SSTV/">'
        "bucknova.github.io/Open-SSTV</a></p>"
        "<p>GPL-3.0-or-later</p>")
    grab(mb, "about-dialog", settle=4)

    # Main window last: its teardown can block on worker-thread shutdown, so
    # this process exits via os._exit() below rather than unwinding.
    import open_sstv.ui.main_window as mw_mod
    from open_sstv.radio.base import ManualRig
    mw_mod.load_config = lambda: cfg          # paths the `config` arg doesn't cover
    # config= by KEYWORD.  The first positional parameter is `rig`; passing the
    # config there silently falls back to load_config() — i.e. the real user's
    # settings, and a real remote server bound to 0.0.0.0.
    mw = mw_mod.MainWindow(rig=ManualRig(), config=cfg)
    bar = mw._tx_panel._qso_widget
    bar._tocall.setText("W1ABC")
    bar._rst.setCurrentText("599")
    bar._rst_received.setCurrentText("579")
    bar._name.setText("Sam")
    bar._qth.setText("Boston, MA")
    bar._grid.setText("FN42")
    bar._note.setText("first SSTV contact — great signal")
    mw.resize(1220, 840)
    mw.show()
    for _ in range(30):
        QApplication.processEvents()
    if mw.grab().save(str(out / "main-window.png")):
        print("  ok   main-window.png")
        n += 1
    else:
        print("  FAIL main-window.png")
    return n


# --------------------------------------------------------------------------
# Remote (phone)
# --------------------------------------------------------------------------

def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return None


class _CDP:
    """Minimal Chrome DevTools Protocol client (websocket-client is a dep)."""

    def __init__(self, chrome: str, port: int = 9339) -> None:
        import websocket

        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             f"--user-data-dir={tempfile.mkdtemp(prefix='cdp_')}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Chrome did not start")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
        target = json.load(urllib.request.urlopen(req, timeout=5))
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self._id = 0
        self.send("Page.enable")
        self.send("Runtime.enable")

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def shot(self, url: str, path: Path, w: int, h: int, tab: str | None = None) -> None:
        self.send("Emulation.setDeviceMetricsOverride",
                  width=w, height=h, deviceScaleFactor=2, mobile=True)
        self.send("Page.navigate", url=url)
        time.sleep(2.5)
        if tab:
            self.send("Runtime.evaluate", expression=(
                "(()=>{const t=[...document.querySelectorAll('*')]"
                f".find(e=>e.children.length===0&&e.textContent.trim()==={tab!r});"
                "if(t)t.click()})()"))
            time.sleep(1.8)
        data = self.send("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()


def capture_remote(out: Path) -> int:
    chrome = find_chrome()
    if chrome is None:
        print("  !! Chrome not found — skipping the phone screenshots")
        return 0

    from open_sstv.remote import GalleryService, RemoteServer
    from open_sstv.remote.control import ControlPlane

    data = Path(tempfile.mkdtemp(prefix="opensstv_remote_shots_"))
    cfg, _coord = seed(data)
    cfg = replace(cfg, gallery_extra_dirs=[])
    service = GalleryService(lambda: cfg)
    # Stub transmit/unkey: log-only, and there is no CAT backend in this
    # process, so the capture can never key a radio.
    control = ControlPlane(
        now=time.monotonic,
        transmit=lambda *a: print("  (stub transmit)", a),
        unkey=lambda *a: print("  (stub unkey)", a),
        enabled=lambda: True, rig_ready=lambda: True,
    )
    server = RemoteServer(service, host="127.0.0.1", port=8799, token="demo",
                          control=control, tx_enabled=lambda: True)
    server.start()
    url = server.url
    cdp = None
    n = 0
    try:
        cdp = _CDP(chrome)
        for name, tab, w, h in (
            ("remote-gallery", "Gallery", 390, 844),
            ("remote-compose", "Compose", 390, 980),
            ("remote-logbook", "Logbook", 390, 844),
        ):
            cdp.shot(url, out / f"{name}.png", w, h, tab)
            print(f"  ok   {name}.png")
            n += 1
    finally:
        if cdp:
            cdp.close()
        server.stop()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--only", choices=("desktop", "remote"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    total = 0
    if args.only != "desktop":
        print("Remote (phone):")
        total += capture_remote(args.out)
    if args.only != "remote":
        print("Desktop:")
        total += capture_desktop(args.out)

    print(f"\n{total} screenshot(s) written to {args.out}")
    sys.stdout.flush()
    # capture_desktop leaves the main window's worker threads running; a normal
    # interpreter exit can block on them.
    os._exit(0)


if __name__ == "__main__":
    main()
