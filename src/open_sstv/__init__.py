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
#
# N-1 (audit 4.7/v0.2.9): the call below is a deliberate import-time
# side effect.  Any ``from open_sstv...`` import — including the CLI
# entry points, test collection, and downstream consumers — runs this
# package's ``__init__`` and therefore lowers ``PIL.Image.MAX_IMAGE_PIXELS``
# before the first ``Image.open()`` is reachable.  Removing this call
# does not just affect the GUI; it would also disarm every CLI and
# test that opens an image.  See ``open_sstv.security`` for the
# rationale (decompression-bomb DoS) and the chosen cap (32 MP).
apply_pil_security_limits()
