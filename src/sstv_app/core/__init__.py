# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-DSP core for sstv-app.

This package owns the SSTV encode and decode pipeline and is **not allowed** to
import anything from ``sstv_app.ui``, ``sstv_app.audio``, ``sstv_app.radio``,
or ``sstv_app.config``. Everything in here takes NumPy arrays in and returns
NumPy arrays / dataclasses out, so the decoder is unit-testable in headless CI
and can be driven from any front-end (Qt, TUI, web, CLI) without modification.
"""
