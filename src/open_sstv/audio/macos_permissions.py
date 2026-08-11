# SPDX-License-Identifier: GPL-3.0-or-later
"""macOS microphone (TCC) authorization status.

macOS gates *all* audio input behind TCC — including virtual loopback
devices like BlackHole that never touch a physical microphone.  The
failure mode is silent by design: a denied app still opens its input
stream successfully and simply receives an endless stream of zeroes.  So
without an explicit check, Open-SSTV cheerfully reports "Capturing…" while
decoding silence forever (issue #35).

This module answers "may we record?" with no new dependency — the same
``ctypes`` + ``libobjc`` trick :func:`open_sstv.app._set_macos_process_name`
already uses — by calling::

    [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio]

Everything is wrapped defensively: any lookup failure, ABI change, or
non-macOS platform yields :data:`UNKNOWN`, and callers treat that as "carry
on".  A permissions *hint* must never be the thing that stops capture from
starting.

Note this only *reads* the status.  When it is ``notDetermined``, opening
the input stream is what triggers the system prompt — provided the bundle
carries ``NSMicrophoneUsageDescription`` and the
``com.apple.security.device.audio-input`` entitlement, which is the other
half of the issue-#35 fix (see ``open_sstv.spec`` and
``packaging/macos-entitlements.plist``).
"""
from __future__ import annotations

import logging
import sys

_log = logging.getLogger(__name__)

#: Access granted — recording will work.
AUTHORIZED = "authorized"
#: The user has actively refused, or a profile forbids it.  Recording will
#: silently produce zeroes until they change it in System Settings.
DENIED = "denied"
#: No decision yet.  Opening the stream will raise the system prompt.
NOT_DETERMINED = "not_determined"
#: Not macOS, or the status could not be read.  Callers proceed normally.
UNKNOWN = "unknown"

#: ``AVAuthorizationStatus`` values, per Apple's AVFoundation headers.
_STATUS_MAP = {
    0: NOT_DETERMINED,
    1: DENIED,   # restricted (e.g. MDM profile) — same practical outcome
    2: DENIED,
    3: AUTHORIZED,
}

#: ``AVMediaTypeAudio`` is the four-char code "soun".
_MEDIA_TYPE_AUDIO = b"soun"


def microphone_authorization() -> str:
    """Return the current microphone TCC status.

    One of :data:`AUTHORIZED`, :data:`DENIED`, :data:`NOT_DETERMINED`, or
    :data:`UNKNOWN`.  Never raises.
    """
    if sys.platform != "darwin":
        return UNKNOWN
    try:
        import ctypes  # noqa: PLC0415
        import ctypes.util  # noqa: PLC0415

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        # AVCaptureDevice lives in AVFoundation; the class won't resolve
        # until that framework is loaded into the process.
        avf = ctypes.util.find_library("AVFoundation")
        if avf is None:
            return UNKNOWN
        ctypes.cdll.LoadLibrary(avf)

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        capture_device = objc.objc_getClass(b"AVCaptureDevice")
        ns_string = objc.objc_getClass(b"NSString")
        if not capture_device or not ns_string:
            return UNKNOWN

        # +[NSString stringWithUTF8String:] → the AVMediaTypeAudio constant.
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p,
        ]
        media_type = objc.objc_msgSend(
            ns_string,
            objc.sel_registerName(b"stringWithUTF8String:"),
            _MEDIA_TYPE_AUDIO,
        )
        if not media_type:
            return UNKNOWN

        # +[AVCaptureDevice authorizationStatusForMediaType:] → NSInteger.
        objc.objc_msgSend.restype = ctypes.c_long
        objc.objc_msgSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        status = objc.objc_msgSend(
            capture_device,
            objc.sel_registerName(b"authorizationStatusForMediaType:"),
            media_type,
        )
        return _STATUS_MAP.get(int(status), UNKNOWN)
    except Exception:  # noqa: BLE001 — a hint must never break capture
        _log.debug("could not read microphone authorization status", exc_info=True)
        return UNKNOWN


#: Shown when TCC has refused.  Names the exact System Settings pane,
#: because the app cannot re-prompt once the user has denied it.
DENIED_MESSAGE = (
    "macOS is blocking microphone access, so capture would receive only "
    "silence. Open System Settings → Privacy & Security → Microphone and "
    "enable Open-SSTV, then start capture again. (macOS treats every audio "
    "input as a microphone, including virtual devices like BlackHole.)"
)


__all__ = [
    "AUTHORIZED",
    "DENIED",
    "DENIED_MESSAGE",
    "NOT_DETERMINED",
    "UNKNOWN",
    "microphone_authorization",
]
