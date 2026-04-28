# SPDX-License-Identifier: GPL-3.0-or-later
"""TOML round-trip serialisation for v0.3 image templates.

Format overview (see docs/design/v0.3_templates.md §10.2)
──────────────────────────────────────────────────────────
    [template]
    name = "CQSSTV"
    role = "cq"
    reference_frame = [320, 256]
    schema_version = 1
    description = "..."

    [[layer]]
    type = "photo"
    id = "base"
    anchor = "FILL"
    fit = "cover"

    [[layer]]
    type = "text"
    id = "banner_call"
    text = "%c"          # NB: "text" in TOML → text_raw in Python
    anchor = "TL"
    ...
    stroke = { color = [255, 255, 255, 255], width_px = 4 }

Design choices
──────────────
- ``text`` key in TOML maps to ``text_raw`` in TextLayer (matches the
  design-doc example and MMSSTV muscle-memory).
- RGBA values serialise as 4-element integer lists: ``[R, G, B, A]``.
- StrokeSpec / ShadowSpec serialise as TOML inline tables.
- Fields at their dataclass defaults are omitted on save (cleaner diffs).
- Unknown keys and unknown layer types are silently ignored (forward-compat).
- Schema-version gate: files with ``schema_version > 1`` are refused.
- Writes are atomic: ``.tmp`` sibling + ``os.replace``.
"""
from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

import tomli_w

from open_sstv.templates.model import (
    RGBA,
    GradientLayer,
    Layer,
    PatternLayer,
    PhotoLayer,
    RectLayer,
    RxImageLayer,
    ShadowSpec,
    StationImageLayer,
    StrokeSpec,
    Template,
    TextLayer,
)

_log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SchemaVersionError(ValueError):
    """Raised when a template file requires a newer schema version."""


class TemplateLoadError(ValueError):
    """Raised for malformed template files (missing required fields, etc.)."""


# ---------------------------------------------------------------------------
# TOML → Python helpers
# ---------------------------------------------------------------------------


def _rgba(lst: list[int]) -> RGBA:
    if len(lst) < 3:
        raise TemplateLoadError(f"RGBA list must have at least 3 elements, got {lst!r}")
    if len(lst) < 4:
        # A 3-element list is treated as fully opaque, but a template author
        # who wrote ``fill = [255, 128, 0]`` almost certainly forgot the alpha
        # channel rather than deliberately omitting it.  Surface a warning so
        # the surprise doesn't show up later as "why is my translucent layer
        # opaque?"
        _log.warning(
            "RGBA list %r has only %d elements; defaulting alpha to 255 "
            "(fully opaque). Add a 4th element to silence this warning.",
            lst, len(lst),
        )
    return (lst[0], lst[1], lst[2], lst[3] if len(lst) >= 4 else 255)


def _stroke(d: dict) -> StrokeSpec:
    return StrokeSpec(color=_rgba(d["color"]), width_px=int(d["width_px"]))


def _shadow(d: dict) -> ShadowSpec:
    return ShadowSpec(
        offset_x=float(d["offset_x"]),
        offset_y=float(d["offset_y"]),
        color=_rgba(d["color"]),
        blur_px=float(d.get("blur_px", 0.0)),
    )


def _base_kwargs(d: dict) -> dict:
    """Extract LayerBase constructor kwargs from a TOML layer dict."""
    kw: dict = {"id": d["id"]}
    for key in (
        "name", "visible", "opacity", "anchor",
        "offset_x_pct", "offset_y_pct", "width_pct",
        "height_pct", "rotation_deg",
    ):
        if key in d:
            kw[key] = d[key]
    return kw


