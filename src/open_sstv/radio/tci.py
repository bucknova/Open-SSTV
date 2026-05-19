# SPDX-License-Identifier: GPL-3.0-or-later
"""TCI (Transceiver Control Interface) rig backend.

Implements the ``Rig`` Protocol for SDRs and transceivers that speak the
Expert Electronics TCI 1.x WebSocket protocol — primarily the SunSDR2 family
(SunSDR2 DX / Pro, ColibriNano) running ExpertSDR2, and compatible servers
such as HDSDR with the TCI plugin or SDR Console.

Protocol overview
-----------------
A single WebSocket connection (``ws://host:port``, default 40001) carries:

* **Text frames** — semicolon-terminated commands and event notifications.
  Examples::

      vfo:0,0,14074000;      # set / report VFO A of TRX 0 at 14.074 MHz
      modulation:0,USB;      # set / report demodulation mode
      trx:0,true;            # PTT on for TRX 0
      rx_smeter:0,0,-73;     # S-meter report (trx, channel, dBm)
      READY:                 # server initialised, ready for commands

* **Binary frames** — raw int16 LE PCM audio from the receiver.
  Frame layout (TCI v1, Expert Electronics)::

      Bytes 0-3 : uint32 LE — TRX index
      Bytes 4-7 : uint32 LE — channel index
      Bytes 8+  : int16 LE  — mono PCM samples

TCI is a **push-based protocol**.  The server sends state changes
proactively; it does not reliably respond to bare query commands.  All
getters therefore return the last value received from the server.  During
connection setup the server sends a full state snapshot (vfo, modulation,
trx, …) after ``READY:``, so by the time ``open()`` returns the cache is
populated.

Audio subscription is **not** managed by ``TciRig``.  ``TciInputStreamWorker``
(in ``open_sstv.audio.tci_input_stream``) handles the ``start:`` / ``stop:``
commands and receives audio via a callback registered on the shared
``TciConnection``.  Both objects share a single WebSocket session through the
``TciRig.connection`` attribute.
"""
from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Callable

import numpy as np

from open_sstv.radio.exceptions import RigCommandError, RigConnectionError

_log = logging.getLogger(__name__)

_READY_TIMEOUT_S: float = 10.0
_RESPONSE_TIMEOUT_S: float = 2.0

# TCI v2.0 audio header (AetherSDR / ExpertSDR3 format): 16 × uint32 LE = 64 bytes
# struct TciAudioHeader { receiver, sampleRate, format, codec, crc, length, type, channels, reserved[8] }
_AUDIO_HEADER_FMT = "<8I"   # first 8 fields; reserved[8] follows but is unused
_AUDIO_HEADER_SIZE = 64     # total header size including reserved
_AUDIO_TYPE_RX = 1          # type=1 is RX_AUDIO
_AUDIO_FMT_INT16 = 0
_AUDIO_FMT_FLOAT32 = 3


# ---------------------------------------------------------------------------
# Shared connection
# ---------------------------------------------------------------------------

