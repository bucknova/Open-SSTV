# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot workers for offline encode / decode via in-panel buttons.

These do the same job as the CLI tools (``open-sstv-encode`` /
``open-sstv-decode``) but run on a background ``QThread`` and emit
Qt signals so the GUI can wire the result into the existing image
gallery and status-bar machinery.

Each worker is single-use: the caller constructs it with the operation
args, moves it to a fresh ``QThread``, connects ``thread.started``
to ``worker.run``, then starts the thread.  When ``run`` returns
(success or failure) the worker emits ``finished``; the thread quits;
the caller releases its strong refs in ``thread.finished``.

Why constructor args instead of ``QMetaObject.invokeMethod`` + ``Q_ARG``?
PySide6 6.11's ``Q_ARG`` cannot marshal arbitrary Python objects
(``PIL.Image``, the ``Mode`` StrEnum) across a queued invocation —
the resulting C++ type ``PyObjectWrapper`` doesn't match the slot's
declared ``PyObject`` parameter, and ``Q_ARG(object, ...)`` fails
to look up the meta-type entirely.  Stashing args in ``__init__``
sidesteps the marshalling layer: by the time ``run`` is invoked
(via direct ``thread.started`` signal, no value marshalling) the
args are already attributes on the worker living in its own thread.
This is the same pattern ``_RigConnectWorker`` uses.

The encode worker takes a pre-rendered ``PIL.Image`` rather than a
file path: the TX panel already composites template + photo + QSO
state into a single image for live transmit, so the "Export to
Audio" button reuses that exact composite — no second image picker,
no second mode picker, no separate template wiring.

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
    * ``error(message)`` — encode failure or WAV write failure.
    * ``finished()`` — always fires last, exactly once.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from open_sstv.audio.file_io import load_audio_file
from open_sstv.core.decoder import decode_wav
from open_sstv.core.encoder import encode
from open_sstv.core.modes import Mode

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_log = logging.getLogger(__name__)


class OfflineDecodeWorker(QObject):
    """Decode a WAV / FLAC file into an SSTV image off the GUI thread.

    Single-use.  Path is passed to ``__init__``; call ``run`` (via
    ``thread.started`` signal) to perform the decode.  Result is
    delivered through ``image_complete`` or ``error`` and is always
    followed by ``finished``.
    """

    #: Matches ``RxWorker.image_complete``: (PIL.Image, Mode, vis_code).
    image_complete = Signal(object, object, int)
    #: Surfaced to the GUI as a status-bar message.
    error = Signal(str)
    #: Always fired last, exactly once.  The owning thread connects
    #: this to its ``quit`` slot for one-shot cleanup.
    finished = Signal()

    def __init__(self, path: str = "") -> None:
        """*path* may be empty for unit tests that drive ``decode`` directly."""
        super().__init__()
        self._path = path

    @Slot()
    def run(self) -> None:
        """Decode the file passed at construction time."""
        self.decode(self._path)

    @Slot(str)
    def decode(self, path: str) -> None:
        """Load the audio file and run ``decode_wav`` on its samples.

        Public so unit tests can drive the worker synchronously without
        going through ``thread.started``.  Production callers should
        use the ``__init__(path)`` + ``thread.started → run`` pattern.
        """
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
    """Encode a pre-rendered image to a WAV file off the GUI thread.

    Single-use.  Args (image, mode, sample rate, output path) are
    passed to ``__init__``; ``run`` does the work.  Writes 16-bit PCM
    mono at the caller-supplied sample rate, matching the format
    ``open-sstv-encode`` produces.

    The caller passes a fully composited ``PIL.Image`` — typically the
    output of ``TxPanel._compose_for_emit()`` (template + photo + QSO
    overlays) — so the exported WAV contains exactly what live TX
    would have transmitted.
    """

    #: ``(output_path, duration_s, mode)`` on a successful write.
    encode_complete = Signal(str, float, object)
    #: Surfaced as a status-bar message + optional QMessageBox.
    error = Signal(str)
    #: Always fired last, exactly once.
    finished = Signal()

    def __init__(
        self,
        image: PILImage | None = None,
        mode: Mode | None = None,
        sample_rate: int = 0,
        output_path: str = "",
    ) -> None:
        """All args optional so unit tests can drive ``encode_from_image`` directly."""
        super().__init__()
        self._image = image
        self._mode = mode
        self._sample_rate = sample_rate
        self._output_path = output_path

    @Slot()
    def run(self) -> None:
        """Encode the image passed at construction time."""
        if self._image is None or self._mode is None:
            self.error.emit("Encode worker started with no image or mode.")
            self.finished.emit()
            return
        self.encode_from_image(
            self._image, self._mode, self._sample_rate, self._output_path
        )

    @Slot(object, object, int, str)
    def encode_from_image(
        self,
        image: PILImage,
        mode: Mode,
        sample_rate: int,
        output_path: str,
    ) -> None:
        """Encode *image* in *mode* and write to *output_path*.

        Public so unit tests can drive the worker synchronously without
        going through ``thread.started``.  Production callers should
        use the ``__init__(image, mode, …)`` + ``thread.started → run``
        pattern, because PySide6 6.11's ``Q_ARG`` cannot marshal PIL
        images or StrEnum modes across a queued ``invokeMethod`` call.
        """
        try:
            samples = encode(image, mode, sample_rate=sample_rate)
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
