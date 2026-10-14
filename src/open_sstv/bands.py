# SPDX-License-Identifier: GPL-3.0-or-later
"""Amateur band lookup — the one table shared by template tokens and ADIF.

v0.6.14 audit: ``templates.tokens`` and ``logbook.adif`` each carried a
band table and they disagreed — the token version held only lower edges,
so 7.5 MHz (above 40 m) rendered as "40m" and 450 MHz as "70cm" forever
upward.  Ranges here follow the ADIF 3.1 Band Enumeration; a frequency
outside every band maps to ``None`` (callers that want text use ``""``).
"""
from __future__ import annotations

import math

#: ``(lo_mhz, hi_mhz, band_name)`` per the ADIF Band Enumeration.
BAND_TABLE: tuple[tuple[float, float, str], ...] = (
    (0.1357, 0.1378, "2190m"),
    (0.472, 0.479, "630m"),
    (1.8, 2.0, "160m"),
    (3.5, 4.0, "80m"),
    (5.06, 5.45, "60m"),
    (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"),
    (14.0, 14.35, "20m"),
    (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"),
    (24.89, 24.99, "12m"),
    (28.0, 29.7, "10m"),
    (50.0, 54.0, "6m"),
    (70.0, 70.5, "4m"),
    (144.0, 148.0, "2m"),
    (219.0, 225.0, "1.25m"),
    (420.0, 450.0, "70cm"),
    (902.0, 928.0, "33cm"),
    (1240.0, 1300.0, "23cm"),
    (2300.0, 2450.0, "13cm"),
    (3300.0, 3500.0, "9cm"),
    (5650.0, 5925.0, "6cm"),
    (10000.0, 10500.0, "3cm"),
    (24000.0, 24250.0, "1.25cm"),
)

#: Band names, lowercase, for validating an imported ADIF ``BAND``.
BAND_NAMES: frozenset[str] = frozenset(name for _lo, _hi, name in BAND_TABLE)


def hz_to_band(freq_hz: float | None) -> str | None:
    """Map a frequency in Hz to its band name ("20m"), or ``None`` when it
    is missing, non-positive, non-finite, or outside every band."""
    if freq_hz is None or not math.isfinite(freq_hz) or freq_hz <= 0:
        return None
    mhz = freq_hz / 1_000_000.0
    for lo, hi, name in BAND_TABLE:
        if lo <= mhz <= hi:
            return name
    return None


__all__ = ["BAND_NAMES", "BAND_TABLE", "hz_to_band"]
