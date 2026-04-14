# SPDX-License-Identifier: GPL-3.0-or-later
"""Allows ``python -m open_sstv`` to run the app entry point."""
from __future__ import annotations

import sys

from open_sstv.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
