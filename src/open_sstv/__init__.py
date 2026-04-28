# SPDX-License-Identifier: GPL-3.0-or-later
"""sstv-app — open-source cross-platform SSTV transceiver for amateur radio."""
from __future__ import annotations

from open_sstv.security import apply_pil_security_limits

__version__ = "0.3.0"
__all__ = ["__version__"]

# Apply PIL decompression-bomb cap on package import so every entry point
# (GUI, CLI encoder, tests) is protected before opening its first image.
apply_pil_security_limits()
