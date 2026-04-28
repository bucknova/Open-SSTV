# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the TemplateGallery widget.

These are widget-level tests; they do not start a full MainWindow.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.core.modes import Mode
from open_sstv.templates.manager import install_starter_pack
from open_sstv.templates.model import PhotoLayer, QSOState, Template, TextLayer
from open_sstv.templates.toml_io import save_template
from open_sstv.ui.template_gallery import TemplateGallery, _ThumbnailCard

pytestmark = pytest.mark.gui


def _make_cfg(**kw: object) -> AppConfig:
    defaults: dict[str, object] = {"callsign": "W0AEZ"}
    defaults.update(kw)
    return AppConfig(**defaults)


def _make_template(name: str = "Test", role: str = "cq") -> Template:
    return Template(
        name=name,
        role=role,
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            TextLayer(
                id="txt",
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
    return tmp_path / "templates"


@pytest.fixture
def cfg() -> AppConfig:
    return _make_cfg()


@pytest.fixture
def gallery(qtbot, tdir: Path, cfg: AppConfig) -> TemplateGallery:
    g = TemplateGallery(app_config=cfg, templates_dir=tdir)
    qtbot.addWidget(g)
    return g


@pytest.fixture
def gallery_with_templates(
    qtbot, tdir: Path, cfg: AppConfig
) -> TemplateGallery:
    tdir.mkdir(parents=True, exist_ok=True)
    save_template(_make_template("Alpha", "cq"), tdir / "alpha.toml")
    save_template(_make_template("Beta", "reply"), tdir / "beta.toml")
    save_template(_make_template("Gamma", "closing"), tdir / "gamma.toml")
    g = TemplateGallery(app_config=cfg, templates_dir=tdir)
    qtbot.addWidget(g)
    g.reload_templates()
    return g


# ---------------------------------------------------------------------------
# Empty gallery
# ---------------------------------------------------------------------------


class TestEmptyGallery:
    def test_no_cards_when_dir_missing(self, gallery: TemplateGallery) -> None:
        gallery.reload_templates()
        assert gallery._cards == []

    def test_no_templates_label_visible(self, gallery: TemplateGallery) -> None:
        gallery.reload_templates()
        assert not gallery._no_templates_label.isHidden()

    def test_selected_template_is_none(self, gallery: TemplateGallery) -> None:
        gallery.reload_templates()
        assert gallery.selected_template() is None


# ---------------------------------------------------------------------------
# Cards built correctly
# ---------------------------------------------------------------------------


class TestCardsBuilt:
    def test_card_count_matches_templates(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        assert len(gallery_with_templates._cards) == 3

    def test_no_templates_label_hidden_when_cards_present(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        assert not gallery_with_templates._no_templates_label.isVisible()

    def test_cards_have_correct_names(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        names = {c.template.name for c in gallery_with_templates._cards}
        assert names == {"Alpha", "Beta", "Gamma"}


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------


class TestTemplateSelection:
    def test_clicking_card_emits_template_selected(
        self, qtbot, gallery_with_templates: TemplateGallery
    ) -> None:
        card = gallery_with_templates._cards[0]
        with qtbot.waitSignal(
            gallery_with_templates.template_selected, timeout=500
        ) as blocker:
            card.clicked.emit(card.template)
        assert blocker.args[0] is card.template

    def test_clicking_card_marks_it_selected(
        self, qtbot, gallery_with_templates: TemplateGallery
    ) -> None:
        card = gallery_with_templates._cards[0]
        gallery_with_templates._on_card_clicked(card.template)
        assert card._selected is True

    def test_clicking_different_card_deselects_previous(
        self, qtbot, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        card_a, card_b = g._cards[0], g._cards[1]
        g._on_card_clicked(card_a.template)
        assert card_a._selected is True
        g._on_card_clicked(card_b.template)
        assert card_a._selected is False
        assert card_b._selected is True

    def test_selected_template_returns_correct_template(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        g._on_card_clicked(g._cards[0].template)
        assert g.selected_template() is g._cards[0].template

    def test_clear_selection_deselects_all(
        self, qtbot, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        g._on_card_clicked(g._cards[0].template)
        with qtbot.waitSignal(g.template_selected, timeout=500) as blocker:
            g.clear_selection()
        assert blocker.args[0] is None
        assert g.selected_template() is None
        assert all(not c._selected for c in g._cards)


# ---------------------------------------------------------------------------
# Role filter
# ---------------------------------------------------------------------------


class TestRoleFilter:
    def test_all_shows_all_cards(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        g._active_role = None
        g._apply_role_filter()
        assert all(not c.isHidden() for c in g._cards)

    def test_cq_filter_hides_non_cq(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        g._active_role = "cq"
        g._apply_role_filter()
        visible = [c for c in g._cards if not c.isHidden()]
        assert all(c.template.role == "cq" for c in visible)

    def test_reply_filter_shows_only_reply(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        g = gallery_with_templates
        g._active_role = "reply"
        g._apply_role_filter()
        visible = [c for c in g._cards if not c.isHidden()]
        assert len(visible) == 1
        assert visible[0].template.name == "Beta"


# ---------------------------------------------------------------------------
# Starter pack integration
# ---------------------------------------------------------------------------


class TestStarterPackGallery:
    def test_starter_pack_loads_into_gallery(
        self, qtbot, tmp_path: Path, cfg: AppConfig
    ) -> None:
        tdir = tmp_path / "templates"
        install_starter_pack(tdir)
        g = TemplateGallery(app_config=cfg, templates_dir=tdir)
        qtbot.addWidget(g)
        g.reload_templates()
        assert len(g._cards) == 8

    def test_starter_pack_renders_thumbnails(
        self, qtbot, tmp_path: Path, cfg: AppConfig
    ) -> None:
        tdir = tmp_path / "templates"
        install_starter_pack(tdir)
        g = TemplateGallery(app_config=cfg, templates_dir=tdir)
        qtbot.addWidget(g)
        g.reload_templates()
        # All cards should have a pixmap (successful render).
        for card in g._cards:
            assert card._thumb_label.pixmap() is not None


# ---------------------------------------------------------------------------
# set_photo / set_qso_state / set_mode
# ---------------------------------------------------------------------------


class TestUpdates:
    def test_set_photo_triggers_rerender(
        self, qtbot, gallery_with_templates: TemplateGallery, tmp_path: Path
    ) -> None:
        # Cards must be visible so ``_rerender_all`` doesn't skip them.
        # ``addWidget`` already keeps the gallery alive but doesn't ``show()``
        # it; manually mark each card visible so ``isVisible()`` flips.
        gallery_with_templates.show()
        qtbot.waitExposed(gallery_with_templates)

        img = Image.new("RGB", (320, 256), color=(100, 150, 200))
        gallery_with_templates.set_photo(img)
        for card in gallery_with_templates._cards:
            pix = card._thumb_label.pixmap()
            # ``pixmap()`` returns a QPixmap object even when no pixmap has
            # been set, so the previous ``is not None`` check passed
            # vacuously.  Tighten to verify the pixmap actually carries
            # a rendered thumbnail.
            assert not pix.isNull(), (
                f"Card '{card.template.name}' has a null pixmap after "
                f"set_photo — render didn't reach the label."
            )
            assert pix.size().width() > 0 and pix.size().height() > 0

    def test_set_photo_forces_repaint_of_thumbnail_labels(
        self, qtbot, gallery_with_templates: TemplateGallery
    ) -> None:
        """Regression: thumbnails went blank after ``load_image`` until the
        user resized the window.

        Root cause: ``QLabel.setPixmap`` schedules a deferred paint via
        ``update()``.  When the cards' fixed sizes don't change (same
        aspect-ratio mode → same ``thumb_w`` in/out) no layout pass fires,
        and on some Qt platforms the queued paint gets coalesced and never
        reaches the screen.  A window resize forces a fresh layout + paint
        cycle which then picks up the already-set pixmap.

        Fix: ``set_pixmap`` now calls ``self._thumb_label.update()``
        explicitly after ``setPixmap``, and ``_rerender_all`` calls
        ``self._strip_widget.update()`` after the batch.  This test wraps
        both to count invocations and asserts both fire on every
        ``set_photo``.
        """
        gallery_with_templates.show()
        qtbot.waitExposed(gallery_with_templates)

        # Spy on per-card and strip-level ``update()`` calls.
        thumb_updates: list[str] = []
        strip_updates: list[int] = [0]
        for card in gallery_with_templates._cards:
            real_update = card._thumb_label.update
            tname = card.template.name

            def _spy(*args, _real=real_update, _name=tname, **kw):
                thumb_updates.append(_name)
                return _real(*args, **kw)

            card._thumb_label.update = _spy  # type: ignore[method-assign]

        real_strip_update = gallery_with_templates._strip_widget.update

        def _strip_spy(*args, _real=real_strip_update, **kw):
            strip_updates[0] += 1
            return _real(*args, **kw)

        gallery_with_templates._strip_widget.update = _strip_spy  # type: ignore[method-assign]

        img = Image.new("RGB", (320, 256), color=(220, 80, 50))
        gallery_with_templates.set_photo(img)

        # Every visible card must have had its label's update() forced.
        visible_names = [
            c.template.name
            for c in gallery_with_templates._cards
            if c.isVisible()
        ]
        for name in visible_names:
            assert name in thumb_updates, (
                f"Card '{name}' visible but ``_thumb_label.update()`` was "
                f"not called — the explicit repaint trigger is missing"
            )
        # Strip-level update() also fires once per ``_rerender_all``.
        assert strip_updates[0] >= 1, (
            "_strip_widget.update() was not called after _rerender_all — "
            "the batch repaint backstop is missing"
        )

    def test_set_qso_state_triggers_rerender(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        qso = QSOState(tocall="K0TEST", rst="595")
        gallery_with_templates.set_qso_state(qso)
        assert gallery_with_templates._qso_state.tocall == "K0TEST"

    def test_set_mode_stores_mode(
        self, gallery_with_templates: TemplateGallery
    ) -> None:
        gallery_with_templates.set_mode(Mode.MARTIN_M1)
        assert gallery_with_templates._mode == Mode.MARTIN_M1
