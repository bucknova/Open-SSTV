# SPDX-License-Identifier: GPL-3.0-or-later
"""Template Gallery widget for the TX panel.

Displays a scrollable grid of template thumbnails.  Each card shows a
live-rendered composite (photo + template + QSO state) scaled to up to
140 px wide with the template name below.  A role filter above narrows
to CQ / Reply / 73 / Custom.

Cards wrap left-to-right into rows (_FlowLayout) and scroll vertically
when the content exceeds the available height.

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

from PySide6.QtCore import QPoint, QRect, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
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

# Thumbnail width bounds (pixels).  Actual width is computed per mode aspect.
_MAX_THUMB_W: int = 140
_MIN_THUMB_W: int = 60
_THUMB_W: int = _MAX_THUMB_W  # kept for backward-compat with existing tests

# Role labels shown in the filter bar.
_ROLE_LABELS: tuple[tuple[str, str | None], ...] = (
    ("All", None),
    ("CQ", "cq"),
    ("Reply", "reply"),
    ("73", "closing"),
    ("Custom", "custom"),
)


class _FlowLayout(QLayout):
    """Left-to-right wrapping layout — items flow into new rows on overflow."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._spacing = spacing
        self._items: list = []

    # --- QLayout pure-virtual interface ---

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        m = self.contentsMargins()
        w = max((it.minimumSize().width() for it in self._items), default=0)
        return QSize(w + m.left() + m.right(), m.top() + m.bottom())

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    # --- layout pass ---

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        row_h = 0
        first_in_row = True

        for item in self._items:
            w = item.widget()
            if w is not None and not w.isVisible():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width()
            if not first_in_row and next_x > right:
                # Wrap to a new row.
                x = rect.x() + m.left()
                y += row_h + self._spacing
                row_h = 0
                next_x = x + hint.width()
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + self._spacing
            row_h = max(row_h, hint.height())
            first_in_row = False

        return y + row_h - rect.y() + m.bottom()


class _ThumbnailCard(QWidget):
    """One card in the gallery: thumbnail image + name label."""

    clicked = Signal(object)  # Template

    def __init__(self, template: Template, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._template = template
        self._selected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 8)  # extra bottom margin for descenders
        layout.setSpacing(4)
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
        self._name_label = QLabel(template.name)
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setFixedWidth(_THUMB_W)
        self._name_label.setMinimumHeight(32)
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet(
            "QLabel { font-size: 9px; padding: 2px 0px 6px 0px; }"
        )
        layout.addWidget(self._name_label)

        self._set_border()

    # --- public ---

    def set_pixmap(self, pix: QPixmap, thumb_w: int = _THUMB_W) -> None:
        """Update the thumbnail image, resizing labels to *thumb_w*."""
        scaled = pix.scaledToWidth(thumb_w, Qt.TransformationMode.SmoothTransformation)
        self._thumb_label.setFixedWidth(thumb_w)
        self._thumb_label.setFixedHeight(scaled.height())
        self._thumb_label.setPixmap(scaled)
        self._name_label.setFixedWidth(thumb_w)

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
    """Wrapping grid of template thumbnails with role filter.

    Cards flow left-to-right and wrap into new rows; a vertical scrollbar
    appears when content exceeds the widget height.

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

        # Debounce re-renders triggered by widget resize.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._rerender_all)

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

        # --- Scroll area with wrapping card grid ---
        self._scroll = QScrollArea()
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMinimumHeight(180)

        self._strip_widget = QWidget()
        self._strip_layout = _FlowLayout(self._strip_widget, spacing=8)
        self._strip_layout.setContentsMargins(4, 4, 4, 4)
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
        """Reload the templates directory and rebuild the grid."""
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
        for card in self._cards:
            self._strip_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

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
        self._strip_layout.invalidate()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_timer.start()

    def _compute_thumb_w(self, frame_w: int, frame_h: int) -> int:
        """Return the ideal thumbnail width for the given frame aspect ratio."""
        max_h = 130  # cap thumbnail height so cards stay compact in the grid
        by_aspect = int(max_h * frame_w / frame_h) if frame_h else _MAX_THUMB_W
        return max(_MIN_THUMB_W, min(_MAX_THUMB_W, by_aspect))

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
        thumb_w = self._compute_thumb_w(spec.width, spec.display_height)
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
            card.set_pixmap(pix, thumb_w)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Thumbnail render failed for '%s': %s", card.template.name, exc
            )


__all__ = ["TemplateGallery"]
