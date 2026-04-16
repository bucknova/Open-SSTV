# SPDX-License-Identifier: GPL-3.0-or-later
"""CW (Morse code) audio generator for SSTV station identification.

Produces int16 PCM samples for a callsign or other text, suitable for
appending to an SSTV transmission as a regulatory station identifier.

Timing follows ITU-R M.1677-1 (international Morse code):

* Dit (dot)                — 1 unit on
* Dah (dash)               — 3 units on
* Intra-character gap      — 1 unit off  (between elements within a character)
* Inter-character gap      — 3 units off (between letters/digits)
* Inter-word gap           — 7 units off (between words separated by spaces)

Unit duration = 1.2 / wpm seconds  (PARIS standard word at the given WPM).

The tone uses a 5 ms linear attack/decay envelope to suppress key-click
transients, which would otherwise cause adjacent-channel interference on
a properly aligned SSB transmitter.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

_log = logging.getLogger(__name__)

#: ITU-R M.1677-1 Morse code table.
#: Letters A–Z, digits 0–9, slash "/" (portable suffix, e.g. W0AEZ/P),
#: and hyphen "-" (occasionally used in vanity callsigns).
_MORSE_TABLE: dict[str, str] = {
    "A": ".-",    "B": "-...",  "C": "-.-.",  "D": "-..",
    "E": ".",     "F": "..-.",  "G": "--.",   "H": "....",
    "I": "..",    "J": ".---",  "K": "-.-",   "L": ".-..",
    "M": "--",    "N": "-.",    "O": "---",   "P": ".--.",
    "Q": "--.-",  "R": ".-.",   "S": "...",   "T": "-",
    "U": "..-",   "V": "...-",  "W": ".--",   "X": "-..-",
    "Y": "-.--",  "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--",
    "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.",
    "/": "-..-.", "-": "-....-",
}


def _make_tone(
    n_samples: int, freq_hz: float, amplitude: float, sample_rate: int
) -> "NDArray[np.int16]":
    """Return *n_samples* of a windowed sine at *freq_hz*.

    A 5 ms linear attack and decay prevent key-click transients.  The
    ramp is capped at half the element length so very short dits at high
    WPM never have overlapping ramps.
    """
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    wave = np.sin(2.0 * np.pi * freq_hz * t)

    ramp_n = min(int(0.005 * sample_rate), n_samples // 2)
    if ramp_n > 0:
        wave[:ramp_n] *= np.linspace(0.0, 1.0, ramp_n)
        wave[-ramp_n:] *= np.linspace(1.0, 0.0, ramp_n)

    return (wave * amplitude * 32767.0).astype(np.int16)


def make_cw(
    text: str,
    *,
    wpm: int = 20,
    tone_hz: int = 800,
    sample_rate: int = 48_000,
    peak_dbfs: float = -1.0,
) -> "NDArray[np.int16]":
    """Generate Morse code PCM samples for *text*.

    Parameters
    ----------
    text:
        The string to encode (case-insensitive).  Characters not in the
        Morse table are silently skipped with a DEBUG log entry.
    wpm:
        Sending speed in words per minute (PARIS standard word).
        Dit duration = 1.2 / wpm seconds.  Typical range 15–30 WPM.
    tone_hz:
        Sidetone frequency in Hz.  800 Hz is comfortable for CW; the
        range 400–1200 Hz sits within the SSTV audio passband so no
        additional filtering is required downstream.
    sample_rate:
        PCM sample rate in Hz.  Must match the rest of the audio chain.
    peak_dbfs:
        Peak amplitude relative to digital full scale in dB (≤ 0.0).
        Default −1.0 matches the SSTV two-tone test signal convention so
        the CW tail has the same perceived level as the image audio.

    Returns
    -------
    numpy.ndarray
        int16 PCM array.  Returns an empty length-0 array when *text*
        contains no encodable characters.

    Examples
    --------
    >>> samples = make_cw("W0AEZ", wpm=20, tone_hz=800, sample_rate=48_000)
    >>> samples.dtype
    dtype('int16')
    """
    amplitude = 10.0 ** (peak_dbfs / 20.0)  # linear ≤ 1.0
    dit_n = max(1, int(round(1.2 / wpm * sample_rate)))

    # Pre-build silence blocks to avoid per-element allocation.
    silence_1 = np.zeros(dit_n, dtype=np.int16)
    silence_3 = np.zeros(3 * dit_n, dtype=np.int16)
    silence_7 = np.zeros(7 * dit_n, dtype=np.int16)

    chunks: list[NDArray[np.int16]] = []
    words = text.upper().split()

    # OP-15: surface unsupported characters at WARNING (not DEBUG) so the
    # operator notices when their callsign contains a glyph that won't be
    # transmitted.  This matters for regulatory ID — a missing character
    # means the CW tail does not actually identify the station.
    skipped: list[str] = []

    for w_idx, word in enumerate(words):
        for c_idx, char in enumerate(word):
            pattern = _MORSE_TABLE.get(char)
            if pattern is None:
                skipped.append(char)
                continue

            for e_idx, sym in enumerate(pattern):
                n = dit_n if sym == "." else 3 * dit_n
                chunks.append(_make_tone(n, float(tone_hz), amplitude, sample_rate))
                if e_idx < len(pattern) - 1:
                    chunks.append(silence_1)  # intra-character gap: 1 unit

            # Inter-character gap: 3 units (after every char except the last
            # in the word; the silence_1 already consumed after the last
            # element so we add 2 more to reach 3 total — but it's cleaner
            # to append the full 3-unit gap and skip the last intra-element
            # silence above, which the loop already does correctly because
            # the `if e_idx < len(pattern) - 1` guard omits the final gap).
            if c_idx < len(word) - 1:
                chunks.append(silence_3)

        # Inter-word gap: 7 units after every word except the last.
        if w_idx < len(words) - 1:
            chunks.append(silence_7)

    if skipped:
        # Deduplicate while preserving order so the log is short.
        unique = list(dict.fromkeys(skipped))
        _log.warning(
            "CW: skipped %d unsupported character(s) %r in %r — "
            "your station ID may be incomplete. Supported set is "
            "A-Z, 0-9, '/', '-'.",
            len(skipped),
            "".join(unique),
            text,
        )

    if not chunks:
        return np.array([], dtype=np.int16)

    return np.concatenate(chunks).astype(np.int16)


__all__ = ["make_cw", "_MORSE_TABLE"]
