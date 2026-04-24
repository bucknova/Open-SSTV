# SPDX-License-Identifier: GPL-3.0-or-later
"""Template Gallery widget for the TX panel.

Displays a horizontal scrolling strip of template thumbnails.  Each card
shows a live-rendered composite (photo + template + QSO state) scaled to
~140 px wide with the template name below.  A role filter above the strip
narrows to CQ / Reply / 73 / Custom.

v0.3.0 note: thumbnails render synchronously on the GUI thread.  For
≤10 templates (the typical starter-pack count) this takes < 50 ms total
and is imperceptible.  Async rendering is a v0.3.1 concern.

Signals
-------
template_selected(object):
    Emitted when the user clicks a thumbnail.  Carries the selected
    ``Template`` instance, or ``None`` when the selection is cleared.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from open_sstv.templates.manager import list_templates, load_by_path
from open_sstv.templates.model import QSOState, TXContext, Template
from open_sstv.templates.renderer import render_template
from open_sstv.ui.utils import pil_to_pixmap

if TYPE_CHECKING:
    from pathlib import Path

    from PIL.Image import Image as PILImage

    from open_sstv.config.schema import AppConfig
    from open_sstv.core.modes import Mode

_log = logging.getLogger(__name__)

# Thumbnail width in pixels (height auto from aspect).
_THUMB_W: int = 140

# Role labels shown in the filter bar.
_ROLE_LABELS: tuple[tuple[str, str | None], ...] = (
    ("All", None),
    ("CQ", "cq"),
    ("Reply", "reply"),
    ("73", "closing"),
    ("Custom", "custom"),
)


class _ThumbnailCard(QWidget):
    """One card in the gallery: thumbnail image + name label."""

    clicked = Signal(object)  # Template

    def __init__(self, template: Template, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._template = template
        self._selected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Thumbnail image.
        self._thumb_label = QLabel()
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setFixedWidth(_THUMB_W)
        self._thumb_label.setMinimumHeight(50)
        self._thumb_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        # Subtle border so empty thumbnails have a visible bounding box.
        self._thumb_label.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); background: palette(base); }"
        )
        layout.addWidget(self._thumb_label)

        # Template name caption.
        name_label = QLabel(template.name)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setFixedWidth(_THUMB_W)
        name_label.setWordWrap(True)
        name_label.setStyleSheet("QLabel { font-size: 9px; }")
        layout.addWidget(name_label)

        self._set_border()

    # --- public ---

    def set_pixmap(self, pix: QPixmap) -> None:
        """Update the thumbnail image."""
        scaled = pix.scaledToWidth(
            _THUMB_W, Qt.TransformationMode.SmoothTransformation
        )
        self._thumb_label.setFixedHeight(scaled.height())
        self._thumb_label.setPixmap(scaled)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._set_border()

    @property
    def template(self) -> Template:
        return self._template

    # --- private ---

    def _set_border(self) -> None:
        border = "2px solid #0078d4" if self._selected else "1px solid palette(mid)"
        self._thumb_label.setStyleSheet(
            f"QLabel {{ border: {border}; background: palette(base); }}"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._template)
        super().mousePressEvent(event)


class TemplateGallery(QWidget):
    """Horizontal scrolling thumbnail strip with role filter.

    Parameters
    ----------
    app_config:
        Application config (callsign, grid, etc.) used for token resolution
        in thumbnail renders.  Can be ``None`` on first construction — call
        ``set_app_config()`` before ``reload_templates()``.
    templates_dir:
        Override the default templates directory (for tests).
    """

    template_selected = Signal(object)  # Template | None

    def __init__(
        self,
        app_config: "AppConfig | None" = None,
        templates_dir: "Path | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._app_config = app_config
        self._templates_dir = templates_dir
        self._photo: "PILImage | None" = None
        self._qso_state: QSOState = QSOState()
        self._mode: "Mode | None" = None
        self._selected_template: Template | None = None
        self._active_role: str | None = None  # None = All
        self._cards: list[_ThumbnailCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # --- Role filter bar ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(2)
        filter_row.addWidget(QLabel("Templates:"))
        self._role_group = QButtonGroup(self)
        self._role_group.setExclusive(True)
        for label, role in _ROLE_LABELS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setMaximumWidth(60)
            btn.setProperty("role_value", role)
            if role is None:
                btn.setChecked(True)
            self._role_group.addButton(btn)
            filter_row.addWidget(btn)
        self._role_group.buttonClicked.connect(self._on_role_filter_changed)
        filter_row.addStretch(1)
        outer.addLayout(filter_row)

        # --- Scroll area with card strip ---
        self._scroll = QScrollArea()
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFixedHeight(175)  # thumb + caption + padding

        self._strip_widget = QWidget()
        self._strip_layout = QHBoxLayout(self._strip_widget)
        self._strip_layout.setContentsMargins(4, 4, 4, 4)
        self._strip_layout.setSpacing(8)
        self._strip_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._strip_widget)
        outer.addWidget(self._scroll)

        self._no_templates_label = QLabel("No templates installed.")
        self._no_templates_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_templates_label.setVisible(False)
        outer.addWidget(self._no_templates_label)

    # === Public API ===

    def set_app_config(self, cfg: "AppConfig") -> None:
        """Update the config used for token resolution."""
        self._app_config = cfg

    def set_photo(self, photo: "PILImage | None") -> None:
        """Update the base photo; re-renders all visible thumbnails."""
        self._photo = photo
        self._rerender_all()

    def set_qso_state(self, qso_state: QSOState) -> None:
        """Update QSO state; re-renders all visible thumbnails."""
        self._qso_state = qso_state
        self._rerender_all()

    def set_mode(self, mode: "Mode") -> None:
        """Update SSTV mode (affects aspect ratio); re-renders all thumbnails."""
        self._mode = mode
        self._rerender_all()

    def reload_templates(self) -> None:
        """Reload the templates directory and rebuild the strip."""
        # Build a fresh list of (template, path) pairs.
        entries = list_templates(self._templates_dir)
        templates: list[Template] = []
        for _name, _role, path in entries:
            t = load_by_path(path)
            if t is not None:
                templates.append(t)

        self._rebuild_strip(templates)

    def selected_template(self) -> Template | None:
        return self._selected_template

    def clear_selection(self) -> None:
        """Deselect the current template."""
        self._selected_template = None
        for card in self._cards:
            card.set_selected(False)
        self.template_selected.emit(None)

    # === Slots ===

    @Slot(object)
    def _on_role_filter_changed(self, button: QPushButton) -> None:
        self._active_role = button.property("role_value")
        self._apply_role_filter()

    @Slot(object)
    def _on_card_clicked(self, template: Template) -> None:
        self._selected_template = template
        for card in self._cards:
            card.set_selected(card.template is template)
        self.template_selected.emit(template)

    # === Private ===

    def _rebuild_strip(self, templates: list[Template]) -> None:
        """Replace all cards with new ones for *templates*."""
        # Remove all old cards.
        for card in self._cards:
            self._strip_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        # If the previously selected template is still present, keep it.
        prev_name = (
            self._selected_template.name if self._selected_template else None
        )
        self._selected_template = None

        for t in templates:
            card = _ThumbnailCard(t, self._strip_widget)
            card.clicked.connect(self._on_card_clicked)
            self._strip_layout.addWidget(card)
            self._cards.append(card)
            if t.name == prev_name:
                self._selected_template = t
                card.set_selected(True)

        has_cards = bool(self._cards)
        self._no_templates_label.setVisible(not has_cards)
        self._apply_role_filter()
        self._rerender_all()

    def _apply_role_filter(self) -> None:
        """Show/hide cards based on the active role filter."""
        for card in self._cards:
            visible = (
                self._active_role is None
                or card.template.role == self._active_role
            )
            card.setVisible(visible)

    def _rerender_all(self) -> None:
        """Re-render thumbnails for all currently visible cards."""
        if self._app_config is None:
            return
        for card in self._cards:
            if not card.isVisible():
                continue
            self._render_card(card)

    def _render_card(self, card: _ThumbnailCard) -> None:
        """Render one thumbnail synchronously and update the card."""
        if self._app_config is None:
            return
        from open_sstv.core.modes import MODE_TABLE, Mode

        mode = self._mode or Mode("scottie_s1")
        spec = MODE_TABLE[mode]
        ctx = TXContext(
            mode_display_name=mode.value,
            frame_size=(spec.width, spec.display_height),
            photo_image=self._photo,
        )
        try:
            img = render_template(
                card.template, self._qso_state, self._app_config, ctx
            )
            pix = pil_to_pixmap(img)
            card.set_pixmap(pix)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Thumbnail render failed for '%s': %s", card.template.name, exc
            )


__all__ = ["TemplateGallery"]
