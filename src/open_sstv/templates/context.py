# SPDX-License-Identifier: GPL-3.0-or-later
"""The one place a render-time ``TXContext`` is built.

v0.6.14 audit C-M3 / C-M4: TX, the template editor preview, the gallery
thumbnails and the remote compose path each built their own
``TXContext``.  Only TX pre-cropped the photo to the frame, so a
``contain`` or sized photo layer looked one way in every preview and
another way on the air; and none of them passed the rig frequency, so
``%f`` / ``{freq}`` / ``%b`` / ``{band}`` always rendered blank.  Every
caller now goes through :func:`build_tx_context`, which applies a single
photo policy and carries ``frequency_hz``.

Photo policy
────────────
The renderer already fits the photo into each photo layer's box
(``cover`` / ``contain`` / ``stretch``), so a pre-crop is only *safe* when
it can't change the result: every visible photo layer covers the whole
frame with ``fit = "cover"``.  There the frame-aspect centre crop is
exactly what the renderer would do anyway, and doing it up front just
shrinks a 12 MP phone photo once instead of per layer.  For anything
else (``contain`` letterboxing, ``stretch``, a sized or offset photo
box) the photo goes to the renderer untouched and its fit decides —
identically in every preview and on the air.

Qt-free, so the remote compose path can use it on a request thread.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image

from open_sstv.core.modes import MODE_TABLE, Mode
from open_sstv.templates.model import PhotoLayer, Template, TXContext

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


def center_crop_to_size(
    photo: PILImage, target_w: int, target_h: int
) -> PILImage:
    """Center-crop *photo* to ``(target_w, target_h)``'s aspect ratio,
    then LANCZOS-resize to those exact dimensions.  Returns *photo*
    unchanged when its size already matches.

    The original image is never mutated — this returns a new
    ``PIL.Image`` whenever a crop or resize is applied.
    """
    if photo.size == (target_w, target_h):
        return photo

    src_w, src_h = photo.size
    target_aspect = target_w / target_h
    src_aspect = src_w / src_h

    if src_aspect > target_aspect:
        # Source is wider than target — crop horizontal margins.
        new_w = int(round(src_h * target_aspect))
        x_off = (src_w - new_w) // 2
        cropped = photo.crop((x_off, 0, x_off + new_w, src_h))
    elif src_aspect < target_aspect:
        # Source is taller than target — crop vertical margins.
        new_h = int(round(src_w / target_aspect))
        y_off = (src_h - new_h) // 2
        cropped = photo.crop((0, y_off, src_w, y_off + new_h))
    else:
        cropped = photo

    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)


def _is_full_frame(layer: PhotoLayer) -> bool:
    if layer.anchor == "FILL":
        return True
    full_w = layer.width_pct is None or layer.width_pct == 100.0
    full_h = layer.height_pct is None or layer.height_pct == 100.0
    return (
        full_w and full_h
        and layer.offset_x_pct == 0.0 and layer.offset_y_pct == 0.0
    )


def needs_photo_precrop(template: Template | None) -> bool:
    """True when pre-cropping the photo to the frame can't change the
    render: every visible photo layer is full-frame ``cover``.

    ``None`` (no template) also pre-crops — nothing composites the photo
    then, and callers only use the result for previews.
    """
    if template is None:
        return True
    photo_layers = [
        layer for layer in template.layers
        if isinstance(layer, PhotoLayer) and layer.visible
    ]
    return all(
        layer.fit == "cover" and _is_full_frame(layer) for layer in photo_layers
    )


def build_tx_context(
    template: Template | None,
    mode: Mode,
    photo: PILImage | None,
    rx_image: PILImage | None = None,
    *,
    frequency_hz: float | None = None,
) -> TXContext:
    """Build the render context for *template* at *mode*.

    Used by TX, the template editor, the gallery and remote compose, so
    every preview renders exactly what transmits.  ``frequency_hz`` is the
    rig's last polled frequency (``None`` with no rig connected), which
    drives the ``%f`` / ``{freq}`` / ``%b`` / ``{band}`` tokens.
    """
    spec = MODE_TABLE[mode]
    frame_w, frame_h = spec.width, spec.display_height
    if photo is not None and needs_photo_precrop(template):
        photo = center_crop_to_size(photo, frame_w, frame_h)
    if frequency_hz is not None and frequency_hz <= 0:
        frequency_hz = None
    return TXContext(
        mode_display_name=mode.value,
        frame_size=(frame_w, frame_h),
        frequency_hz=frequency_hz,
        photo_image=photo,
        rx_image=rx_image,
    )


__all__ = ["build_tx_context", "center_crop_to_size", "needs_photo_precrop"]
