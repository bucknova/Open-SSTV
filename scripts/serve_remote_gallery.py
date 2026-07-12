#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dev preview for the Phase 1 read-only remote gallery server.

Spins up :class:`~open_sstv.remote.server.RemoteServer` against your
*real* gallery so you can eyeball the web UI in a browser without
launching the full Qt app.  If the real gallery is empty (or you pass
``--fake``), it generates a handful of synthetic testcards in a temp
directory instead, so there is always something to look at — the temp
images are never written into your real Pictures folder.

    python3 scripts/serve_remote_gallery.py
    python3 scripts/serve_remote_gallery.py --fake --port 9000

Ctrl-C to stop.  This is a developer convenience only; the real embedded
server is what runs inside the app when ``remote_enabled`` is set.
"""
from __future__ import annotations

import argparse
import colorsys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from open_sstv.config.schema import AppConfig
from open_sstv.config.store import load_config
from open_sstv.gallery.scanner import scan_dirs
from open_sstv.remote import GalleryService, RemoteServer

#: Filenames for the synthetic fallback set.  A mix of default-pattern
#: names (so the mode badge resolves) and a bare name (so the "Unknown"
#: path is exercised too).
_FAKE_NAMES = (
    "2026-07-05_142230_scottie_s1.png",
    "2026-07-05_150012_martin_m1.png",
    "2026-07-06_090500_pd_120.png",
    "2026-07-06_181745_robot_36.png",
    "2026-07-07_071010_wraase_sc2_180.png",
    "holiday_beach.png",
)


def _real_source_dirs(cfg: AppConfig) -> list[Path]:
    """``images_save_dir`` + any ``gallery_extra_dirs`` (same as the app)."""
    dirs: list[Path] = []
    if cfg.images_save_dir:
        dirs.append(Path(cfg.images_save_dir))
    dirs.extend(Path(d) for d in cfg.gallery_extra_dirs)
    return dirs


def _make_fake_gallery(count: int) -> Path:
    """Generate *count* synthetic SSTV-ish testcards in a fresh temp dir.

    Returns the directory.  Imports Pillow lazily so ``--help`` and the
    real-gallery path don't pay for it.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    out = Path(tempfile.mkdtemp(prefix="open_sstv_remote_demo_"))
    names = list(_FAKE_NAMES)[: max(1, count)]
    for i, name in enumerate(names):
        base = colorsys.hsv_to_rgb(i / max(1, len(names)), 0.5, 0.55)
        fill = tuple(int(c * 255) for c in base)
        im = Image.new("RGB", (320, 256), fill)
        dr = ImageDraw.Draw(im)
        for y in range(0, 256, 8):
            hue = (y / 256.0 + i / len(names)) % 1.0
            r, g, b = (int(v * 255) for v in colorsys.hsv_to_rgb(hue, 0.5, 0.9))
            dr.line([(0, y), (320, y)], fill=(r, g, b), width=3)
        dr.rectangle([90, 100, 230, 156], fill=fill)
        dr.text((100, 122), name.split("_")[-1].split(".")[0], fill="white")
        im.save(out / name)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default loopback)")
    parser.add_argument("--port", type=int, default=8731, help="port (default 8731)")
    parser.add_argument("--token", default="demo", help="access token (default 'demo')")
    parser.add_argument("--count", type=int, default=6, help="synthetic image count")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="force synthetic images even if the real gallery has some",
    )
    args = parser.parse_args()

    cfg = load_config()
    real_items = scan_dirs(_real_source_dirs(cfg))

    if real_items and not args.fake:
        source = "real gallery"
        service = GalleryService(lambda: cfg)
        detail = f"{len(real_items)} image(s) from {cfg.images_save_dir}"
    else:
        why = "forced" if args.fake else "real gallery is empty"
        fake_dir = _make_fake_gallery(args.count)
        # A non-existent temp DB path → no logbook enrichment, and the
        # service never creates one (matches the view-only contract).
        synth = replace(
            cfg,
            images_save_dir=str(fake_dir),
            gallery_extra_dirs=[],
            logbook_db_path=str(fake_dir / "no-logbook.db"),
        )
        service = GalleryService(lambda: synth)
        source = f"synthetic images ({why})"
        detail = f"{args.count} testcard(s) in {fake_dir}"

    server = RemoteServer(service, host=args.host, port=args.port, token=args.token)
    server.start()
    print(f"Serving {source}: {detail}")
    print(f"Open:  {server.url}")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
