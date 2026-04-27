# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression for H3: the RX→TX image pipeline is wired end-to-end.

When the user single-clicks a thumbnail in the RX gallery (or invokes
*View* from the context menu), ``RxPanel.rx_image_selected`` fires.  The
main window connects that to ``TxPanel.set_rx_image``, which in turn
plumbs the PIL image into ``TXContext.rx_image`` so any selected reply
template's ``RxImageLayer`` renders the *user-pinned* image rather than
the most-recent decode.

This file pins down that contract: emit the signal, watch the data flow
all the way to the renderer's TXContext.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import (
    PhotoLayer,
    RxImageLayer,
    Template,
)
from open_sstv.templates.toml_io import save_template
from open_sstv.ui.rx_panel import RxPanel
from open_sstv.ui.tx_panel import TxPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def tdir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    return d


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(callsign="W0AEZ")


@pytest.fixture
def rx_panel(qtbot) -> RxPanel:
    p = RxPanel()
    qtbot.addWidget(p)
    return p


@pytest.fixture
def tx_panel(qtbot, cfg: AppConfig, tdir: Path) -> TxPanel:
    p = TxPanel(app_config=cfg, templates_dir=tdir)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def rx_image() -> Image.Image:
    """A distinctive RX image so it's easy to spot in rendered output."""
    img = Image.new("RGB", (160, 120), color=(200, 50, 50))
    return img


def _connect_pipeline(rx_panel: RxPanel, tx_panel: TxPanel) -> None:
    """Mirror the connection ``MainWindow`` makes at startup."""
    rx_panel.rx_image_selected.connect(tx_panel.set_rx_image)


def test_signal_sets_tx_panel_rx_image(
    rx_panel: RxPanel, tx_panel: TxPanel, rx_image: Image.Image
) -> None:
    """rx_image_selected → TxPanel._rx_image holds the same object."""
    _connect_pipeline(rx_panel, tx_panel)
    assert tx_panel._rx_image is None

    rx_panel.rx_image_selected.emit(rx_image)

    assert tx_panel._rx_image is rx_image


def test_signal_propagates_to_gallery(
    rx_panel: RxPanel, tx_panel: TxPanel, rx_image: Image.Image
) -> None:
    """The gallery picks up the RX image so its thumbnails re-render."""
    _connect_pipeline(rx_panel, tx_panel)
    assert tx_panel._gallery._rx_image is None

    rx_panel.rx_image_selected.emit(rx_image)

    assert tx_panel._gallery._rx_image is rx_image


def test_gallery_render_passes_rx_image_in_tx_context(
    qtbot,
    rx_panel: RxPanel,
    tx_panel: TxPanel,
    rx_image: Image.Image,
    tdir: Path,
) -> None:
    """The renderer call from the gallery sees rx_image in its TXContext.

    Patch ``render_template`` at the spot the gallery imports it from so
    we can capture every TXContext the gallery hands the renderer after
    the RX image arrives.  We then assert at least one of those contexts
    carries ``rx_image_selected``'s payload — proving the signal really
    drove a re-render with the new image.
    """
    template = Template(
        name="Reply",
        role="reply",
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            RxImageLayer(id="rx", anchor="BR", width_pct=30.0, height_pct=25.0),
        ],
    )
    save_template(template, tdir / "reply.toml")
    tx_panel._gallery.reload_templates()

    # Cards must be visible for the gallery's visibility-gated re-render
    # to actually call render_template; we also need a base photo so the
    # rendering path matches what the user sees.
    tx_panel.show()
    qtbot.waitExposed(tx_panel)
    tx_panel._gallery._photo = Image.new("RGB", (320, 256), color=(0, 50, 0))

    _connect_pipeline(rx_panel, tx_panel)

    captured_contexts: list = []
    real_render = __import__(
        "open_sstv.templates.renderer", fromlist=["render_template"]
    ).render_template

    def spy(template, qso_state, app_config, tx_context, **kw):
        captured_contexts.append(tx_context)
        return real_render(template, qso_state, app_config, tx_context, **kw)

    with patch("open_sstv.ui.template_gallery.render_template", side_effect=spy):
        rx_panel.rx_image_selected.emit(rx_image)
        # Let any deferred re-renders run.
        qtbot.wait(50)

    rx_contexts = [ctx for ctx in captured_contexts if ctx.rx_image is rx_image]
    assert rx_contexts, (
        "Expected at least one render_template call carrying the emitted "
        f"rx_image; got {len(captured_contexts)} contexts but none had it."
    )


def test_clearing_rx_image_propagates(
    rx_panel: RxPanel, tx_panel: TxPanel, rx_image: Image.Image
) -> None:
    """Emitting None resets the panel and gallery to no pinned RX image."""
    _connect_pipeline(rx_panel, tx_panel)
    rx_panel.rx_image_selected.emit(rx_image)
    assert tx_panel._rx_image is rx_image

    rx_panel.rx_image_selected.emit(None)
    assert tx_panel._rx_image is None
    assert tx_panel._gallery._rx_image is None


def test_rx_image_selected_signal_is_object_typed() -> None:
    """The signal must accept arbitrary PIL images (object-typed Signal).

    A ``Signal(PIL.Image.Image)`` would force PySide to import PIL at
    QObject metaclass time and break C++-side serialisation when None is
    emitted to clear.  We pin the loose typing so a refactor can't
    silently regress the clear path.
    """
    sig = RxPanel.rx_image_selected
    # PySide6 stores the parameter spec on the unbound signal descriptor.
    # It's enough to verify we can connect, emit a PIL image, and emit
    # None without TypeError — exact metadata layout differs across
    # PySide versions.
    p = RxPanel()
    received: list = []
    p.rx_image_selected.connect(lambda v: received.append(v))
    p.rx_image_selected.emit(Image.new("RGB", (4, 4)))
    p.rx_image_selected.emit(None)
    assert len(received) == 2
    assert received[1] is None
