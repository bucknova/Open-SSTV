# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared pytest fixtures for sstv-app.

In Phase 2 this file generates SSTV WAV fixtures on demand at session start
by feeding small reference images from ``tests/fixtures/images/`` through
PySSTV. We deliberately don't commit the WAV blobs to the repo — they're
regenerated locally so the binary surface stays small. Noisy variants are
produced by mixing in white Gaussian noise at 20 / 10 / 5 dB SNR.

Phase 0 stub. Real fixtures land alongside the decoder work in Phase 2.
"""
from __future__ import annotations
