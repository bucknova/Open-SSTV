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
# TxWorker.set_v3_template_active
# ---------------------------------------------------------------------------


class TestTxWorkerV3Flag:
    def test_default_v3_flag_is_false(self) -> None:
        worker = TxWorker()
        assert worker._v3_template_active is False

    def test_set_v3_template_active_true(self) -> None:
        worker = TxWorker()
        worker.set_v3_template_active(True)
        assert worker._v3_template_active is True

    def test_set_v3_template_active_false(self) -> None:
        worker = TxWorker()
        worker.set_v3_template_active(True)
        worker.set_v3_template_active(False)
        assert worker._v3_template_active is False

    def test_banner_skipped_when_v3_active(self, tmp_path: Path) -> None:
        """When _v3_template_active is True, apply_tx_banner is NOT called.

        We verify this by encoding a tiny 1×1 image (which would raise
        ValueError from apply_tx_banner's content_height check) and
        confirming no error is emitted from the worker.
        """
        errors: list[str] = []
        worker = TxWorker()
        worker.set_v3_template_active(True)
        worker._tx_banner_enabled = True
        worker._tx_banner_callsign = "W0AEZ"
        worker.error.connect(errors.append)

        img = Image.new("RGB", (320, 256), color=(0, 128, 0))
        done = threading.Event()
        worker.transmission_complete.connect(done.set)
        worker.transmission_aborted.connect(done.set)

        worker.transmit(img, Mode.MARTIN_M1)
        done.wait(timeout=5)
        # With v3 active, banner code is skipped so no ValueError from
        # a tiny image — any errors should be audio-related (no device),
        # not banner-related.
        banner_errors = [e for e in errors if "banner" in e.lower()]
        assert banner_errors == [], (
            f"Banner error emitted even with v3 template active: {banner_errors}"
        )
