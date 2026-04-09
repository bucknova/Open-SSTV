# SPDX-License-Identifier: GPL-3.0-or-later
"""SSTV encoder — thin facade over PySSTV.

PySSTV (MIT) already implements the encoder for every mode we care about.
This module exists so the rest of the app never imports ``pysstv`` directly:
it gives us one place to (a) translate from our ``Mode`` enum to PySSTV's
class objects, (b) preprocess images (resize to mode-native dimensions,
convert to RGB) before handing them off, and (c) return NumPy arrays for the
audio output layer.

Public API:
    encode(image, mode, sample_rate=48000) -> np.ndarray  # int16 PCM samples

Phase 0 stub. Implemented in Phase 1 step 5 of the v1 plan.
"""
from __future__ import annotations