def _coerce_reference_frame(
    raw: object, file_name: str
) -> tuple[int, int]:
    """Validate and coerce a TOML ``reference_frame`` value to ``(int, int)``.

    Accepts a 2-element sequence of ints or floats.  Floats are rounded
    (``round`` not ``int``) and surface a warning so a template author who
    typed ``reference_frame = [320.7, 256.3]`` notices that the field is
    integer-only — the previous ``int(…)`` path silently truncated to
    ``(320, 256)`` and shifted every percentage-based layer by sub-pixel
    amounts, which is the kind of bug nobody catches in review.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise TemplateLoadError(
            f"Template {file_name!r}: reference_frame must be a 2-element "
            f"list, got {raw!r}"
        )
    coerced: list[int] = []
    for i, v in enumerate(raw):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            # ``bool`` is a subclass of ``int``; reject explicitly so
            # ``reference_frame = [true, 256]`` doesn't load as ``(1, 256)``.
            raise TemplateLoadError(
                f"Template {file_name!r}: reference_frame[{i}] must be a "
                f"number, got {type(v).__name__} {v!r}"
            )
        if isinstance(v, float):
            _log.warning(
                "Template %r: reference_frame[%d] = %r is a float; "
                "rounding to %d. reference_frame is integer-pixel-only — "
                "use an int to silence this warning.",
                file_name, i, v, round(v),
            )
            coerced.append(round(v))
        else:
            coerced.append(v)
    if coerced[0] <= 0 or coerced[1] <= 0:
        raise TemplateLoadError(
            f"Template {file_name!r}: reference_frame must be positive, "
            f"got {tuple(coerced)!r}"
        )
    return (coerced[0], coerced[1])


def _validate_station_image_path(path: str) -> None:
    """Raise ``TemplateLoadError`` if *path* would escape the assets dir.

    StationImageLayer.path is documented as relative to
    ``{user_config_dir}/open_sstv/assets/``.  A malicious template could
    otherwise smuggle in absolute paths (``/etc/passwd``) or ``..`` segments
    to make the renderer open arbitrary files.  We reject both at load time
    so a hostile TOML never reaches ``PIL.Image.open`` — the renderer also
    re-checks ``is_relative_to(assets_dir)`` as a defense-in-depth layer.
    """
    if not path:
        return
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or PureWindowsPath(path).is_absolute():
        raise TemplateLoadError(
            f"StationImageLayer.path must be relative to the assets directory, "
            f"got absolute path {path!r}"
        )
    if any(part == ".." for part in p.parts):
        raise TemplateLoadError(
            f"StationImageLayer.path must not contain '..' components, got {path!r}"
        )


def _layer_from_dict(d: dict) -> Layer | None:
    """Deserialise one ``[[layer]]`` table into the appropriate Layer subclass.

    Returns ``None`` for unknown layer types (forward-compat — older installs
    silently skip layers added in future schema versions).
    """
    layer_type = d.get("type", "")
    try:
        base = _base_kwargs(d)
    except KeyError as exc:
        _log.warning("Layer missing required field %s — skipping", exc)
        return None

    # Security checks happen before the catch-all below so attacks fail loudly
    # rather than getting silently downgraded to a "skip with warning".
    if layer_type == "station_image":
        _validate_station_image_path(d.get("path", ""))

    try:
        if layer_type == "photo":
            return PhotoLayer(**base, fit=d.get("fit", "cover"))

        if layer_type == "rx_image":
            return RxImageLayer(**base, fit=d.get("fit", "cover"))

        if layer_type == "station_image":
            raw_path = d.get("path", "")
            return StationImageLayer(
                **base,
                path=raw_path,
                fit=d.get("fit", "contain"),
            )

        if layer_type == "text":
            return TextLayer(
                **base,
                text_raw=d.get("text", ""),
                font_family=d.get("font_family", "DejaVu Sans Bold"),
                font_size_pct=float(d.get("font_size_pct", 6.0)),
                weight=d.get("weight", "regular"),
                italic=bool(d.get("italic", False)),
                fill=_rgba(d.get("fill", [255, 255, 255, 255])),
                stroke=_stroke(d["stroke"]) if "stroke" in d else None,
                shadow=_shadow(d["shadow"]) if "shadow" in d else None,
                align=d.get("align", "left"),
                line_height_mult=float(d.get("line_height_mult", 1.0)),
                slashed_zero=bool(d.get("slashed_zero", True)),
                date_format=d.get("date_format", "%Y-%m-%d"),
                time_format=d.get("time_format", "%H:%M"),
                orientation=d.get("orientation", "horizontal"),
            )

        if layer_type == "rect":
            return RectLayer(**base, fill=_rgba(d.get("fill", [0, 0, 0, 200])))

        if layer_type == "gradient":
            return GradientLayer(
                **base,
                from_color=_rgba(d.get("from_color", [0, 0, 0, 255])),
                to_color=_rgba(d.get("to_color", [0, 0, 0, 0])),
                angle_deg=float(d.get("angle_deg", 90.0)),
            )

        if layer_type == "pattern":
            return PatternLayer(
                **base,
                pattern_id=d.get("pattern_id", "checkered"),
                tint=_rgba(d.get("tint", [255, 255, 255, 128])),
                cell_size_pct=float(d.get("cell_size_pct", 2.0)),
            )

    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Could not parse layer type %r: %s — skipping", layer_type, exc)
        return None

    _log.warning("Unknown layer type %r — skipping (forward-compat)", layer_type)
    return None


# ---------------------------------------------------------------------------
# Python → TOML helpers
# ---------------------------------------------------------------------------


def _layer_to_dict(layer: Layer) -> dict:
    """Serialise one Layer to a plain dict suitable for tomli_w."""
    d: dict = {
        "type": layer.type,
        "id": layer.id,
    }
    # Optional LayerBase fields — only emit if non-default
    if layer.name:
        d["name"] = layer.name
    if not layer.visible:
        d["visible"] = layer.visible
    if layer.opacity != 1.0:
        d["opacity"] = layer.opacity
    d["anchor"] = layer.anchor
    if layer.offset_x_pct != 0.0:
        d["offset_x_pct"] = layer.offset_x_pct
    if layer.offset_y_pct != 0.0:
        d["offset_y_pct"] = layer.offset_y_pct
    if layer.width_pct is not None:
        d["width_pct"] = layer.width_pct
    if layer.height_pct is not None:
        d["height_pct"] = layer.height_pct
    if layer.rotation_deg != 0.0:
        d["rotation_deg"] = layer.rotation_deg

    # Type-specific fields
    if isinstance(layer, (PhotoLayer, RxImageLayer)):
        d["fit"] = layer.fit

    elif isinstance(layer, StationImageLayer):
        d["path"] = layer.path
        d["fit"] = layer.fit

    elif isinstance(layer, TextLayer):
        d["text"] = layer.text_raw  # TOML key is "text", Python field is "text_raw"
        d["font_family"] = layer.font_family
        d["font_size_pct"] = layer.font_size_pct
        if layer.weight != "regular":
            d["weight"] = layer.weight
        if layer.italic:
            d["italic"] = layer.italic
        d["fill"] = list(layer.fill)
        if layer.stroke is not None:
            d["stroke"] = {
                "color": list(layer.stroke.color),
                "width_px": layer.stroke.width_px,
            }
        if layer.shadow is not None:
            sd: dict = {
                "offset_x": layer.shadow.offset_x,
                "offset_y": layer.shadow.offset_y,
                "color": list(layer.shadow.color),
            }
            if layer.shadow.blur_px != 0.0:
                sd["blur_px"] = layer.shadow.blur_px
            d["shadow"] = sd
        if layer.align != "left":
            d["align"] = layer.align
        if layer.line_height_mult != 1.0:
            d["line_height_mult"] = layer.line_height_mult
        d["slashed_zero"] = layer.slashed_zero
        if layer.date_format != "%Y-%m-%d":
            d["date_format"] = layer.date_format
        if layer.time_format != "%H:%M":
            d["time_format"] = layer.time_format
        if layer.orientation != "horizontal":
            d["orientation"] = layer.orientation

    elif isinstance(layer, RectLayer):
        d["fill"] = list(layer.fill)

    elif isinstance(layer, GradientLayer):
        d["from_color"] = list(layer.from_color)
        d["to_color"] = list(layer.to_color)
        d["angle_deg"] = layer.angle_deg

    elif isinstance(layer, PatternLayer):
        d["pattern_id"] = layer.pattern_id
        d["tint"] = list(layer.tint)
        if layer.cell_size_pct != 2.0:
            d["cell_size_pct"] = layer.cell_size_pct

    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_template(path: Path) -> Template:
    """Load a ``Template`` from a ``.toml`` file.

    Parameters
    ----------
    path:
        Path to the ``.toml`` file.

    Returns
    -------
    Template
        Fully populated Template dataclass.

    Raises
    ------
    SchemaVersionError
        If the file's ``schema_version`` exceeds ``CURRENT_SCHEMA_VERSION``.
    TemplateLoadError
        If the file is structurally invalid (missing required fields).
    OSError
        If the file cannot be opened.
    tomllib.TOMLDecodeError
        If the file contains invalid TOML.
    """
    with path.open("rb") as f:
        raw = tomllib.load(f)

    tpl_raw = raw.get("template", {})

    schema_version = int(tpl_raw.get("schema_version", 1))
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Template '{path.name}' requires schema_version={schema_version}, "
            f"but this version of Open-SSTV only supports up to "
            f"schema_version={CURRENT_SCHEMA_VERSION}. "
            f"Please upgrade Open-SSTV to use this template."
        )

    name = tpl_raw.get("name")
    if not name:
        raise TemplateLoadError(f"Template file {path.name!r} is missing the 'name' field.")

    ref_raw = tpl_raw.get("reference_frame", [320, 256])
    reference_frame = _coerce_reference_frame(ref_raw, path.name)

    layers: list[Layer] = []
    for layer_raw in raw.get("layer", []):
        layer = _layer_from_dict(layer_raw)
        if layer is not None:
            layers.append(layer)

    return Template(
        name=name,
        role=tpl_raw.get("role", "custom"),
        reference_frame=reference_frame,
        schema_version=schema_version,
        description=tpl_raw.get("description", ""),
        layers=layers,
    )


def save_template(template: Template, path: Path) -> None:
    """Serialise *template* to *path* atomically.

    Creates parent directories if needed. Writes via a ``.tmp`` sibling
    and ``os.replace`` so a mid-write crash never leaves a truncated file.

    Raises
    ------
    OSError
        If the directory cannot be created or the file cannot be written.
    """
    data: dict = {
        "template": {
            "name": template.name,
            "role": template.role,
            "reference_frame": list(template.reference_frame),
            "schema_version": template.schema_version,
            "description": template.description,
        },
        "layer": [_layer_to_dict(layer) for layer in template.layers],
    }

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        _log.error("Failed to save template to %s: %s", path, exc)
        tmp.unlink(missing_ok=True)
        raise


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SchemaVersionError",
    "TemplateLoadError",
    "load_template",
    "save_template",
]
