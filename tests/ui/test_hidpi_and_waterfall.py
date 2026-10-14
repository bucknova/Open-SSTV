# SPDX-License-Identifier: GPL-3.0-or-later
"""HiDPI previews and the waterfall's TX scaling (v0.6.14 audit nits).

* The RX preview and the waterfall scaled their pixmaps to the widget's
  logical size.  On a 2x display Qt then upscaled them again, so both were
  blurry.  They now render at device pixels with the ratio set.
* ``add_tx_column`` decided int16 vs float by ``max(abs) > 1.5``.  A quiet
  int16 chunk (TX silence, or the CW-ID gap) has max ≤ 1, so it was left
  unnormalised and drawn at full scale.  The scaling now follows the dtype.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QPixmap

from open_sstv.core.modes import Mode
from open_sstv.ui.rx_panel import RxPanel
from open_sstv.ui.utils import scaled_pixmap_for
from open_sstv.ui.waterfall_widget import WaterfallWidget

pytestmark = pytest.mark.gui


def test_scaled_pixmap_for_uses_device_pixels(qtbot) -> None:
    w = WaterfallWidget()
    qtbot.addWidget(w)
    w.devicePixelRatioF = lambda: 2.0  # type: ignore[method-assign]
    src = QPixmap(100, 50)
    out = scaled_pixmap_for(src, w, QSize(200, 100))
    assert (out.width(), out.height()) == (400, 200)
    assert out.devicePixelRatio() == 2.0


def test_rx_preview_is_rendered_at_device_pixels(qtbot) -> None:
    panel = RxPanel()
    qtbot.addWidget(panel)
    panel.resize(800, 600)
    panel._preview.devicePixelRatioF = lambda: 2.0  # type: ignore[method-assign]
    # Capture what the panel hands the label: newer Qt's QLabel.pixmap()
    # re-renders at the real screen ratio, which is 1 here.
    given: list[QPixmap] = []
    panel._preview.setPixmap = given.append  # type: ignore[method-assign]
    panel.show_image_complete(Image.new("RGB", (320, 256)), Mode.ROBOT_36, 8)
    pix = given[-1]
    assert pix.devicePixelRatio() == 2.0
    label = panel._preview.size()
    # KeepAspectRatio: one dimension fills the label at 2x.
    assert pix.width() == label.width() * 2 or pix.height() == label.height() * 2


def test_waterfall_backing_pixmap_uses_device_pixels(qtbot) -> None:
    w = WaterfallWidget()
    qtbot.addWidget(w)
    w.devicePixelRatioF = lambda: 2.0  # type: ignore[method-assign]
    pix = w._backing_pixmap(400, 200)
    assert (pix.width(), pix.height()) == (800, 400)
    assert pix.devicePixelRatio() == 2.0


def _column(w: WaterfallWidget, chunk: np.ndarray) -> np.ndarray:
    w.clear()
    w.add_tx_column(chunk)
    return w._buf[:, 0, :].copy()


def test_quiet_int16_tx_chunk_is_normalised(qtbot) -> None:
    w = WaterfallWidget()
    qtbot.addWidget(w)
    rng = np.random.default_rng(1)
    pcm = rng.integers(-1, 2, size=2048).astype(np.int16)  # |x| <= 1 LSB
    as_int = _column(w, pcm)
    as_float = _column(w, pcm.astype(np.float32) / 32768.0)
    assert np.array_equal(as_int, as_float), "int16 must always be /32768"
    assert as_int.max() == 0, "1-LSB noise is below the display floor"


def test_loud_float_tx_chunk_is_not_rescaled(qtbot) -> None:
    w = WaterfallWidget()
    qtbot.addWidget(w)
    t = np.arange(2048) / 48_000
    loud = (2.0 * np.sin(2 * np.pi * 1500 * t)).astype(np.float32)  # overdrive
    col = _column(w, loud)
    assert col.max() > 0, "a float chunk is already normalised; don't /32768"


def test_rx_chunk_unchanged(qtbot) -> None:
    w = WaterfallWidget()
    qtbot.addWidget(w)
    w.add_rx_column(np.zeros(2048))
    assert w._buf.max() == 0
