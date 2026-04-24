# SPDX-License-Identifier: GPL-3.0-or-later
"""Data model for the v0.3 template compositor.

All layer positions and sizes are stored as percentages of the frame
dimensions (0.0–100.0) so a single template definition renders correctly
at any SSTV mode's resolution.

Anchor system
─────────────
Each layer has an ``anchor`` that specifies which corner/edge of the
*layer's bounding box* aligns to the corresponding point on the canvas:

    TL ── TC ── TR
    │            │
    CL ── C ─── CR
    │            │
    BL ── BC ── BR

``FILL`` stretches the layer to cover the entire canvas regardless of
size fields.

``offset_x_pct`` / ``offset_y_pct`` move the layer *inward* from the
anchor edge: positive offset_x on a right-side anchor moves left, on a
left-side anchor moves right, on a center anchor moves right.  Positive
offset_y on a top anchor moves down, on a bottom anchor moves up, on a
center anchor moves down.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    import PIL.Image

# ---------------------------------------------------------------------------
# Primitive types
# ---------------------------------------------------------------------------

RGBA = tuple[int, int, int, int]  # (R, G, B, A), each 0–255

Anchor = Literal["TL", "TC", "TR", "CL", "C", "CR", "BL", "BC", "BR", "FILL"]
ANCHORS: frozenset[str] = frozenset(get_args(Anchor))


@dataclass(frozen=True)
class StrokeSpec:
    """Outline drawn around text glyphs."""

    color: RGBA
    width_px: int


@dataclass(frozen=True)
class ShadowSpec:
    """Drop-shadow offset and optional Gaussian blur."""

    offset_x: float
    offset_y: float
    color: RGBA
    blur_px: float = 0.0


# ---------------------------------------------------------------------------
# Layer base
# ---------------------------------------------------------------------------


@dataclass
class LayerBase:
    """Fields shared by every layer type."""

    id: str
    name: str = ""
    visible: bool = True
    opacity: float = 1.0
    anchor: Anchor = "TL"
    offset_x_pct: float = 0.0
    offset_y_pct: float = 0.0
    width_pct: float | None = None
    height_pct: float | None = None
    rotation_deg: float = 0.0


# ---------------------------------------------------------------------------
# Concrete layer types
# ---------------------------------------------------------------------------


@dataclass
class PhotoLayer(LayerBase):
    """The user-selected TX photo, composited as the base image."""

    type: str = field(default="photo", init=False)
    fit: Literal["contain", "cover", "stretch"] = "cover"


@dataclass
class RxImageLayer(LayerBase):
    """Most-recently-received image from the RX panel.

    Falls back to transparent when no RX image is available.
    """

    type: str = field(default="rx_image", init=False)
    fit: Literal["contain", "cover", "stretch"] = "cover"


@dataclass
class StationImageLayer(LayerBase):
    """Fixed image from Settings (QSL card, station photo).

    ``path`` is relative to ``{user_config_dir}/open_sstv/assets/``.
    """

    type: str = field(default="station_image", init=False)
    path: str = ""
    fit: Literal["contain", "cover", "stretch"] = "contain"


@dataclass
class TextLayer(LayerBase):
    """Text overlay with optional stroke, shadow, and stacked-vertical mode.

    ``slashed_zero=True`` (default) replaces ASCII ``0`` with ``Ø`` in
    callsign-valued tokens so W0AEZ displays as WØAEZ — a common amateur
    radio convention that distinguishes zero from the letter O.

    ``orientation="stacked"`` renders characters letter-over-letter (not
    rotated), suitable for vertical callsign banners.
    """

    type: str = field(default="text", init=False)
    text_raw: str = ""
    font_family: str = "DejaVu Sans Bold"
    font_size_pct: float = 6.0
    weight: Literal["regular", "bold"] = "regular"
    italic: bool = False
    fill: RGBA = (255, 255, 255, 255)
    stroke: StrokeSpec | None = None
    shadow: ShadowSpec | None = None
    align: Literal["left", "center", "right"] = "left"
    line_height_mult: float = 1.0
    slashed_zero: bool = True
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"
    orientation: Literal["horizontal", "stacked"] = "horizontal"


@dataclass
class RectLayer(LayerBase):
    """Solid or semi-transparent rectangle — used for banner strips."""

    type: str = field(default="rect", init=False)
    fill: RGBA = (0, 0, 0, 200)


@dataclass
class GradientLayer(LayerBase):
    """Two-stop linear gradient.

    ``angle_deg``: 0 = left→right, 90 = top→bottom, 180 = right→left,
    270 = bottom→top.
    """

    type: str = field(default="gradient", init=False)
    from_color: RGBA = (0, 0, 0, 255)
    to_color: RGBA = (0, 0, 0, 0)
    angle_deg: float = 90.0


@dataclass
class PatternLayer(LayerBase):
    """Tiled pattern from the built-in library.

    Built-in ``pattern_id`` values: ``"checkered"``, ``"diagonal_stripes"``,
    ``"dots"``.
    """

    type: str = field(default="pattern", init=False)
    pattern_id: str = "checkered"
    tint: RGBA = (255, 255, 255, 128)
    cell_size_pct: float = 2.0


# Discriminated union — exhaustive over all layer types.
Layer = (
    PhotoLayer
    | RxImageLayer
    | StationImageLayer
    | TextLayer
    | RectLayer
    | GradientLayer
    | PatternLayer
)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


@dataclass
class Template:
    """A stack of layers that the renderer composites into a TX image.

    ``reference_frame`` is the design canvas the template was authored at
    (default 320×256, the 5:4 Scottie/Martin frame).  The renderer
    re-scales all percentage coordinates to whatever mode is selected at TX
    time, so the same template works at 320×240 (Robot 36) or 640×496
    (PD-120) without editing.

    ``schema_version=1`` for all v0.3 templates.
    """

    name: str
    role: Literal["cq", "reply", "closing", "custom"] = "custom"
    reference_frame: tuple[int, int] = (320, 256)
    schema_version: int = 1
    description: str = ""
    layers: list[Layer] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Render-time context objects
# ---------------------------------------------------------------------------


@dataclass
class QSOState:
    """Per-QSO dynamic fields populated by the QSO State widget.

    All fields default to empty/sentinel values so the renderer can be
    called without a live QSO in progress (e.g., gallery thumbnails).
    """

    tocall: str = ""
    rst: str = "595"
    tocall_name: str = ""
    note: str = ""
    serial: int = 0


@dataclass
class TXContext:
    """Everything the renderer needs that isn't in the template or QSOState.

    ``photo_image``: the user's selected TX photo (PIL RGBA or RGB).
    ``rx_image``: the most recently decoded RX image, or ``None``.
    ``frequency_hz``: current rig frequency; ``None`` when no rig is
      connected (causes ``{freq}`` / ``{band}`` tokens to resolve blank).
    """

    mode_display_name: str = ""
    frame_size: tuple[int, int] = (320, 256)
    frequency_hz: float | None = None
    photo_image: "PIL.Image.Image | None" = None
    rx_image: "PIL.Image.Image | None" = None


__all__ = [
    "ANCHORS",
    "RGBA",
    "Anchor",
    "GradientLayer",
    "Layer",
    "LayerBase",
    "PatternLayer",
    "PhotoLayer",
    "QSOState",
    "RectLayer",
    "RxImageLayer",
    "ShadowSpec",
    "StationImageLayer",
    "StrokeSpec",
    "TXContext",
    "Template",
    "TextLayer",
]
