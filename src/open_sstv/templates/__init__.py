# SPDX-License-Identifier: GPL-3.0-or-later
"""Template subsystem: filename builders (v0.2) and image compositor (v0.3).

v0.2 API (unchanged):
    TokenContext, resolve_tokens — filename token resolver.
    build_autosave_filename, sanitize_filename_component — filename builders.

v0.3 API:
    Template, Layer types, QSOState, TXContext — data model.
    resolve_text — image-template token resolver.
    render_template — pure compositor function.
    resolve_font_path, is_font_available — font resolution.

See ``docs/design/v0.3_templates.md`` for the full design.
"""
from __future__ import annotations

from open_sstv.templates.filename import (
    build_autosave_filename,
    sanitize_filename_component,
)
from open_sstv.templates.fonts import is_font_available, resolve_font_path
from open_sstv.templates.model import (
    ANCHORS,
    RGBA,
    Anchor,
    GradientLayer,
    Layer,
    LayerBase,
    PatternLayer,
    PhotoLayer,
    QSOState,
    RectLayer,
    RxImageLayer,
    ShadowSpec,
    StationImageLayer,
    StrokeSpec,
    TXContext,
    Template,
    TextLayer,
)
from open_sstv.templates.renderer import render_template
from open_sstv.templates.tokens import TokenContext, resolve_text, resolve_tokens

__all__ = [
    # v0.2 compat
    "TokenContext",
    "build_autosave_filename",
    "resolve_tokens",
    "sanitize_filename_component",
    # v0.3 model
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
    # v0.3 functions
    "is_font_available",
    "render_template",
    "resolve_font_path",
    "resolve_text",
]
