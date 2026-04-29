# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ``open_sstv.__version__``.

The package's ``__version__`` was hardcoded as a literal string until v0.3.3.
That meant every release commit had to bump *two* version markers in lock-
step — pyproject.toml and __init__.py — and one of them was missed in
v0.3.1 / v0.3.2, leaving the About dialog, TX banner, and update checker
all reading a stale value.

Since v0.3.3 the package reads its own version via
``importlib.metadata.version("open_sstv")`` so pyproject is the single
source of truth.  These tests pin that contract:

* ``__version__`` is a non-empty string that looks like a version.
* When the package metadata isn't available (running from unpacked source
  without ``pip install -e .``), the module falls back to a sentinel
  ``"0.0.0-dev"`` rather than raising at import time.
"""
from __future__ import annotations

import importlib
import importlib.metadata


class TestVersionAttribute:
    def test_is_nonempty_string(self) -> None:
        from open_sstv import __version__
        assert isinstance(__version__, str)
        assert __version__  # truthy / non-empty

    def test_looks_like_a_version(self) -> None:
        """Both the real metadata-derived version (e.g. '0.3.3') and the
        fallback ('0.0.0-dev') contain digits and at least one dot."""
        from open_sstv import __version__
        assert any(c.isdigit() for c in __version__), (
            f"__version__={__version__!r} has no digits"
        )
        assert "." in __version__, f"__version__={__version__!r} has no '.'"


class TestPackageNotFoundFallback:
    """Exercise the unpacked-source path where ``importlib.metadata`` can't
    locate the package."""

    def test_fallback_to_dev_sentinel(self) -> None:
        import open_sstv

        original = importlib.metadata.version

        def raise_not_found(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        importlib.metadata.version = raise_not_found  # type: ignore[assignment]
        try:
            importlib.reload(open_sstv)
            assert open_sstv.__version__ == "0.0.0-dev"
        finally:
            # Restore the real metadata.version and reload so downstream
            # tests see the genuine package version, not the sentinel.
            importlib.metadata.version = original  # type: ignore[assignment]
            importlib.reload(open_sstv)