class TciConnection:
    """Thread-safe WebSocket connection for the TCI protocol.

    ``send`` is protected by a lock so any thread can issue commands
    concurrently.  The recv loop runs on a daemon thread and dispatches
    incoming text events and binary audio frames to registered callbacks.

    Callbacks are called on the recv thread — keep them fast and
    non-blocking.  Qt signal emissions from callbacks are safe because Qt
    queues them for delivery on the owning thread.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._ws = None  # websocket.WebSocket, opened in connect()
        self._send_lock = threading.Lock()
        self._recv_thread: threading.Thread | None = None
        self._closed = False

        self._text_callbacks: list[Callable[[str], None]] = []
        self._audio_callbacks: list[Callable[[np.ndarray, int], None]] = []

        # Set when "READY:" is received.
        self._ready_event = threading.Event()
        # RX audio sample rate from "rx_samplerate:0,<hz>;" event.
        self._sample_rate: int = 48_000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def is_alive(self) -> bool:
        """True if the WebSocket is open and the recv thread is running."""
        t = self._recv_thread
        return (
            self._ws is not None
            and not self._closed
            and t is not None
            and t.is_alive()
        )

    def connect(self, timeout_s: float = _READY_TIMEOUT_S) -> None:
        """Open the WebSocket and wait for the server ``READY:`` message."""
        try:
            import websocket  # type: ignore[import]  # noqa: PLC0415
        except ImportError as exc:
            raise RigConnectionError(
                "websocket-client is not installed — run: pip install websocket-client"
            ) from exc

        url = f"ws://{self._host}:{self._port}"
        _log.info("TCI: connecting to %s", url)
        try:
            self._ws = websocket.WebSocket()
            self._ws.connect(url, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            raise RigConnectionError(f"TCI WebSocket connect failed: {exc}") from exc

        self._closed = False
        self._ready_event.clear()

        self._recv_thread = threading.Thread(
            target=self._recv_loop, name="tci-recv", daemon=True
        )
        self._recv_thread.start()

        if not self._ready_event.wait(timeout=timeout_s):
            self.disconnect()
            raise RigConnectionError(
                f"TCI: server at {self._host}:{self._port} did not send READY: "
                f"within {timeout_s:.0f} s"
            )
        _log.info("TCI: READY — sample rate %d Hz", self._sample_rate)

    def disconnect(self) -> None:
        """Close the WebSocket and stop the recv thread."""
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        t = self._recv_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def send(self, command: str) -> None:
        """Send a TCI text command. Thread-safe."""
        if self._ws is None:
            raise RigConnectionError("TCI: not connected")
        with self._send_lock:
            try:
                self._ws.send(command)
            except Exception as exc:  # noqa: BLE001
                raise RigConnectionError(f"TCI send failed: {exc}") from exc

    def send_binary(self, data: bytes) -> None:
        """Send a binary WebSocket frame. Thread-safe."""
        if self._ws is None:
            raise RigConnectionError("TCI: not connected")
        with self._send_lock:
            try:
                self._ws.send_binary(data)
            except Exception as exc:  # noqa: BLE001
                raise RigConnectionError(f"TCI binary send failed: {exc}") from exc

    def send_tx_audio_chunk(
        self,
        samples_f32: np.ndarray,
        sample_rate: int | None = None,
    ) -> None:
        """Send one chunk of float32 mono TX audio as a TCI v2.0 binary frame.

        Parameters
        ----------
        samples_f32:
            PCM audio normalised to ``[-1.0, 1.0]`` float32.
        sample_rate:
            Sample rate of the audio in *samples_f32*.  Defaults to
            ``self._sample_rate`` (the RX sample rate reported by the server)
            but callers should pass their actual generation rate so the
            header is accurate even if the server reports a different RX rate.

        Builds the 64-byte ``TciAudioHeader`` with ``type=TX_AUDIO (2)``,
        ``format=float32 (3)``, ``channels=1``, then appends the payload
        and sends a binary WebSocket frame.  Thread-safe (protected by the
        same send lock as :meth:`send`).

        **Silence dither:** AetherSDR uses a heuristic to detect WSJT-X's
        interleaved-stereo layout by checking whether consecutive sample pairs
        are within 1 × 10⁻⁶ of each other.  Silence frames (all zeros from
        CW inter-element gaps) always satisfy this test and are mis-classified
        as stereo, causing them to be processed as ``n/2`` stereo pairs and
        played at half duration — which corrupts CW timing.  We must patch
        individual samples, not just all-zero chunks: a mixed chunk (CW tone
        followed by inter-element silence) still has zero-pairs in its silent
        portion, which is enough to trigger the heuristic.  Alternating
        ±1 × 10⁻⁵ dither (~−100 dBFS, inaudible) replaces every near-zero
        sample so every pair (even_idx, odd_idx) = (+1e-5, −1e-5) — never
        equal — regardless of where the tone/silence boundary falls.
        """
        declared_rate = sample_rate if sample_rate is not None else self._sample_rate
        samples_f32 = samples_f32.astype(np.float32)
        # Per-sample dither to defeat the AetherSDR stereo-detection heuristic.
        near_zero = np.abs(samples_f32) < 1e-6
        if np.any(near_zero):
            samples_f32 = samples_f32.copy()
            idx = np.arange(len(samples_f32), dtype=np.int32)
            samples_f32[near_zero] = np.where(
                idx[near_zero] % 2 == 0, np.float32(1e-5), np.float32(-1e-5)
            )
        n = len(samples_f32)
        header = struct.pack(
            "<16I",
            0,              # receiver
            declared_rate,  # sampleRate — must match the actual generation rate
            3,              # format: float32
            0,              # codec: uncompressed
            0,              # crc: unused
            n,              # length: total sample count
            2,              # type: TX_AUDIO
            1,              # channels: mono
            0, 0, 0, 0, 0, 0, 0, 0,  # reserved[8]
        )
        self.send_binary(header + samples_f32.tobytes())

    def register_text_callback(self, fn: Callable[[str], None]) -> None:
        """Register *fn* to be called for every text event received."""
        self._text_callbacks.append(fn)

    def unregister_text_callback(self, fn: Callable[[str], None]) -> None:
        try:
            self._text_callbacks.remove(fn)
        except ValueError:
            pass

    def register_audio_callback(
        self, fn: Callable[[np.ndarray, int], None]
    ) -> None:
        """Register *fn* to be called for each binary audio frame.

        ``fn(samples: np.ndarray[int16], sample_rate: int)`` — runs on the
        recv thread.
        """
        self._audio_callbacks.append(fn)

    def unregister_audio_callback(
        self, fn: Callable[[np.ndarray, int], None]
    ) -> None:
        try:
            self._audio_callbacks.remove(fn)
        except ValueError:
            pass

    # --- recv loop ----------------------------------------------------------

    def _recv_loop(self) -> None:
        while not self._closed and self._ws is not None:
            try:
                opcode, data = self._ws.recv_data()
            except Exception:  # noqa: BLE001
                if not self._closed:
                    _log.warning("TCI: recv error — connection lost")
                break
            if opcode == 1:  # TEXT
                text = data.decode("utf-8", errors="replace").strip()
                self._dispatch_text(text)
            elif opcode == 2:  # BINARY
                self._dispatch_audio(data)

    def _dispatch_text(self, text: str) -> None:
        for msg in text.split(";"):
            msg = msg.strip()
            if not msg:
                continue
            if msg.upper() == "READY":
                self._ready_event.set()
            elif msg.lower().startswith("rx_samplerate:"):
                # rx_samplerate:0,48000
                try:
                    self._sample_rate = int(msg.split(",")[-1])
                except (ValueError, IndexError):
                    pass
            for cb in list(self._text_callbacks):
                try:
                    cb(msg)
                except Exception:  # noqa: BLE001
                    pass

    def _dispatch_audio(self, data: bytes) -> None:
        """Parse a TCI v2.0 binary audio frame and invoke audio callbacks.

        AetherSDR header layout (64 bytes, 16 × uint32 LE):
          [0] receiver   — TRX index (ignored)
          [1] sampleRate — Hz
          [2] format     — 0=int16, 3=float32
          [3] codec      — 0 (uncompressed, ignored)
          [4] crc        — 0 (ignored)
          [5] length     — number of samples per channel
          [6] type       — 0=IQ, 1=RX_AUDIO, 2=TX_AUDIO, 3=TX_CHRONO
          [7] channels   — 1=mono, 2=stereo
          [8-15] reserved

        Callbacks receive float32 mono ndarray + sample rate.
        """
        if len(data) < _AUDIO_HEADER_SIZE:
            return

        _receiver, sample_rate, fmt, _codec, _crc, length, type_, channels = \
            struct.unpack_from(_AUDIO_HEADER_FMT, data, 0)

        # Only handle RX audio; ignore IQ (0), TX_AUDIO (2), TX_CHRONO (3)
        if type_ != _AUDIO_TYPE_RX:
            return

        if sample_rate > 0:
            self._sample_rate = sample_rate

        payload = data[_AUDIO_HEADER_SIZE:]
        if not payload:
            return

        # Decode samples to float32
        if fmt == _AUDIO_FMT_INT16:
            n = (len(payload) // 2) * 2
            samples = np.frombuffer(payload[:n], dtype="<i2").astype(np.float32) / 32768.0
        elif fmt == _AUDIO_FMT_FLOAT32:
            n = (len(payload) // 4) * 4
            samples = np.frombuffer(payload[:n], dtype="<f4").copy()
        else:
            return  # unsupported format

        # Trim to declared sample count (length × channels)
        declared = int(length) * int(channels)
        if 0 < declared < len(samples):
            samples = samples[:declared]

        # Downmix stereo to mono
        if channels == 2:
            n2 = (len(samples) // 2) * 2
            samples = (samples[:n2:2] + samples[1:n2:2]) * 0.5

        sr = int(sample_rate) if sample_rate > 0 else self._sample_rate
        for cb in list(self._audio_callbacks):
            try:
                cb(samples, sr)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# TCI Rig
# ---------------------------------------------------------------------------

class TciRig:
    """Rig backend using the TCI WebSocket protocol.

    Satisfies ``open_sstv.radio.base.Rig`` (duck-typed Protocol) for any
    SDR or transceiver that runs a TCI 1.x server.

    The ``connection`` attribute is a shared ``TciConnection`` that
    ``TciInputStreamWorker`` can use so both control and audio travel over
    the same WebSocket session.

    TCI is push-based: the server sends state changes as they happen rather
    than responding to query commands.  All getters return the last value
    received.  ``_freq_received``, ``_mode_received``, and ``_ptt_received``
    flags track whether the cache has been populated at least once (it is,
    during the post-READY: state burst).  If a getter is called before the
    first push arrives it sends the relevant query and waits briefly.
    """

    name: str

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self.name = f"TCI {host}:{port}"
        self.connection = TciConnection(host, port)

        # Cached state updated by incoming text events.
        self._last_freq: int = 0
        self._last_mode: str = ""
        self._last_ptt: bool = False
        self._strength: int = 0

        # Whether each cache entry has been populated by the server yet.
        self._freq_received = False
        self._mode_received = False
        self._ptt_received = False

        # Events used as a fallback when cache is cold (first call before
        # the post-READY: burst has arrived).
        self._freq_event = threading.Event()
        self._mode_event = threading.Event()
        self._ptt_event = threading.Event()

        self.connection.register_text_callback(self._on_text)

    # --- Rig Protocol -------------------------------------------------------

    def open(self) -> None:
        self.connection.connect()

    def close(self) -> None:
        try:
            self.connection.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def get_freq(self) -> int:
        if self._freq_received:
            return self._last_freq
        # Cache cold — request and wait for first push.
        self._freq_event.clear()
        self.connection.send("vfo:0,0;")
        if self._freq_event.wait(timeout=_RESPONSE_TIMEOUT_S):
            return self._last_freq
        raise RigCommandError("TCI get_freq timed out")

    def set_freq(self, hz: int) -> None:
        self.connection.send(f"vfo:0,0,{hz};")

    def get_mode(self) -> tuple[str, int]:
        if self._mode_received:
            return (self._last_mode, 2700)
        self._mode_event.clear()
        self.connection.send("modulation:0;")
        if self._mode_event.wait(timeout=_RESPONSE_TIMEOUT_S):
            return (self._last_mode, 2700)
        raise RigCommandError("TCI get_mode timed out")

    def set_mode(self, mode: str, passband_hz: int) -> None:
        del passband_hz
        self.connection.send(f"modulation:0,{mode.upper()};")

    def get_ptt(self) -> bool:
        if self._ptt_received:
            return self._last_ptt
        self._ptt_event.clear()
        self.connection.send("trx:0;")
        if self._ptt_event.wait(timeout=_RESPONSE_TIMEOUT_S):
            return self._last_ptt
        raise RigCommandError("TCI get_ptt timed out")

    def set_ptt(self, on: bool) -> None:
        if on:
            # "tci" source routes audio through the TCI WebSocket (our TX
            # audio frames) and triggers AetherSDR's TCI-routed TX path.
            # Without the source argument the server uses hardware routing
            # and ignores our binary audio frames entirely.
            self.connection.send("trx:0,true,tci;")
        else:
            # Unkey unconditionally stops TX_CHRONO and releases PTT
            # regardless of the original keying source.
            self.connection.send("trx:0,false;")

    def get_strength(self) -> int:
        return self._strength

    def ping(self) -> None:
        """Verify the connection is still live.

        TCI is push-based, so there is no reliable round-trip query to use
        here.  ``open()`` already proved the connection by waiting for
        ``READY:``.  We simply check the WebSocket socket is open and the
        recv thread is still running.
        """
        if not self.connection.is_alive:
            raise RigConnectionError("TCI connection lost")

    # --- internal -----------------------------------------------------------

    def _on_text(self, msg: str) -> None:
        lower = msg.lower()
        if lower.startswith("vfo:"):
            # vfo:0,0,14074000
            try:
                self._last_freq = int(msg.split(",")[-1])
                self._freq_received = True
                self._freq_event.set()
            except (ValueError, IndexError):
                pass
        elif lower.startswith("modulation:"):
            # modulation:0,USB
            try:
                self._last_mode = msg.split(",")[-1].strip()
                self._mode_received = True
                self._mode_event.set()
            except (ValueError, IndexError):
                pass
        elif lower.startswith("trx:"):
            # trx:0,false  or  trx:0,true,tci  — field [1] is the bool,
            # field [2] (optional) is the keying source; ignore it.
            try:
                parts = msg.split(",")
                val = parts[1].strip().lower() if len(parts) >= 2 else ""
                self._last_ptt = val == "true"
                self._ptt_received = True
                self._ptt_event.set()
            except (ValueError, IndexError):
                pass
        elif lower.startswith("rx_smeter:"):
            # rx_smeter:0,0,-73
            try:
                self._strength = int(float(msg.split(",")[-1]))
            except (ValueError, IndexError):
                pass


__all__ = ["TciConnection", "TciRig"]
