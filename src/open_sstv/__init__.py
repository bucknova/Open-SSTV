# SPDX-License-Identifier: GPL-3.0-or-later
"""sstv-app — open-source cross-platform SSTV transceiver for amateur radio."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from open_sstv.security import apply_pil_security_limits

try:
    __version__ = version("open_sstv")
except PackageNotFoundError:
    # Running from unpacked source without an install (rare — but the
    # CLI tools and test runners can hit this). Keep the package importable
    # so the rest of the app doesn't blow up at import time.
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]

# Apply PIL decompression-bomb cap on package import so every entry point
# (GUI, CLI encoder, tests) is protected before opening its first image.
apply_pil_security_limits()
