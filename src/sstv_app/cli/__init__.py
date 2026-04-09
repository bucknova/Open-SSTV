# SPDX-License-Identifier: GPL-3.0-or-later
"""No-Qt command-line entry points.

Two trivial argparse front-ends that call ``core.encoder`` / ``core.decoder``
directly without bringing in PySide6. Useful for headless smoke tests in CI
and for users on a Raspberry Pi who don't want a desktop GUI.
"""
