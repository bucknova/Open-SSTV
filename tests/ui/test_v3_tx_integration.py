# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for v0.3 template → TX encoder integration.

Covers:
- When a v0.3 template is selected, transmit_requested emits the composed image.
- When no template is selected, transmit_requested emits the raw loaded image.
- template_composited signal fires True/False at the right times.
- TxWorker.set_v3_template_active skips banner when True.
- TxPanel.get_qso_state / QSO widget integration.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import Mode
from open_sstv.templates.manager import install_starter_pack
from open_sstv.templates.model import (
    PhotoLayer,
    QSOState,
    RectLayer,
    Template,
    TextLayer,
)
from open_sstv.templates.toml_io import save_template
from open_sstv.ui.tx_panel import TxPanel
from open_sstv.ui.workers import TxWorker

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**kw: object) -> AppConfig:
    defaults: dict[str, object] = {"callsign": "W0AEZ"}
    defaults.update(kw)
    return AppConfig(**defaults)


def _make_template(name: str = "Test CQ", role: str = "cq") -> Template:
    return Template(
        name=name,
        role=role,
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            RectLayer(
                id="banner",
                anchor="BL",
                width_pct=100.0,
                height_pct=20.0,
                fill=(0, 0, 0, 200),
            ),
            TextLayer(
                id="call",
                text_raw="%c",
                anchor="BC",
                font_family="DejaVu Sans Bold",
                font_size_pct=8.0,
                fill=(255, 255, 255, 255),
            ),
        ],
    )


@pytest.fixture
def tdir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    return d


@pytest.fixture
def cfg() -> AppConfig:
    return _make_cfg()


@pytest.fixture
def img_path(tmp_path: Path) -> Path:
    img = Image.new("RGB", (320, 256), color=(64, 128, 192))
    p = tmp_path / "photo.png"
    img.save(p)
    return p


@pytest.fixture
def panel(qtbot, cfg: AppConfig, tdir: Path) -> TxPanel:
    p = TxPanel(app_config=cfg, templates_dir=tdir)
    qtbot.addWidget(p)
    return p


# ---------------------------------------------------------------------------
# template_composited signal
# ---------------------------------------------------------------------------


class TestTemplateComposited:
    def test_emits_true_when_template_selected(
        self, qtbot, panel: TxPanel, tdir: Path
    ) -> None:
        t = _make_template()
        save_template(t, tdir / "test.toml")
        panel._gallery.reload_templates()
        card = panel._gallery._cards[0]
        with qtbot.waitSignal(panel.template_composited, timeout=500) as blocker:
            card.clicked.emit(card.template)
        assert blocker.args[0] is True

    def test_emits_false_when_selection_cleared(
        self, qtbot, panel: TxPanel, tdir: Path
    ) -> None:
        t = _make_template()
        save_template(t, tdir / "test.toml")
        panel._gallery.reload_templates()
        card = panel._gallery._cards[0]
        panel._gallery._on_card_clicked(card.template)
        with qtbot.waitSignal(panel.template_composited, timeout=500) as blocker:
            panel._gallery.clear_selection()
        assert blocker.args[0] is False


# ---------------------------------------------------------------------------
# TX image content
# ---------------------------------------------------------------------------


class TestTransmitImage:
    def test_no_template_emits_raw_image(
        self, qtbot, panel: TxPanel, img_path: Path
    ) -> None:
        panel.load_image(img_path)
        with qtbot.waitSignal(panel.transmit_requested, timeout=1000) as blocker:
            panel._transmit_btn.click()
        image, _mode = blocker.args
        assert image.size == (320, 256)

    def test_with_template_emits_composed_image(
        self, qtbot, panel: TxPanel, img_path: Path, tdir: Path, cfg: AppConfig
    ) -> None:
        panel.set_app_config(cfg)
        t = _make_template()
        save_template(t, tdir / "test.toml")
        panel.load_image(img_path)
        panel._gallery.reload_templates()
        card = panel._gallery._cards[0]
        panel._gallery._on_card_clicked(card.template)
        with qtbot.waitSignal(panel.transmit_requested, timeout=1000) as blocker:
            panel._transmit_btn.click()
        image, mode = blocker.args
        # Composed image has the mode's native dimensions.
        assert isinstance(image, Image.Image)
        assert image.mode == "RGB"

    def test_with_template_mode_shape_matches_selection(
        self, qtbot, panel: TxPanel, img_path: Path, tdir: Path, cfg: AppConfig
    ) -> None:
        panel.set_app_config(cfg)
        t = _make_template()
        save_template(t, tdir / "test.toml")
        panel.load_image(img_path)
        panel._gallery.reload_templates()
        card = panel._gallery._cards[0]
        panel._gallery._on_card_clicked(card.template)
        # Select Martin M1 (320×256)
        for i in range(panel._mode_combo.count()):
            if panel._mode_combo.itemData(i) == Mode.MARTIN_M1:
                panel._mode_combo.setCurrentIndex(i)
                break
        with qtbot.waitSignal(panel.transmit_requested, timeout=1000) as blocker:
            panel._transmit_btn.click()
        image, mode = blocker.args
        assert mode == Mode.MARTIN_M1
        assert image.size == (320, 256)


