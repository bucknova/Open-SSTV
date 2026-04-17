# SPDX-License-Identifier: GPL-3.0-or-later
"""Auto-save filename builder.

Takes a user-configured pattern like ``"%d_%t_%c_%m"`` and resolves it
into a concrete file path in the auto-save directory, handling:

1. Token resolution via :func:`open_sstv.templates.tokens.resolve_tokens`.
2. Filename-safety sanitisation (strip OS-forbidden characters, collapse
   whitespace, cap length).
3. Collision resolution — if the resolved filename already exists on
   disk, append ``_001``, ``_002``, … until unique.

All paths go through this single function so RX and TX auto-save share
identical safety + collision behaviour.  The resolver itself is pure
and tested independently; this module layers the filesystem concerns
on top.
"""
from __future__ import annotations

import re
from pathlib import Path

from open_sstv.templates.tokens import TokenContext, resolve_tokens

# Characters forbidden in filenames on at least one of our three target
# platforms.  Windows is the strictest: ``/ \ : * ? " < > |``.  macOS
# disallows ``/`` and the NUL byte (``/`` is path separator, NUL is a
# legacy C-string terminator).  Linux technically only forbids NUL, but
# many utilities misbehave on the full Windows set — sanitising against
# the superset lets users share save directories over SMB / NAS without
# surprises.
_FORBIDDEN_CHARS_RE = re.compile(r'[/\\:\*\?"<>\|\x00]')

# Whitespace sequences collapse to a single underscore.  Tabs and newlines
# can appear if a user pastes a weird template string; this keeps the
# resulting filename tidy.
_WHITESPACE_RUN_RE = re.compile(r"\s+")

# Hard cap on the resolved filename stem (before the extension).  200
# leaves comfortable headroom for ``.png``/``.jpg``, collision suffixes
# ``_NNN``, and typical save-directory path lengths on every OS we ship.
_MAX_STEM_LENGTH = 200

# Default separator when a token resolves to empty (e.g. no callsign set)
# and leaves neighbouring separators adjacent — ``a__b`` collapses to
# ``a_b``.  Preserves readability without the caller having to know
# which tokens are currently empty.
_SEPARATOR_COLLAPSE_RE = re.compile(r"[_\-]{2,}")


def sanitize_filename_component(raw: str) -> str:
    """Return *raw* with forbidden characters stripped and whitespace
    collapsed.

    Applies, in order:

    * Strip characters forbidden on Windows / macOS / Linux
      (``/ \\ : * ? " < > |`` plus NUL).
    * Collapse whitespace runs to a single ``_``.
    * Collapse runs of ``_`` or ``-`` longer than one to a single
      character (handles the case where a resolved token was empty
      and left neighbouring separators adjacent).
    * Strip leading / trailing ``_``, ``-``, and whitespace.
    * Cap length at :data:`_MAX_STEM_LENGTH`.

    Returns the cleaned string.  If the input was entirely forbidden
    characters (so nothing survives), returns ``"sstv"`` as a last-ditch
    fallback — the file still needs a name.
    """
    cleaned = _FORBIDDEN_CHARS_RE.sub("", raw)
    cleaned = _WHITESPACE_RUN_RE.sub("_", cleaned)
    cleaned = _SEPARATOR_COLLAPSE_RE.sub(lambda m: m.group(0)[0], cleaned)
    cleaned = cleaned.strip("_- \t\n")
    if len(cleaned) > _MAX_STEM_LENGTH:
        cleaned = cleaned[:_MAX_STEM_LENGTH].rstrip("_- ")
    if not cleaned:
        return "sstv"
    return cleaned


def _resolve_collision(candidate: Path) -> Path:
    """Return a path that does not collide with an existing file.

    If *candidate* does not exist, return it unchanged.  Otherwise,
    append ``_001``, ``_002``, … (zero-padded to 3 digits) before the
    extension until an unused path is found.  The search caps at
    ``_999`` — at which point we fall back to appending a longer index
    suffix rather than silently overwriting.
    """
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    parent = candidate.parent
    for n in range(1, 1000):
        trial = parent / f"{stem}_{n:03d}{suffix}"
        if not trial.exists():
            return trial
    # 999 collisions in the same filename bucket is absurd but we still
    # mustn't overwrite.  Fall through to 4-digit, 5-digit, etc.
    n = 1000
    while True:
        trial = parent / f"{stem}_{n}{suffix}"
        if not trial.exists():
            return trial
        n += 1


def build_autosave_filename(
    pattern: str,
    save_dir: Path,
    ctx: TokenContext,
    file_format: str = "png",
) -> Path:
    """Build a complete auto-save path for the given context.

    Parameters
    ----------
    pattern:
        User-configured filename pattern with tokens, e.g.
        ``"%d_%t_%c_%m"``.  See :mod:`open_sstv.templates.tokens`
        for the supported vocabulary.
    save_dir:
        Target directory.  Not created here — the caller is responsible
        for ensuring the directory exists (``Path.mkdir(parents=True,
        exist_ok=True)``).
    ctx:
        Token resolution context (callsign, mode, clock, direction, …).
    file_format:
        File-format extension without the dot: ``"png"`` or ``"jpg"``.
        Lowercased before being appended.

    Returns
    -------
    pathlib.Path
        A complete, collision-free path inside *save_dir* ready for
        ``image.save(path)``.

    Notes
    -----
    The returned path's existence is checked at build time but not
    held — a caller writing to it should still handle the rare race
    where two threads produce the same path between build and write.
    In practice, RX and TX auto-save both run on the GUI thread so the
    race cannot happen today; the collision loop only fires when a
    previous run left files on disk or when two receives land in the
    same second with an empty callsign.
    """
    resolved = resolve_tokens(pattern, ctx)
    stem = sanitize_filename_component(resolved)
    ext = file_format.lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg"):
        # Unknown format — default to PNG rather than producing a file
        # the user's viewer can't open.  Silent fallback is acceptable
        # here because the Settings UI constrains the input to PNG/JPG.
        ext = "png"
    candidate = save_dir / f"{stem}.{ext}"
    return _resolve_collision(candidate)


__all__ = ["build_autosave_filename", "sanitize_filename_component"]
