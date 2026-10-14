# SPDX-License-Identifier: GPL-3.0-or-later
"""Template TX-flow regressions from the v0.6.14 audit.

C-M1  an editor save is the active TX template immediately
C-M4  the rig frequency reaches %f / %b at TX
C-M5  Save As / Rename onto an existing name asks before overwriting
C-M6  the editor asks before discarding unsaved edits
UI-M4 removing the selected template rebuilds the preview from the bare
      image; a render failure at TX asks instead of sending the raw photo
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from open_sstv.config.schema import AppConfig
from open_sstv.templates.model import PhotoLayer, Template, TextLayer
from open_sstv.templates.toml_io import load_template, save_template
from open_sstv.ui.template_editor import TemplateEditor
from open_sstv.ui.tx_panel import TxPanel

pytestmark = pytest.mark.gui


def _tpl(name: str, text: str = "%c") -> Template:
    return Template(
        name=name,
        role="cq",
        layers=[
            PhotoLayer(id="photo", anchor="FILL", fit="cover"),
            TextLayer(id="t", text_raw=text, anchor="BC", font_size_pct=8.0),
        ],
    )


@pytest.fixture
def tdir(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    save_template(_tpl("Alpha"), d / "alpha.toml")
    save_template(_tpl("Beta"), d / "beta.toml")
    return d


@pytest.fixture
def panel(qtbot, tmp_path: Path, tdir: Path) -> TxPanel:
    p = TxPanel(app_config=AppConfig(callsign="W0AEZ"), templates_dir=tdir)
    qtbot.addWidget(p)
    img = Image.new("RGB", (200, 100), (10, 200, 10))
    path = tmp_path / "photo.png"
    img.save(path)
    p.load_image(path)
    return p


def _select(panel: TxPanel, name: str) -> None:
    for card in panel._gallery._cards:
        if card.template.name == name:
            panel._gallery._on_card_clicked(card.template)
            return
    raise AssertionError(name)


# --- C-M1 -------------------------------------------------------------------


def test_editor_save_becomes_active_tx_template(panel: TxPanel, tdir: Path) -> None:
    _select(panel, "Alpha")
    old = panel._selected_template
    edited = _tpl("Alpha", text="EDITED %c")
    save_template(edited, tdir / "alpha.toml")
    panel._on_template_saved_in_editor(tdir / "alpha.toml")
    assert panel._selected_template is not old
    texts = [l.text_raw for l in panel._selected_template.layers if isinstance(l, TextLayer)]
    assert texts == ["EDITED %c"]


def test_editor_save_of_other_template_selects_it(panel: TxPanel, tdir: Path) -> None:
    _select(panel, "Alpha")
    panel._on_template_saved_in_editor(tdir / "beta.toml")
    assert panel._selected_template.name == "Beta"


# --- UI-M4 ------------------------------------------------------------------


def test_deleting_selected_template_shows_bare_image(
    panel: TxPanel, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    _select(panel, "Alpha")
    # Composite is at the mode's frame size, not the 200x100 photo.
    assert panel._preview_source.width() != 200
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    panel._on_delete_template_requested(panel._selected_template, tdir / "alpha.toml")
    assert panel._selected_template is None
    assert (panel._preview_source.width(), panel._preview_source.height()) == (200, 100)


def test_render_failure_at_tx_asks_and_can_abort(
    qtbot, panel: TxPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_sstv.ui.tx_panel as tx_mod

    _select(panel, "Alpha")

    def _boom(*_a, **_k):
        raise RuntimeError("font exploded")

    monkeypatch.setattr(tx_mod, "render_template", _boom)
    sent: list[object] = []
    panel.transmit_requested.connect(lambda img, mode: sent.append(img))

    asked: list[bool] = []
    monkeypatch.setattr(
        panel, "_confirm_send_without_template", lambda: asked.append(True) or False
    )
    panel._on_transmit_clicked()
    assert asked and sent == []  # declined → nothing transmitted

    monkeypatch.setattr(panel, "_confirm_send_without_template", lambda: True)
    panel._on_transmit_clicked()
    assert sent == [panel._current_image]


# --- C-M4 -------------------------------------------------------------------


def test_rig_frequency_reaches_tx_context(
    panel: TxPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_sstv.ui.tx_panel as tx_mod

    seen: list[object] = []
    real = tx_mod.render_template

    def spy(t, q, c, ctx):  # type: ignore[no-untyped-def]
        seen.append(ctx.frequency_hz)
        return real(t, q, c, ctx)

    monkeypatch.setattr(tx_mod, "render_template", spy)
    panel.set_frequency_getter(lambda: 14_230_000)
    _select(panel, "Alpha")
    panel._compose_template()
    assert seen and seen[-1] == 14_230_000


# --- C-M5 -------------------------------------------------------------------


def test_rename_onto_existing_asks_and_cancel_keeps_both(
    panel: TxPanel, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_sstv.ui.tx_panel as tx_mod
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Beta", True)))
    asked: list[Path] = []
    monkeypatch.setattr(
        tx_mod, "ask_overwrite_template", lambda parent, target: asked.append(target) or "cancel"
    )
    alpha = load_template(tdir / "alpha.toml")
    panel._on_rename_template_requested(alpha, tdir / "alpha.toml")
    assert asked == [tdir / "beta.toml"]
    assert (tdir / "alpha.toml").exists()
    assert load_template(tdir / "beta.toml").name == "Beta"


def test_rename_onto_existing_keep_both(
    panel: TxPanel, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_sstv.ui.tx_panel as tx_mod
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Beta", True)))
    monkeypatch.setattr(tx_mod, "ask_overwrite_template", lambda parent, target: "keep")
    alpha = load_template(tdir / "alpha.toml")
    panel._on_rename_template_requested(alpha, tdir / "alpha.toml")
    assert not (tdir / "alpha.toml").exists()
    assert (tdir / "beta_2.toml").exists()
    assert len(list(tdir.glob("*.toml"))) == 2


def _editor(qtbot, tdir: Path, template: Template, path: Path | None) -> TemplateEditor:
    ed = TemplateEditor(
        template, path=path, app_config=AppConfig(callsign="W0AEZ"), templates_dir=tdir
    )
    qtbot.addWidget(ed)
    return ed


def test_editor_save_as_onto_existing_asks(
    qtbot, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import open_sstv.ui.template_editor as ed_mod
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    ed = _editor(qtbot, tdir, _tpl("Beta", text="NEW"), None)
    monkeypatch.setattr(ed_mod, "ask_overwrite_template", lambda p, t: "cancel")
    ed._on_save_as_clicked()
    assert load_template(tdir / "beta.toml").layers[1].text_raw == "%c"

    monkeypatch.setattr(ed_mod, "ask_overwrite_template", lambda p, t: "overwrite")
    ed._on_save_as_clicked()
    assert load_template(tdir / "beta.toml").layers[1].text_raw == "NEW"


def test_new_template_save_never_overwrites(qtbot, tdir: Path) -> None:
    ed = _editor(qtbot, tdir, _tpl("Alpha", text="NEW"), None)
    ed._on_save_clicked()
    assert load_template(tdir / "alpha.toml").layers[1].text_raw == "%c"
    assert (tdir / "alpha_2.toml").exists()


# --- C-M6 -------------------------------------------------------------------


def test_editor_close_with_unsaved_edits_asks(
    qtbot, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ed = _editor(qtbot, tdir, load_template(tdir / "alpha.toml"), tdir / "alpha.toml")
    ed.show()
    assert not ed.is_dirty()
    ed._template.name = "Alpha changed"
    assert ed.is_dirty()

    monkeypatch.setattr(ed, "_ask_save_changes", lambda: "cancel")
    ed.reject()
    assert ed.isVisible(), "Cancel must keep the editor open"

    monkeypatch.setattr(ed, "_ask_save_changes", lambda: "save")
    ed.reject()
    assert not ed.isVisible()
    assert load_template(tdir / "alpha.toml").name == "Alpha changed"


def test_editor_close_clean_does_not_ask(
    qtbot, tdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ed = _editor(qtbot, tdir, load_template(tdir / "alpha.toml"), tdir / "alpha.toml")
    ed.show()

    def _fail() -> str:
        raise AssertionError("must not prompt when nothing changed")

    monkeypatch.setattr(ed, "_ask_save_changes", _fail)
    ed.reject()
    assert not ed.isVisible()


# --- HiDPI preview (v0.6.14 audit UI nit) ----------------------------------


def test_preview_pixmap_is_rendered_at_device_pixel_ratio(
    panel: TxPanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel._preview.resize(400, 300)
    monkeypatch.setattr(type(panel._preview), "devicePixelRatioF", lambda self: 2.0)
    # QLabel.pixmap() re-rasterises to the real (offscreen, 1.0) screen
    # ratio, so capture what the panel hands to setPixmap instead.
    shown: list = []
    monkeypatch.setattr(
        type(panel._preview), "setPixmap", lambda self, p: shown.append(p)
    )
    panel._update_preview_pixmap()
    pix = shown[-1]
    assert pix.devicePixelRatio() == 2.0
    # Device pixels ≈ 2× the logical fit (the 200x100 photo fits 400 wide).
    assert pix.width() == 800
    assert pix.height() == 400
