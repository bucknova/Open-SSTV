# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-function template renderer for the v0.3 compositor.

Entry point
───────────
``render_template(template, qso_state, app_config, tx_context) → PIL.Image``

The function is intentionally side-effect-free:

- No Qt imports.
- No filesystem reads (fonts are resolved via the ``fonts`` module before
  entering the render loop; image data arrives via TXContext).
- No clock access (now_utc defaults to ``datetime.datetime.now(utc)`` but
  callers may inject a fixed time for deterministic tests).

Layer compositing
─────────────────
Layers are rendered bottom→top onto an RGBA canvas at the target mode's
frame size.  Each layer is blended with ``Image.alpha_composite`` so
per-layer opacity is honoured correctly without special-casing.

Anchor / positioning
────────────────────
See ``model.py`` docstring for the full anchor semantics.  Summary:

    offset_x/y always inward from the anchor edge:
    - Left anchors (TL/CL/BL): positive x → right
    - Right anchors (TR/CR/BR): positive x → left
    - Center anchors (TC/C/BC): positive x → right
    - Top anchors (TL/TC/TR): positive y → down
    - Bottom anchors (BL/BC/BR): positive y → up
    - Center anchors (CL/C/CR): positive y → down
"""
from __future__ import annotations

import datetime
import math
from pathlib import Path
from typing import TYPE_CHECKING

import PIL.Image
import PIL.ImageDraw
import PIL.ImageFilter
import PIL.ImageFont

from open_sstv.templates.fonts import resolve_font_path
from open_sstv.templates.model import (
    GradientLayer,
    Layer,
    LayerBase,
    PatternLayer,
    PhotoLayer,
    QSOState,
    RectLayer,
    RxImageLayer,
    StationImageLayer,
    TXContext,
    Template,
    TextLayer,
)
from open_sstv.templates.tokens import resolve_text

if TYPE_CHECKING:
    from open_sstv.config.schema import AppConfig

# ---------------------------------------------------------------------------
# Anchor geometry helpers
# ---------------------------------------------------------------------------


def _anchor_top_left(
    anchor: str,
    offset_x_pct: float,
    offset_y_pct: float,
    bbox_w: int,
    bbox_h: int,
    canvas_w: int,
    canvas_h: int,
) -> tuple[int, int]:
    """Compute the top-left pixel where a layer should be pasted.

    Returns
    -------
    tuple[int, int]
        (x, y) top-left corner of the layer bounding box on the canvas.
    """
    if anchor == "FILL":
        return (0, 0)

    ox = offset_x_pct / 100.0 * canvas_w
    oy = offset_y_pct / 100.0 * canvas_h

    if anchor in ("TL", "CL", "BL"):
        x = ox
    elif anchor in ("TC", "C", "BC"):
        x = canvas_w / 2.0 + ox - bbox_w / 2.0
    else:  # TR, CR, BR
        x = canvas_w - ox - bbox_w

    if anchor in ("TL", "TC", "TR"):
        y = oy
    elif anchor in ("CL", "C", "CR"):
        y = canvas_h / 2.0 + oy - bbox_h / 2.0
    else:  # BL, BC, BR
        y = canvas_h - oy - bbox_h

    return int(round(x)), int(round(y))


def _layer_bbox(
    layer: LayerBase, canvas_w: int, canvas_h: int
) -> tuple[int, int]:
    """Return (width, height) in pixels for a layer, respecting width/height_pct."""
    w = int((layer.width_pct / 100.0) * canvas_w) if layer.width_pct is not None else canvas_w
    h = int((layer.height_pct / 100.0) * canvas_h) if layer.height_pct is not None else canvas_h
    return max(1, w), max(1, h)


# ---------------------------------------------------------------------------
# Image fit helpers
# ---------------------------------------------------------------------------


def _fit_image(
    img: PIL.Image.Image,
    bbox_w: int,
    bbox_h: int,
    fit: str,
) -> PIL.Image.Image:
    """Scale/crop *img* to fit *bbox* according to *fit* mode.

    Returns an RGBA image exactly (bbox_w × bbox_h).
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    if fit == "stretch":
        return img.resize((bbox_w, bbox_h), PIL.Image.LANCZOS)

    iw, ih = img.size
    if fit == "contain":
        ratio = min(bbox_w / iw, bbox_h / ih)
    else:  # "cover"
        ratio = max(bbox_w / iw, bbox_h / ih)

    new_w = max(1, int(round(iw * ratio)))
    new_h = max(1, int(round(ih * ratio)))
    resized = img.resize((new_w, new_h), PIL.Image.LANCZOS)

    if fit == "cover":
        cx = (new_w - bbox_w) // 2
        cy = (new_h - bbox_h) // 2
        resized = resized.crop((cx, cy, cx + bbox_w, cy + bbox_h))
        return resized

    # contain: letterbox / pillarbox with transparency
    out = PIL.Image.new("RGBA", (bbox_w, bbox_h), (0, 0, 0, 0))
    px = (bbox_w - new_w) // 2
    py = (bbox_h - new_h) // 2
    out.paste(resized, (px, py), resized)
    return out


