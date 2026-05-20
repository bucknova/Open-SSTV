# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot workers for offline encode / decode via the File menu.

These do the same job as the CLI tools (``open-sstv-encode`` /
``open-sstv-decode``) but run on a background ``QThread`` and emit
Qt signals so the GUI can wire the result into the existing image
gallery and status-bar machinery.

Each worker is single-use: the caller constructs it, moves it to a
fresh ``QThread``, connects its ``finished`` signal to the thread's
``quit`` and to ``deleteLater`` for both objects, then calls the
operation slot via ``QMetaObject.invokeMethod`` (queued).  When the
operation finishes (success or failure) the worker emits
``finished``; the thread quits; everything is garbage-collected.

Worker emit semantics
---------------------

``OfflineDecodeWorker``:
    * ``image_complete(image, mode, vis_code)`` — matches the
      ``RxWorker.image_complete`` signature so the same MainWindow
      slot can handle both live RX and file-decode results.  Only
      fired on a successful decode.
    * ``error(message)`` — file-not-found, no-VIS, unsupported mode,
      or a soundfile import error for FLAC.
    * ``finished()`` — always fires last, exactly once.

``OfflineEncodeWorker``:
    * ``encode_complete(output_path, duration_s, mode)`` — written
      successfully; the GUI shows a status-bar confirmation with the
      filename + duration.
    * ``error(message)`` — image open failure, encode failure, or
      WAV write failure.
    * ``finished()`` — always fires last, exactly once.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Signal, Slot

from open_sstv.audio.file_io import load_audio_file
from open_sstv.core.decoder import decode_wav
from open_sstv.core.encoder import encode
from open_sstv.core.modes import Mode

_log = logging.getLogger(__name__)


class OfflineDecodeWorker(QObject):
    """Decode a WAV / FLAC file into an SSTV image off the GUI thread.

    Designed so its ``image_complete`` signal can be connected directly
    to ``MainWindow._on_rx_image_complete`` — the resulting image lands
    in the gallery exactly as if it had been decoded live.
    """

    #: Matches ``RxWorker.image_complete``: (PIL.Image, Mode, vis_code).
    image_complete = Signal(object, object, int)
    #: Surfaced to the GUI as a status-bar message.
    error = Signal(str)
    #: Always fired last, exactly once.  The owning thread connects
    #: this to its ``quit`` slot for one-shot cleanup.
    finished = Signal()

    @Slot(str)
    def decode(self, path: str) -> None:
        """Load the audio file and run ``decode_wav`` on its samples."""
        try:
            samples, fs = load_audio_file(Path(path))
        except FileNotFoundError as exc:
            self.error.emit(str(exc))
            self.finished.emit()
            return
        except ImportError as exc:
            # FLAC requested but soundfile not installed.
            self.error.emit(str(exc))
            self.finished.emit()
            return
        except (ValueError, wave.Error) as exc:
            self.error.emit(f"Could not read audio file: {exc}")
            self.finished.emit()
            return

        if samples.size == 0:
            self.error.emit("Audio file is empty.")
            self.finished.emit()
            return

        try:
            result = decode_wav(samples, fs)
        except Exception as exc:  # noqa: BLE001 — bubble anything to UI
            _log.exception("offline decode raised")
            self.error.emit(f"Decode failed: {exc}")
            self.finished.emit()
            return

        if result is None:
            self.error.emit(
                "No SSTV header found in file, or detected mode is not supported."
            )
            self.finished.emit()
            return

        self.image_complete.emit(result.image, result.mode, result.vis_code)
        self.finished.emit()


class OfflineEncodeWorker(QObject):
    """Encode an image to a WAV file off the GUI thread.

    Writes 16-bit PCM mono at the caller-supplied sample rate, matching
    the format ``open-sstv-encode`` produces.  PySSTV's ``encode``
    returns int16 samples directly, so no extra conversion is needed.
    """

    #: ``(output_path, duration_s, mode)`` on a successful write.
    encode_complete = Signal(str, float, object)
    #: Surfaced as a status-bar message + optional QMessageBox.
    error = Signal(str)
    #: Always fired last, exactly once.
    finished = Signal()

    @Slot(str, object, int, str)
    def encode(
        self,
        image_path: str,
        mode: Mode,
        sample_rate: int,
        output_path: str,
    ) -> None:
        """Open *image_path*, encode it in *mode*, write to *output_path*."""
        try:
            img = Image.open(image_path)
            img.load()  # force decoder to read the file now, before we close
        except (FileNotFoundError, OSError, Image.UnidentifiedImageError) as exc:
            self.error.emit(f"Could not open image: {exc}")
            self.finished.emit()
            return

        try:
            samples = encode(img, mode, sample_rate=sample_rate)
        except (ValueError, OSError) as exc:
            self.error.emit(f"Encode failed: {exc}")
            self.finished.emit()
            return
        except Exception as exc:  # noqa: BLE001 — PySSTV can raise generic exceptions
            _log.exception("offline encode raised")
            self.error.emit(f"Encode failed: {exc}")
            self.finished.emit()
            return

        try:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)  # 16-bit PCM
                wav.setframerate(sample_rate)
                wav.writeframes(np.asarray(samples, dtype=np.int16).tobytes())
        except OSError as exc:
            self.error.emit(f"Could not write WAV: {exc}")
            self.finished.emit()
            return

        duration_s = samples.size / sample_rate
        self.encode_complete.emit(str(out), float(duration_s), mode)
        self.finished.emit()


__all__ = ["OfflineDecodeWorker", "OfflineEncodeWorker"]
