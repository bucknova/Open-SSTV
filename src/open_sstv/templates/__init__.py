# SPDX-License-Identifier: GPL-3.0-or-later
"""Template-related utilities: token resolution, filename builders.

v0.2.8 ships the token resolver and filename builder used by the
auto-save feature.  The same resolver will be reused in v0.3 when the
image-template compositor lands — one resolver, two call sites (one
for filenames, one for on-image text layers).

See ``docs/design/v0.3_templates.md`` for the full v0.3 design.
"""
from __future__ import annotations

from open_sstv.templates.filename import (
    build_autosave_filename,
    sanitize_filename_component,
)
from open_sstv.templates.tokens import TokenContext, resolve_tokens

__all__ = [
    "TokenContext",
    "build_autosave_filename",
    "resolve_tokens",
    "sanitize_filename_component",
]