# ---------------------------------------------------------------------------
# Pattern generation
# ---------------------------------------------------------------------------


def _make_pattern_tile(pattern_id: str, cell_px: int) -> PIL.Image.Image:
    """Return a small tileable RGBA image for the given pattern id."""
    size = max(2, cell_px)
    tile = PIL.Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(tile)

    if pattern_id == "checkered":
        draw.rectangle([0, 0, size - 1, size - 1], fill=(255, 255, 255, 255))
        draw.rectangle([size, size, size * 2 - 1, size * 2 - 1], fill=(255, 255, 255, 255))
    elif pattern_id == "diagonal_stripes":
        for i in range(size * 4):
            if (i // size) % 2 == 0:
                draw.line([(i - size * 2, 0), (i, size * 2)], fill=(255, 255, 255, 255))
    elif pattern_id == "dots":
        r = max(1, size // 3)
        cx, cy = size // 2, size // 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
        cx2, cy2 = size + size // 2, size + size // 2
        draw.ellipse([cx2 - r, cy2 - r, cx2 + r, cy2 + r], fill=(255, 255, 255, 255))

    return tile


def _tile_pattern(tile: PIL.Image.Image, bbox_w: int, bbox_h: int) -> PIL.Image.Image:
    """Tile *tile* to cover a (bbox_w × bbox_h) region."""
    out = PIL.Image.new("RGBA", (bbox_w, bbox_h), (0, 0, 0, 0))
    tw, th = tile.size
    for y in range(0, bbox_h, th):
        for x in range(0, bbox_w, tw):
            out.paste(tile, (x, y), tile)
    return out


# ---------------------------------------------------------------------------
# Gradient generation
# ---------------------------------------------------------------------------


def _make_gradient(
    bbox_w: int,
    bbox_h: int,
    from_color: tuple[int, int, int, int],
    to_color: tuple[int, int, int, int],
    angle_deg: float,
) -> PIL.Image.Image:
    """Generate a two-stop linear gradient image."""
    import numpy as np

    angle_rad = math.radians(angle_deg % 360)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    # Normalised gradient coordinate for each pixel [0, 1]
    xs = np.linspace(0, 1, bbox_w, dtype=np.float32)
    ys = np.linspace(0, 1, bbox_h, dtype=np.float32)
    xv, yv = np.meshgrid(xs, ys)
    # Project onto gradient direction; shift so range is [0, 1]
    t = cos_a * xv + sin_a * yv
    t_min, t_max = t.min(), t.max()
    if t_max > t_min:
        t = (t - t_min) / (t_max - t_min)
    else:
        t = np.zeros_like(t)

    # Interpolate RGBA channels
    arr = np.zeros((bbox_h, bbox_w, 4), dtype=np.uint8)
    for ch in range(4):
        arr[:, :, ch] = (from_color[ch] * (1 - t) + to_color[ch] * t).astype(np.uint8)

    return PIL.Image.fromarray(arr)


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _load_font(
    font_family: str, font_size_px: int, user_fonts_dir: Path | None = None
) -> PIL.ImageFont.FreeTypeFont:
    path = resolve_font_path(font_family, user_fonts_dir=user_fonts_dir)
    return PIL.ImageFont.truetype(str(path), font_size_px)


def _text_bbox(
    font: PIL.ImageFont.FreeTypeFont, text: str
) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) bounding box for *text*."""
    # Pillow ≥9.2 uses getbbox; older uses getsize.
    if hasattr(font, "getbbox"):
        return font.getbbox(text)
    w, h = font.getsize(text)  # type: ignore[attr-defined]
    return (0, 0, w, h)


def _render_horizontal_text(
    layer: TextLayer,
    resolved: str,
    canvas_w: int,
    canvas_h: int,
    font: PIL.ImageFont.FreeTypeFont,
) -> tuple[PIL.Image.Image, int, int]:
    """Render resolved text horizontally, returning (image, x, y) on canvas."""
    stroke_w = layer.stroke.width_px if layer.stroke else 0
    shadow = layer.shadow

    # Measure bounding box (single or multi-line)
    lines = resolved.split("\n")
    line_bboxes = [_text_bbox(font, ln) for ln in lines]
    line_heights = [bb[3] - bb[1] for bb in line_bboxes]
    line_widths = [bb[2] - bb[0] for bb in line_bboxes]

    line_h_px = max(line_heights) if line_heights else 1
    spacing = int(line_h_px * layer.line_height_mult)
    total_h = spacing * (len(lines) - 1) + line_h_px + stroke_w * 2
    total_w = max(line_widths) if line_widths else 1

    # Add shadow padding
    shadow_pad_x = int(abs(shadow.offset_x)) + int(shadow.blur_px) if shadow else 0
    shadow_pad_y = int(abs(shadow.offset_y)) + int(shadow.blur_px) if shadow else 0
    pad_left = max(0, -int(shadow.offset_x if shadow else 0)) + shadow_pad_x
    pad_top = max(0, -int(shadow.offset_y if shadow else 0)) + shadow_pad_y
    pad_right = max(0, int(shadow.offset_x if shadow else 0)) + shadow_pad_x
    pad_bottom = max(0, int(shadow.offset_y if shadow else 0)) + shadow_pad_y

    img_w = total_w + stroke_w * 2 + pad_left + pad_right
    img_h = total_h + pad_top + pad_bottom
    img = PIL.Image.new("RGBA", (max(1, img_w), max(1, img_h)), (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(img)

    text_x0 = pad_left + stroke_w
    text_y0 = pad_top + stroke_w

    for i, (line, lw) in enumerate(zip(lines, line_widths)):
        y = text_y0 + i * spacing
        if layer.align == "left":
            x = text_x0
        elif layer.align == "center":
            x = text_x0 + (total_w - lw) // 2
        else:  # right
            x = text_x0 + total_w - lw

        # Shadow pass
        if shadow:
            sx = x + shadow.offset_x
            sy = y + shadow.offset_y
            s_img = PIL.Image.new("RGBA", img.size, (0, 0, 0, 0))
            s_draw = PIL.ImageDraw.Draw(s_img)
            s_draw.text((sx, sy), line, font=font, fill=shadow.color)
            if shadow.blur_px > 0:
                s_img = s_img.filter(
                    PIL.ImageFilter.GaussianBlur(radius=shadow.blur_px)
                )
            img = PIL.Image.alpha_composite(img, s_img)
            draw = PIL.ImageDraw.Draw(img)

        # Main text with optional stroke
        stroke_fill = layer.stroke.color if layer.stroke else None
        draw.text(
            (x, y),
            line,
            font=font,
            fill=layer.fill,
            stroke_width=stroke_w,
            stroke_fill=stroke_fill,
        )

    # Anchor text image onto canvas
    bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
    cx, cy = _anchor_top_left(
        layer.anchor,
        layer.offset_x_pct,
        layer.offset_y_pct,
        img_w,
        img_h,
        canvas_w,
        canvas_h,
    )
    return img, cx, cy


def _render_stacked_text(
    layer: TextLayer,
    resolved: str,
    canvas_w: int,
    canvas_h: int,
    font: PIL.ImageFont.FreeTypeFont,
) -> tuple[PIL.Image.Image, int, int]:
    """Render stacked (letter-over-letter) vertical text."""
    stroke_w = layer.stroke.width_px if layer.stroke else 0
    chars = list(resolved)
    if not chars:
        chars = [" "]

    char_bboxes = [_text_bbox(font, c) for c in chars]
    char_w = max(bb[2] - bb[0] for bb in char_bboxes)
    char_h = max(bb[3] - bb[1] for bb in char_bboxes)
    spacing = int(char_h * layer.line_height_mult)
    total_h = spacing * (len(chars) - 1) + char_h + stroke_w * 2
    total_w = char_w + stroke_w * 2

    img = PIL.Image.new("RGBA", (max(1, total_w), max(1, total_h)), (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(img)

    for i, ch in enumerate(chars):
        y = stroke_w + i * spacing
        x = stroke_w
        stroke_fill = layer.stroke.color if layer.stroke else None
        draw.text(
            (x, y),
            ch,
            font=font,
            fill=layer.fill,
            stroke_width=stroke_w,
            stroke_fill=stroke_fill,
        )

    cx, cy = _anchor_top_left(
        layer.anchor,
        layer.offset_x_pct,
        layer.offset_y_pct,
        total_w,
        total_h,
        canvas_w,
        canvas_h,
    )
    return img, cx, cy


# ---------------------------------------------------------------------------
# Per-layer rasterizers
# ---------------------------------------------------------------------------


def _rasterize_photo(
    layer: PhotoLayer,
    canvas_w: int,
    canvas_h: int,
    photo: PIL.Image.Image | None,
) -> PIL.Image.Image | None:
    if photo is None:
        return None
    if layer.anchor == "FILL":
        bbox_w, bbox_h = canvas_w, canvas_h
    else:
        bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
    fitted = _fit_image(photo, bbox_w, bbox_h, layer.fit)
    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    if layer.anchor == "FILL":
        x, y = 0, 0
    else:
        x, y = _anchor_top_left(
            layer.anchor, layer.offset_x_pct, layer.offset_y_pct,
            bbox_w, bbox_h, canvas_w, canvas_h,
        )
    cell.paste(fitted, (x, y), fitted)
    return _apply_opacity(cell, layer.opacity)


def _rasterize_image_layer(
    layer: "RxImageLayer | StationImageLayer",
    canvas_w: int,
    canvas_h: int,
    img: PIL.Image.Image | None,
) -> PIL.Image.Image | None:
    if img is None:
        return None
    if layer.anchor == "FILL":
        bbox_w, bbox_h = canvas_w, canvas_h
    else:
        bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
    fit = getattr(layer, "fit", "contain")
    fitted = _fit_image(img, bbox_w, bbox_h, fit)
    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    if layer.anchor == "FILL":
        x, y = 0, 0
    else:
        x, y = _anchor_top_left(
            layer.anchor, layer.offset_x_pct, layer.offset_y_pct,
            bbox_w, bbox_h, canvas_w, canvas_h,
        )
    cell.paste(fitted, (x, y), fitted)
    return _apply_opacity(cell, layer.opacity)


def _rasterize_rect(
    layer: RectLayer, canvas_w: int, canvas_h: int
) -> PIL.Image.Image:
    if layer.anchor == "FILL":
        bbox_w, bbox_h = canvas_w, canvas_h
        x, y = 0, 0
    else:
        bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
        x, y = _anchor_top_left(
            layer.anchor, layer.offset_x_pct, layer.offset_y_pct,
            bbox_w, bbox_h, canvas_w, canvas_h,
        )
    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(cell)
    draw.rectangle([x, y, x + bbox_w - 1, y + bbox_h - 1], fill=layer.fill)
    return _apply_opacity(cell, layer.opacity)


def _rasterize_gradient(
    layer: GradientLayer, canvas_w: int, canvas_h: int
) -> PIL.Image.Image:
    if layer.anchor == "FILL":
        bbox_w, bbox_h = canvas_w, canvas_h
        x, y = 0, 0
    else:
        bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
        x, y = _anchor_top_left(
            layer.anchor, layer.offset_x_pct, layer.offset_y_pct,
            bbox_w, bbox_h, canvas_w, canvas_h,
        )
    grad_img = _make_gradient(bbox_w, bbox_h, layer.from_color, layer.to_color, layer.angle_deg)
    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cell.paste(grad_img, (x, y), grad_img)
    return _apply_opacity(cell, layer.opacity)


def _rasterize_pattern(
    layer: PatternLayer, canvas_w: int, canvas_h: int
) -> PIL.Image.Image:
    cell_px = max(2, int(layer.cell_size_pct / 100.0 * min(canvas_w, canvas_h)))
    tile = _make_pattern_tile(layer.pattern_id, cell_px)

    if layer.anchor == "FILL":
        bbox_w, bbox_h = canvas_w, canvas_h
        x, y = 0, 0
    else:
        bbox_w, bbox_h = _layer_bbox(layer, canvas_w, canvas_h)
        x, y = _anchor_top_left(
            layer.anchor, layer.offset_x_pct, layer.offset_y_pct,
            bbox_w, bbox_h, canvas_w, canvas_h,
        )

    tiled = _tile_pattern(tile, bbox_w, bbox_h)

    # Apply tint: multiply pattern alpha by tint RGBA
    tint = layer.tint
    tinted = PIL.Image.new("RGBA", (bbox_w, bbox_h), (0, 0, 0, 0))
    tr, tg, tb, ta = tint
    for px_x in range(bbox_w):
        for px_y in range(bbox_h):
            r2, g2, b2, a2 = tiled.getpixel((px_x, px_y))
            tinted.putpixel(
                (px_x, px_y),
                (
                    r2 * tr // 255,
                    g2 * tg // 255,
                    b2 * tb // 255,
                    a2 * ta // 255,
                ),
            )

    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cell.paste(tinted, (x, y), tinted)
    return _apply_opacity(cell, layer.opacity)


def _wrap_text(
    font: PIL.ImageFont.FreeTypeFont, text: str, max_w: int
) -> str:
    """Break *text* at word boundaries so every line fits within *max_w* px."""
    if max_w <= 0:
        return text
    out_lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out_lines.append("")
            continue
        cur: list[str] = []
        for word in words:
            candidate = " ".join(cur + [word])
            w = _text_bbox(font, candidate)[2] - _text_bbox(font, candidate)[0]
            if w > max_w and cur:
                out_lines.append(" ".join(cur))
                cur = [word]
            else:
                cur.append(word)
        out_lines.append(" ".join(cur))
    return "\n".join(out_lines)


def _fit_text(
    layer: TextLayer,
    text: str,
    font: PIL.ImageFont.FreeTypeFont,
    font_size_px: int,
    canvas_w: int,
) -> tuple[str, PIL.ImageFont.FreeTypeFont]:
    """Return (text, font) adjusted so the text fits within *canvas_w*.

    Strategy (in order):
    1. If the text already fits, return unchanged.
    2. Shrink the font proportionally down to 50 % of the original size.
    3. If it still doesn't fit at the floor size, word-wrap.
    """
    stroke_w = layer.stroke.width_px if layer.stroke else 0
    avail_w = max(1, canvas_w - stroke_w * 2)
    min_size = max(1, font_size_px // 2)

    def _max_line_w(f: PIL.ImageFont.FreeTypeFont, t: str) -> int:
        return max(
            (_text_bbox(f, ln)[2] - _text_bbox(f, ln)[0]) for ln in t.split("\n")
        )

    mlw = _max_line_w(font, text)
    if mlw <= avail_w:
        return text, font

    # Proportional shrink — one font load.
    shrunk_size = max(min_size, int(font_size_px * avail_w / mlw))
    if shrunk_size < font_size_px:
        font = _load_font(layer.font_family, shrunk_size)
        mlw = _max_line_w(font, text)

    # Word-wrap fallback if still overflowing.
    if mlw > avail_w:
        text = _wrap_text(font, text, avail_w)

    return text, font


def _rasterize_text(
    layer: TextLayer,
    resolved_text: str,
    canvas_w: int,
    canvas_h: int,
) -> PIL.Image.Image:
    font_size_px = max(1, int(layer.font_size_pct / 100.0 * canvas_h))
    font = _load_font(layer.font_family, font_size_px)

    if layer.orientation == "stacked":
        text_img, x, y = _render_stacked_text(layer, resolved_text, canvas_w, canvas_h, font)
    else:
        resolved_text, font = _fit_text(layer, resolved_text, font, font_size_px, canvas_w)
        text_img, x, y = _render_horizontal_text(layer, resolved_text, canvas_w, canvas_h, font)

    cell = PIL.Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    cell.paste(text_img, (x, y), text_img)
    return _apply_opacity(cell, layer.opacity)


def _apply_opacity(img: PIL.Image.Image, opacity: float) -> PIL.Image.Image:
    """Scale alpha channel by *opacity* (0.0–1.0)."""
    if opacity >= 1.0:
        return img
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * max(0.0, min(1.0, opacity))))
    return PIL.Image.merge("RGBA", (r, g, b, a))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_template(
    template: Template,
    qso_state: QSOState,
    app_config: "AppConfig",
    tx_context: TXContext,
    *,
    now_utc: datetime.datetime | None = None,
) -> PIL.Image.Image:
    """Render *template* to a PIL RGB image at the mode's frame size.

    Parameters
    ----------
    template:
        The template data model (layers, name, role, …).
    qso_state:
        Per-QSO dynamic fields (ToCall, RST, etc.).
    app_config:
        User configuration (own callsign, grid, name, …).
    tx_context:
        TX-time context: frame size, mode name, rig frequency, photo/rx images.
    now_utc:
        Clock override for date/time tokens; defaults to ``datetime.now(utc)``.

    Returns
    -------
    PIL.Image.Image
        RGB image at ``tx_context.frame_size``.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

    W, H = tx_context.frame_size
    canvas = PIL.Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # Load station image once if any StationImageLayer references it
    _station_img_cache: dict[str, PIL.Image.Image] = {}

    for layer in template.layers:
        if not layer.visible:
            continue

        cell: PIL.Image.Image | None = None

        if isinstance(layer, PhotoLayer):
            cell = _rasterize_photo(layer, W, H, tx_context.photo_image)

        elif isinstance(layer, RxImageLayer):
            cell = _rasterize_image_layer(layer, W, H, tx_context.rx_image)

        elif isinstance(layer, StationImageLayer):
            if layer.path:
                if layer.path not in _station_img_cache:
                    try:
                        _station_img_cache[layer.path] = PIL.Image.open(layer.path)
                    except OSError:
                        _station_img_cache[layer.path] = None  # type: ignore[assignment]
                station_img = _station_img_cache.get(layer.path)
            else:
                station_img = None
            cell = _rasterize_image_layer(layer, W, H, station_img)

        elif isinstance(layer, TextLayer):
            resolved = resolve_text(
                layer.text_raw,
                qso_state,
                app_config,
                tx_context,
                slashed_zero=layer.slashed_zero,
                date_format=layer.date_format,
                time_format=layer.time_format,
                now_utc=now_utc,
            )
            if resolved:
                cell = _rasterize_text(layer, resolved, W, H)

        elif isinstance(layer, RectLayer):
            cell = _rasterize_rect(layer, W, H)

        elif isinstance(layer, GradientLayer):
            cell = _rasterize_gradient(layer, W, H)

        elif isinstance(layer, PatternLayer):
            cell = _rasterize_pattern(layer, W, H)

        if cell is not None:
            canvas = PIL.Image.alpha_composite(canvas, cell)

    return canvas.convert("RGB")


__all__ = ["render_template", "_fit_text", "_wrap_text"]