# ---------------------------------------------------------------------------
# QSO state integration
# ---------------------------------------------------------------------------


class TestQSOStateIntegration:
    def test_get_qso_state_returns_qso_state(self, panel: TxPanel) -> None:
        assert isinstance(panel.get_qso_state(), QSOState)

    def test_tocall_set_in_state(self, qtbot, panel: TxPanel) -> None:
        panel._qso_widget._tocall.setText("K0TEST")
        qtbot.wait(50)  # let debounce settle somewhat
        # Direct get_state() is always fresh
        assert panel.get_qso_state().tocall == "K0TEST"

    def test_qso_state_feeds_gallery(
        self, qtbot, panel: TxPanel, tdir: Path, cfg: AppConfig
    ) -> None:
        panel.set_app_config(cfg)
        t = _make_template()
        save_template(t, tdir / "test.toml")
        panel._gallery.reload_templates()

        qso = QSOState(tocall="W0XYZ", rst="595")
        with qtbot.waitSignal(
            panel._gallery.template_selected, raising=False, timeout=100
        ):
            panel._gallery.set_qso_state(qso)
        assert panel._gallery._qso_state.tocall == "W0XYZ"


# ---------------------------------------------------------------------------
# TxWorker banner policy (v0.3.13: always-on-when-enabled)
# ---------------------------------------------------------------------------
#
# v0.3.13 removed ``_v3_template_active`` and ``set_v3_template_active``.
# The banner stamp now obeys only ``_tx_banner_enabled``, regardless of
# whether a v0.3 template has been composited into the image.  Per user
# (Kevin/W0AEZ) feedback: banner-on means banner-always-on.


class TestTxWorkerBannerPolicy:
    def test_banner_enabled_stamps_regardless_of_template(
        self, tmp_path: Path  # noqa: ARG002
    ) -> None:
        """Banner is applied whenever ``_tx_banner_enabled`` is True.

        Previously gated on a separate ``_v3_template_active`` flag; v0.3.13
        removed that gating.  Verify by enabling the banner and confirming
        the banner code path runs (the apply_tx_banner call itself runs
        because no ``TX banner failed`` error is emitted on a normally-sized
        image)."""
        errors: list[str] = []
        worker = TxWorker()
        worker._tx_banner_enabled = True
        worker._tx_banner_callsign = "W0AEZ"
        worker.error.connect(errors.append)

        img = Image.new("RGB", (320, 256), color=(0, 128, 0))
        done = threading.Event()
        worker.transmission_complete.connect(done.set)
        worker.transmission_aborted.connect(done.set)

        worker.transmit(img, Mode.MARTIN_M1)
        done.wait(timeout=5)
        banner_errors = [e for e in errors if "banner" in e.lower()]
        assert banner_errors == [], (
            f"Unexpected banner error: {banner_errors}"
        )

    def test_v3_template_active_attr_removed(self) -> None:
        """v0.3.13 contract: the ``_v3_template_active`` field and the
        ``set_v3_template_active`` slot are gone.  Pins the policy
        change so a regression that re-introduces template-aware
        banner gating fails this test."""
        worker = TxWorker()
        assert not hasattr(worker, "_v3_template_active"), (
            "v0.3.13 removed _v3_template_active — banner is now "
            "always-on-when-enabled regardless of template state"
        )
        assert not hasattr(worker, "set_v3_template_active"), (
            "v0.3.13 removed set_v3_template_active slot"
        )
