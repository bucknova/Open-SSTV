# SPDX-License-Identifier: GPL-3.0-or-later
"""Process-wide security limits.

Pillow's default ``MAX_IMAGE_PIXELS`` (~89 MP) is high enough that a
maliciously crafted PNG/TIFF can decompress to gigabytes of memory and
DoS the app — a classic "decompression bomb."  We tighten this to 32 MP,
which still covers any legitimate SSTV input (the largest mode we support
is 800×616 ≈ 0.5 MP) and any reasonable TX photo or QSL-card image.

``apply_pil_security_limits`` is idempotent and runs from
``open_sstv/__init__.py`` so every entry point — GUI, CLI encoder, tests
— gets the limit before opening its first image.  Call sites still
wrap ``Image.open`` in ``try/except Image.DecompressionBombError`` so an
oversized file degrades into a user-visible error instead of an uncaught
exception that crashes the GUI thread.
"""
from __future__ import annotations

#: Hard cap on decoded image pixels.  32 MP is generous for SSTV (largest
#: native frame is ~0.5 MP) and still well below "let the OS swap to
#: death" territory on a typical 16 GB laptop.
MAX_IMAGE_PIXELS: int = 1024 * 1024 * 32

#: What we actually store in ``PIL.Image.MAX_IMAGE_PIXELS``.  Pillow only
#: *raises* ``DecompressionBombError`` above **2x** that value (between 1x
#: and 2x it merely warns and decodes), so storing the cap itself made the
#: real limit 64 MP (v0.6.14 audit, Info).  Storing half puts the error at
#: exactly ``MAX_IMAGE_PIXELS``.  We deliberately don't turn the warning
#: into an error instead: it would surface as ``DecompressionBombWarning``,
#: which the call sites' ``except DecompressionBombError`` don't catch.
PIL_MAX_IMAGE_PIXELS: int = MAX_IMAGE_PIXELS // 2


def apply_pil_security_limits() -> None:
    """Apply the process-wide Pillow safety cap.

    Importing PIL here keeps the cost off the import path of pure-Python
    modules that don't need it (the encoder/decoder CLIs already import
    PIL eagerly, but this lets ``open_sstv`` itself stay light if a
    future entry point doesn't touch images).
    """
    import warnings

    import PIL.Image
    PIL.Image.MAX_IMAGE_PIXELS = PIL_MAX_IMAGE_PIXELS
    # 16-32 MP is a legitimate camera photo, not a bomb: the warning Pillow
    # now emits for it is noise.  Above 32 MP it raises regardless.
    warnings.filterwarnings("ignore", category=PIL.Image.DecompressionBombWarning)


__all__ = ["MAX_IMAGE_PIXELS", "PIL_MAX_IMAGE_PIXELS", "apply_pil_security_limits"]
