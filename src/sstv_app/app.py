# SPDX-License-Identifier: GPL-3.0-or-later
"""QApplication bootstrap and dependency-injection wiring.

For Phase 0 this is just a stub ``main()`` that prints a banner and exits, so
we can verify ``python -m sstv_app`` runs end-to-end before any heavyweight Qt
or audio dependencies have been wired in. The real Qt application is wired up
in Phase 1 (TX-only main window) and Phase 2 (full RX/TX) per the v1 plan.
"""
from __future__ import annotations

import sys

from sstv_app import __version__


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``sstv-app`` console script and ``python -m sstv_app``."""
    del argv  # Phase 0 stub: no CLI flags wired yet.
    print(f"Hello, SSTV — sstv-app v{__version__} (pre-alpha scaffold).")
    print("Phase 0 only: no UI, no audio, no decoder yet. See docs/architecture.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
