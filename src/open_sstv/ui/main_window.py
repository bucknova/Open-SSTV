# SPDX-License-Identifier: GPL-3.0-or-later
"""Top-level Qt main window.

Composes the TX panel (left), RX panel (right), three worker threads
(TX playback, RX audio capture, RX decode), a menu bar (File > Settings /
Quit), optional rig polling on a 1 Hz QTimer, and auto-save of decoded
images to the configured directory.

Threading
---------

Five threads total:

* GUI thread — owns the window, the two panels, and Qt's event loop.
* TX worker thread — owns ``TxWorker``, runs encode + ``play_blocking``.
* RX audio thread — owns ``InputStreamWorker``, runs PortAudio + queue
  drain timer.
* RX decode thread — owns ``RxWorker``, runs ``Decoder.feed`` on
  flushed batches.
* Rig poll thread — owns ``_RigPollWorker``, runs blocking get_freq /
  get_mode / get_strength calls so the GUI never stalls on serial I/O.

Splitting audio capture from decoding is deliberate: a slow decode
pass must not stall the PortAudio queue drain. Each worker runs on a
dedicated ``QThread`` with its own event loop, and signals cross the
boundaries via Qt's automatic queued connections.

Signal flow
-----------

TX (unchanged from Phase 1)::

    tx_panel.transmit_requested        ──> tx_worker.transmit
    tx_panel.stop_requested            ──> _on_stop_requested (direct, UI thread)
    tx_panel.export_to_audio_requested ──> _on_export_to_audio_requested
        spawns OfflineEncodeWorker on its own QThread (one-shot)
    tx_worker.transmission_*           ──> _on_tx_*        ──> tx_panel
    tx_worker.error                    ──> _on_tx_error    ──> status bar

RX::

    rx_panel.capture_requested  ──> _on_capture_requested (UI thread)
        (True)  ──> audio_worker.start
        (False) ──> audio_worker.stop

    audio_worker.started    ──> rx_panel.set_capturing(True)
    audio_worker.stopped    ──> rx_worker.flush + rx_panel.set_capturing(False)
    audio_worker.chunk_ready──> rx_worker.feed_chunk
    audio_worker.error      ──> _on_rx_error  ──> status bar

    rx_panel.clear_requested           ──> rx_worker.reset + rx_panel.clear
    rx_panel.decode_audio_file_requested ──> _on_decode_audio_file_requested
        spawns OfflineDecodeWorker on its own QThread (one-shot)
    rx_worker.image_started ──> rx_panel.show_image_started
    rx_worker.image_complete──> rx_panel.show_image_complete
    rx_worker.error         ──> _on_rx_error

The ``stopped → flush`` wire is what makes the tail of an in-flight
image decode even when the user clicks Stop mid-transmission: the
audio worker drains its queue before emitting ``stopped``, and
``RxWorker.flush`` forces any buffered scratch samples through the
decoder one last time.

Lifecycle
---------

``closeEvent`` tears everything down in a safe order: stop TX, stop RX
audio capture, flush the decoder, quit and join all three worker
threads, and finally close the rig.
"""
from __future__ import annotations

import datetime
import logging
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

_log = logging.getLogger(__name__)

from PySide6.QtCore import (
    QEventLoop,
    QMetaObject,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from open_sstv import __version__
from open_sstv.audio.devices import (
    AudioDevice,
    find_input_device_by_name,
    find_output_device_by_name,
)
from open_sstv.audio.input_stream import (
    DEFAULT_BLOCKSIZE,
    InputStreamWorker,
)
from open_sstv.audio.pipewire_route import PipeWireSink, find_pipewire_sink_by_name
from open_sstv.audio.tci_input_stream import TciInputStreamWorker
from open_sstv.config.schema import AppConfig
from open_sstv.config.store import last_corrupt_backup, load_config, save_config
from open_sstv.config.templates import load_templates
from open_sstv.core.modes import Mode
from open_sstv.logbook import QSO, LogbookCoordinator, QsoLoggingError, UdpQsoLogger
from open_sstv.radio.band_plan import mode_family, resolve_tune_mode
from open_sstv.radio.base import ManualRig, Rig, RigConnectionMode
from open_sstv.radio.exceptions import RigCommandError, RigError
from open_sstv.radio.rigctld import RigctldClient, is_safe_rigctld_arg
from open_sstv.radio.serial_rig import create_serial_rig
from open_sstv.remote import (
    ComposeService,
    ControlPlane,
    EventHub,
    GalleryService,
    RemoteServer,
)
from open_sstv.templates import TokenContext, build_autosave_filename, run_migration
from open_sstv.ui.first_launch_dialog import FirstLaunchDialog
from open_sstv.ui.gallery_dialog import GalleryDialog
from open_sstv.ui.log_qso_dialog import LogQsoDialog
from open_sstv.ui.logbook_dialog import LogbookDialog
from open_sstv.ui.offline_workers import OfflineDecodeWorker, OfflineEncodeWorker
from open_sstv.ui.radio_panel import RadioPanel
from open_sstv.ui.rx_panel import RxPanel
from open_sstv.ui.settings_dialog import SettingsDialog
from open_sstv.ui.tx_panel import TxPanel
from open_sstv.ui.update_checker import UpdateCheckerWorker
from open_sstv.ui.waterfall_widget import WaterfallWindow
from open_sstv.ui.workers import RxWorker, TxWorker

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image as PILImage


def _drain_subprocess_stderr(proc: subprocess.Popen, name: str) -> None:
    """Pump *proc*'s stderr into the app log on a daemon thread.

    v0.4.0 audit high #2: a child launched with ``stderr=PIPE`` that
    nobody reads wedges once the OS pipe buffer (~64 KB) fills — the
    child blocks on ``write(2)`` and stops doing its real job.  For
    rigctld that means the daemon stops servicing CAT commands, worst
    case with PTT keyed.  The pump thread is a daemon so it can never
    hold the interpreter open; it exits when the child closes stderr.

    Qt-free and module-level so it's unit-testable without a window.
    """
    if proc.stderr is None:  # not launched with stderr=PIPE — nothing to do
        return

    def _pump() -> None:
        try:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    _log.info("%s: %s", name, line)
        except Exception:  # noqa: BLE001 — the drain must never die loudly
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(
        target=_pump, name=f"{name}-stderr-drain", daemon=True
    ).start()


class _RigPollWorker(QObject):
    """Polls the rig for frequency, mode, and S-meter on a dedicated thread.

    The ``poll`` slot blocks on serial/socket I/O; by running on its own
    ``QThread`` it cannot freeze the GUI regardless of connection quality.
    The GUI thread fires the 1 Hz ``_rig_poll_timer``; that timer's
    ``timeout`` signal is connected here via a queued connection so the
    actual blocking call happens on this object's thread.
    """

    poll_result = Signal(int, str, int)  # (freq_hz, mode_name, strength_db)
    poll_error = Signal()
    #: Emitted exactly once when ``_POLL_FAIL_THRESHOLD`` consecutive polls
    #: fail.  ``MainWindow`` stops the timer and reverts to disconnected state.
    radio_disconnected = Signal()
    #: Emitted when a band-plan ``tune()`` call fails — a rejected/timed-out
    #: CAT command, or a frequency readback that doesn't match what was
    #: requested.  Payload is a human-readable reason for the status bar.
    tune_failed = Signal(str)

    #: How many consecutive poll failures trigger the auto-disconnect signal.
    _POLL_FAIL_THRESHOLD: int = 3

    def __init__(self) -> None:
        super().__init__()
        self._rig: Rig = ManualRig()
        self._consecutive_errors: int = 0
        #: Latches once per rig so an unsupported S-meter is logged a single
        #: time instead of once per second.
        self._strength_unsupported: bool = False

    def set_rig(self, rig: Rig) -> None:
        """Swap the rig reference. GIL-safe for a plain attribute store."""
        self._rig = rig
        self._consecutive_errors = 0
        self._strength_unsupported = False

    @Slot()
    def poll(self) -> None:
        """Read freq/mode/strength from the rig. Blocks; runs on worker thread."""
        try:
            freq = self._rig.get_freq()
            mode_name, _ = self._rig.get_mode()
        except Exception:  # noqa: BLE001 — catch RigError + any raw OSError/termios.error
            self._consecutive_errors += 1
            self.poll_error.emit()
            if self._consecutive_errors == self._POLL_FAIL_THRESHOLD:
                self.radio_disconnected.emit()
            return
        # The S-meter is cosmetic and not every backend implements it (a
        # rigctld backend without a STRENGTH level answers ``RPRT -11``).
        # Polling it in the same try as freq/mode meant an unsupported
        # S-meter tripped the 3-strike auto-disconnect and dropped a rig
        # whose PTT and frequency control were working fine.  Failure here
        # is now non-fatal: report 0 and keep the connection.
        try:
            strength = self._rig.get_strength()
        except Exception as exc:  # noqa: BLE001 — optional reading, never fatal
            if not self._strength_unsupported:
                self._strength_unsupported = True
                _log.info(
                    "rig S-meter unavailable (%s) — continuing without it", exc
                )
            strength = 0
        self._consecutive_errors = 0
        self.poll_result.emit(freq, mode_name, strength)

    @Slot(int, str, int)
    def tune(self, freq_hz: int, mode: str, passband_hz: int) -> None:
        """Send a frequency + mode command to the rig.

        Runs on the rig-poll thread (queued from ``MainWindow._request_tune``
        signal) so it cannot race with the 1 Hz ``poll`` slot — both live on
        the same event loop.  Failures emit ``tune_failed`` so the GUI thread
        can surface them; the poll cycle still catches any persistent
        connection problem within 3 s independently.

        Mode is only re-sent if the current mode's **sideband family** differs
        from the target's.  This preserves data-variant modes (IC-7300
        ``USB-D``, Yaesu ``USB-DATA``, Kenwood / Hamlib ``PKTUSB``, …) when
        the band-plan entry's family matches what the user is already on.
        Without this, every band-plan pick would clobber the rig's data
        routing and re-enable the speech processor.  Band-edge crossings
        that flip sideband (e.g. 20 m USB → 40 m LSB) still switch correctly
        because the family changes.

        Frequency is verified with a readback: ``KenwoodRig``/``YaesuRig``
        set commands are fire-and-forget (the radio sends no response, so
        the CAT write itself can't detect a rejection — dial/VFO lock,
        memory-mode display, TX-inhibit/band edge, …), so without this check
        a rejected frequency change is silently indistinguishable from
        success.  A readback of ``0`` means the backend doesn't report
        frequency at all (e.g. ``SerialPttRig``) and is not treated as a
        mismatch.
        """
        try:
            self._rig.set_freq(freq_hz)
            actual_freq = self._rig.get_freq()
            if actual_freq and actual_freq != freq_hz:
                raise RigCommandError(
                    f"radio still at {actual_freq} Hz — frequency change rejected "
                    "(check VFO/dial lock, memory mode, or band-edge limits)"
                )
            if mode:
                try:
                    current_mode, _ = self._rig.get_mode()
                except Exception as exc:  # noqa: BLE001 — same tolerance as poll()
                    _log.debug("tune: get_mode failed, assuming mode switch needed: %s", exc)
                    current_mode = ""
                if mode_family(current_mode) != mode_family(mode):
                    self._rig.set_mode(mode, passband_hz)
        except Exception as exc:  # noqa: BLE001 — same tolerance as poll()
            # M10: log so failures are visible in OPEN_SSTV_DEBUG=1, and emit
            # tune_failed so the GUI thread can also show it to the user —
            # previously this was logged only, and the "Tuning to…" status
            # message wasn't corrected, so a rejected tune looked identical
            # to a successful one.  Persistent connection loss is still
            # surfaced via the next poll cycle within ~3 s.
            _log.warning(
                "tune to %d Hz (%s) failed: %s", freq_hz, mode or "mode unchanged", exc
            )
            self.tune_failed.emit(str(exc))


class _RigConnectWorker(QObject):
    """One-shot: runs rig.open() + rig.ping() on a background thread.

    Prevents the GUI from freezing for up to ~4 s on unresponsive radios
    (OP2-02).  Caller supplies a ``threading.Event``; when set the worker
    silently discards any pending emit so a timeout or cancel can win the
    race without a spurious success/error reaching the GUI.
    """

    succeeded = Signal(object)  # emits the connected Rig instance
    failed = Signal(str)        # emits a human-readable error message

    def __init__(self, rig: Rig, cancel: threading.Event) -> None:
        super().__init__()
        self._rig = rig
        self._cancel = cancel

    def _close_quietly(self) -> None:
        """Best-effort close of a rig we opened but nobody will use."""
        try:
            self._rig.close()
        except Exception:  # noqa: BLE001 — already abandoned; never raise
            pass

    @Slot()
    def run(self) -> None:
        try:
            self._rig.open()
            if self._cancel.is_set():
                # v0.4.0 audit high #7: the GUI timeout/cancel won the
                # race but open() succeeded anyway — close what we just
                # opened or it leaks for the process lifetime (a leaked
                # exclusive COM handle on Windows makes every subsequent
                # Connect fail until the app restarts; a leaked TCI
                # socket keeps a recv daemon thread alive per attempt).
                self._close_quietly()
                return
            self._rig.ping()
            if self._cancel.is_set():
                self._close_quietly()
                return
            # Log the outcome: a successful connect left no trace at all, so
            # a bug report's log couldn't show whether the rig ever attached.
            _log.info("rig connected: %s", getattr(self._rig, "name", self._rig))
            self.succeeded.emit(self._rig)
        except RigError as exc:
            _log.warning("rig connect failed: %s", exc, exc_info=True)
            try:
                self._rig.close()
            except Exception:  # noqa: BLE001
                pass
            if not self._cancel.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            _log.warning("rig connect failed (unexpected): %s", exc, exc_info=True)
            try:
                self._rig.close()
            except Exception:  # noqa: BLE001
                pass
            if not self._cancel.is_set():
                self.failed.emit(str(exc))


class _RigConnectRelay(QObject):
    """Receives _RigConnectWorker signals on the GUI thread.

    Plain Python callables (lambdas) have no QObject thread affinity, so
    PySide6 cannot guarantee which thread delivers a QueuedConnection event
    to them — in practice the delivery lands on the worker thread, where
    every widget mutation in on_success/on_error is silently dropped and
    QTimer.stop() is called from the wrong thread.

    This relay is a QObject that is never moved off the GUI thread.
    AutoConnection from the worker thread therefore automatically promotes
    to QueuedConnection and the slots execute on the GUI event loop.
    """

    def __init__(
        self,
        on_success: Callable[[Rig], None],
        on_error: Callable[[str], None],
        thread: QThread,
        timer: QTimer,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self._on_success = on_success
        self._on_error = on_error
        self._thread = thread
        self._timer = timer
        self._cancel = cancel

    @Slot(object)
    def on_succeeded(self, rig: Rig) -> None:
        if self._cancel.is_set():
            return
        self._cancel.set()  # prevent late timeout from also calling on_error
        self._timer.stop()
        self._thread.quit()
        self._on_success(rig)

    @Slot(str)
    def on_failed(self, message: str) -> None:
        if self._cancel.is_set():
            return
        self._cancel.set()
        self._timer.stop()
        self._thread.quit()
        self._on_error(message)


class MainWindow(QMainWindow):
    """The Phase 2 main window: TX + RX side-by-side, three worker threads."""

    #: How long to wait for rig.open()+ping() before giving up (seconds).
    _CONNECT_TIMEOUT_S: float = 5.0

    #: Private signals used to dispatch ``start``/``stop`` calls onto
    #: the audio worker thread. We can't just call
    #: ``self._audio_worker.start(...)`` directly — that would run the
    #: PortAudio open on the UI thread. Emitting via signals means Qt's
    #: auto-connect promotes the call to a ``QueuedConnection`` and the
    #: slot executes on the worker thread's event loop.
    _request_start_capture = Signal(object, int, int)
    _request_stop_capture = Signal()
    #: Routes the "Clear" action to RxWorker.reset() via a queued connection
    #: so the reset runs on the RX decode thread, not the GUI thread.
    _request_rx_reset = Signal()
    #: Fires on app close to stop the RxWorker's wall-clock watchdog QTimer
    #: on its owning thread. Without it, the timer is still active when
    #: ``_rx_thread.quit()`` returns and the later destructor on the GUI
    #: thread prints "QObject::killTimer: Timers cannot be stopped from
    #: another thread".
    _request_rx_shutdown = Signal()
    #: Dispatch TX calls to the TX worker thread via queued connection.
    #: Direct method calls from the GUI thread would run on the wrong thread.
    _request_transmit = Signal(object, object)  # (PIL.Image, Mode)
    _request_test_tone = Signal()
    #: v0.6 (Phase 3c): a remote confirm() marshals here (from a request
    #: thread) onto the GUI thread, which re-checks the control-plane state
    #: and then keys the rig via ``_request_transmit`` — the "web is just
    #: another Send button" seam.  Carries (image_id, mode-value).
    _remote_tx_request = Signal(str, str)
    #: Gates the RX decoder on/off during TX (queued → RxWorker thread).
    _request_rx_gate = Signal(bool)
    #: Settings-change dispatchers — queued to RxWorker so decoder rebuilds
    #: happen on the worker thread, never racing with feed_chunk.
    _rx_weak_signal_changed = Signal(bool)
    _rx_watchdog_timeout_changed = Signal(int)
    _rx_incremental_decode_changed = Signal(bool)
    _rx_sample_rate_changed = Signal(int)
    #: OP-09: cover the previously-direct-call settings too so every
    #: per-worker setting flows through a queued connection on its
    #: receiver's event loop.  Symmetry > convenience.
    _rx_final_slant_correction_changed = Signal(bool)
    _tx_sample_rate_changed = Signal(int)
    #: Relays a band-plan tune request from the GUI thread to the rig-poll
    #: thread.  Qt auto-promotes cross-thread connections to QueuedConnection,
    #: so ``_RigPollWorker.tune`` executes on its own event loop — safely
    #: serialised with the 1 Hz ``poll`` slot on the same thread.
    _request_tune = Signal(int, str, int)  # (freq_hz, rig_mode, passband_hz)
    #: Triggers the one-shot update check on the update worker thread.
    _request_update_check = Signal()

    def __init__(
        self,
        rig: Rig | None = None,
        output_device: AudioDevice | PipeWireSink | int | None = None,
        input_device: AudioDevice | int | None = None,
        config: AppConfig | None = None,
        parent: QMainWindow | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open-SSTV")
        # v0.3.5: default geometry tuned so the TX panel opens wide
        # enough for exactly 4 template gallery cards in a row at the
        # 140 px max thumbnail width with the 8 px flow-layout gutter.
        # Initial splitter sizes apply the 640 / 540 split (see below);
        # users can resize freely after launch and the gallery flow
        # layout reflows to fit more or fewer cards per row.
        self.resize(1280, 720)

        # Only consult ``last_corrupt_backup()`` (below) when we actually
        # loaded the config from disk.  An injected config (tests,
        # embedded use) has no corrupt-backup relationship, and reading
        # the process-global would otherwise surface a stale result from
        # an unrelated prior ``load_config`` call.
        loaded_config_from_disk = config is None
        self._config = config if config is not None else load_config()

        # Rig starts as ManualRig (no-op). The user clicks "Connect Rig"
        # in the radio panel to establish a live rigctld link; the settings
        # dialog configures host/port.
        self._rig: Rig = rig if rig is not None else ManualRig()

        # Resolve saved device names from config to real AudioDevice objects.
        # If the caller passed explicit devices, use those; otherwise look up
        # what the user last selected in Settings.
        # OP-18: track whether a saved-but-missing device fell back to the
        # system default so we can surface a status-bar notice once the
        # status bar exists later in __init__.
        self._missing_devices: list[str] = []
        if output_device is None:
            output_device = find_output_device_by_name(
                self._config.audio_output_device
            )
            # Not a PortAudio/ALSA device by that name — try a PipeWire
            # sink (e.g. a user's virtual "Radio" routing sink). PortAudio
            # only exposes those under its JACK host API, which is unsafe
            # to write to directly (see audio/pipewire_route.py); TxWorker
            # routes a PipeWireSink via pactl instead of opening it as a
            # PortAudio device.
            if output_device is None:
                output_device = find_pipewire_sink_by_name(
                    self._config.audio_output_device
                )
            if output_device is None and self._config.audio_output_device:
                self._missing_devices.append(
                    f"output '{self._config.audio_output_device}'"
                )
        if input_device is None:
            input_device = find_input_device_by_name(
                self._config.audio_input_device
            )
            if input_device is None and self._config.audio_input_device:
                self._missing_devices.append(
                    f"input '{self._config.audio_input_device}'"
                )
        self._input_device = input_device
        self._rigctld_proc: subprocess.Popen | None = None
        self._waterfall_window: WaterfallWindow | None = None
        self._capture_running: bool = False
        self._last_abort_was_watchdog: bool = False
        #: Set by _on_audio_device_lost so _on_rx_stopped can re-show the
        #: disconnect message instead of clobbering it with "Capture stopped."
        self._last_rx_disconnect_msg: str = ""
        #: Set by _on_rx_audio_error when stream-open fails so _on_rx_stopped
        #: can re-show the error instead of overwriting with "Not listening…".
        self._last_rx_audio_error_msg: str = ""
        #: When True, RxWorker status_update signals are silently dropped so
        #: "Listening… Xs buffered" updates can't overwrite disconnect or stopped
        #: messages.  Cleared by _on_rx_started when the stream is confirmed up.
        self._suppress_rx_status_updates: bool = True
        #: Watchdog budget (seconds) of the most recently fired TX watchdog,
        #: forwarded by ``TxWorker.watchdog_fired``.  Used by
        #: ``_on_tx_aborted`` to format a precise "exceeded N s" message
        #: instead of hardcoding the value (the budget is now per-
        #: transmission, see ``_compute_playback_watchdog_s``).
        self._last_watchdog_duration_s: float = 0.0
        self._last_tx_was_test_tone: bool = False
        #: Set by _on_tx_error so _on_tx_aborted doesn't wipe the error
        #: message with "Transmission aborted." before the user can read it.
        self._tx_error_pending: bool = False
        #: H7: tracks the closure currently connected to
        #: ``RxWorker.reset_done`` so a rapid Start / Stop / Start can
        #: disconnect-by-reference instead of nuking all connections
        #: (which fires a noisy ``RuntimeWarning`` from PySide6 when
        #: there's nothing to disconnect).
        self._start_once_closure: object | None = None
        #: M16: set by ``_on_audio_device_lost`` so ``_start_once``
        #: knows to re-resolve the PortAudio index for the saved device
        #: name (USB replug typically reassigns the index).  Avoids the
        #: 50–500 ms ``sd.query_devices()`` GUI-thread freeze on every
        #: capture start in the common case where nothing changed.
        self._input_device_needs_relookup: bool = False
        #: v0.2.8: latest TX image captured (after banner compositing, before
        #: encoding) so it can be auto-saved on ``transmission_complete``
        #: when ``autosave_tx`` is enabled.  Cleared after each save so a
        #: follow-up test tone (which never emits ``tx_image_prepared``)
        #: cannot accidentally re-save the previous real transmission.
        self._last_tx_image: PILImage | None = None
        self._last_tx_mode: Mode | str | None = None
        #: Raw RX audio buffer set by ``_on_rx_audio_ready`` and consumed by
        #: ``_on_rx_image_complete``.  Both fire from the same worker ``_dispatch``
        #: call, so the audio is always available when the image handler runs.
        self._pending_rx_audio: tuple | None = None  # (audio_f64, sample_rate)
        #: OP-47: remembers whether the 1 Hz rig-poll timer was running when
        #: TX started, so ``_unlock_rig_controls`` can resume it only if the
        #: rig is still connected. The poll is *suspended* for the duration
        #: of every TX — without this, CAT reads (get_freq / get_mode /
        #: get_strength) can interleave with PTT writes on the same serial
        #: port, which on Windows triggers a USB-CODEC renegotiation that
        #: drops both the virtual COM port and the rig's USB audio device.
        #: Same issue is why WSJT-X / JS8Call / MMSSTV all gate polling
        #: during TX.
        self._rig_poll_was_active: bool = False

        # --- Logbook (v0.4) ---
        #: Builds draft QSOs at TX/RX completion and owns the SQLite
        #: store (opened lazily on first use).  The lambda indirection
        #: matters: ``self._config`` is *replaced* on settings save, so
        #: a direct reference would go stale.
        self._logbook_coordinator = LogbookCoordinator(lambda: self._config)
        self._logbook_dialog: LogbookDialog | None = None
        #: v0.6 (Phase 1): embedded read-only remote gallery server, started
        #: on launch when ``remote_enabled`` and stopped in ``closeEvent``.
        #: The lambda keeps the service reading the live config across saves.
        self._remote_server: RemoteServer | None = None
        #: Live-event fan-out, created once and shared across server
        #: restarts so the RX→browser bridge always publishes to the
        #: hub the current server is draining.
        self._remote_hub = EventHub()
        self._remote_service = GalleryService(lambda: self._config)
        #: v0.6 (Phase 4): server-side compositor + in-memory staging store.
        #: Created here (not in RemoteServer) so the stage endpoint and the
        #: transmit path below share the same staged images.
        self._remote_compose = ComposeService(lambda: self._config)
        #: Throttle for the live RX preview push (monotonic seconds).
        self._remote_last_preview_t = 0.0
        #: v0.6 (Phase 3c): remote-TX control plane — the reference monitor
        #: for keying the rig from a browser.  Callbacks reference workers
        #: lazily (called at runtime, after they exist).  ``unkey`` is the
        #: thread-safe ``request_stop`` so the dead-man's-switch works even
        #: if the GUI loop stalls; ``transmit`` marshals onto the GUI thread
        #: via ``_remote_tx_request`` so the key-down is re-checked there.
        #: The RemoteServer's tick thread drives its dead-man's-switch.
        self._remote_control = ControlPlane(
            now=time.monotonic,
            transmit=lambda image_id, mode: self._remote_tx_request.emit(image_id, mode),
            unkey=self._remote_tx_unkey,
            enabled=lambda: self._config.remote_tx_enabled,
            # Remote TX is unattended: require a CAT rig that can be positively
            # keyed/unkeyed.  The no-op manual/VOX backend doesn't qualify —
            # the dead-man's-switch could only stop audio, not drop PTT.
            rig_ready=lambda: not isinstance(self._rig, ManualRig),
        )
        self._remote_tx_request.connect(
            self._on_remote_tx_request, Qt.ConnectionType.QueuedConnection
        )
        #: Persistent status-bar indicator, shown only while the remote
        #: server is running.  Rich-text so it doubles as a click-to-open
        #: link to the local gallery URL.
        self._remote_status_label = QLabel()
        self._remote_status_label.setTextFormat(Qt.TextFormat.RichText)
        self._remote_status_label.setOpenExternalLinks(True)
        self._remote_status_label.setVisible(False)
        self.statusBar().addPermanentWidget(self._remote_status_label)
        #: v0.5: detached image gallery window, lazily created on first
        #: Tools → Gallery… (Cmd/Ctrl+G).
        self._gallery_dialog: GalleryDialog | None = None
        #: Latest successful rig-poll frequency (Hz), or ``None`` when
        #: no rig is connected.  This is the QSO frequency snapshot: the
        #: poll is suspended during TX (OP-47), so at TX completion this
        #: still holds the pre-TX value — the frequency the contact
        #: actually happened on — and during RX it's ≤1 s old.  Reading
        #: a cache here instead of calling ``get_freq()`` at completion
        #: avoids a CAT read racing the poll thread on the serial port.
        self._last_rig_freq_hz: int | None = None
        #: In-flight capture dialog (window-modal, non-blocking) plus
        #: the context needed at save time: the decoded/transmitted PIL
        #: image (for deferred save-to-disk) and the original Mode (so
        #: a deferred save builds the same filename autosave would).
        #: ``None`` when no capture dialog is open; while one IS open,
        #: further completions are silently written as drafts instead
        #: of stacking dialogs.
        self._capture_context: tuple[LogQsoDialog, PILImage | None, object] | None = None
        #: Set at the top of ``closeEvent`` (audit #3): the shutdown
        #: drain can deliver one final queued ``image_complete``, and
        #: the capture flow must not run against a closing window /
        #: closed store.
        self._closing: bool = False
        #: Set by ``closeEvent`` only once its teardown has actually run to
        #: completion.  Deliberately NOT ``_closing``: that one means "we are
        #: shutting down, stand down from new work" and is set by other code
        #: paths (and by tests) well before any teardown happens, so reusing
        #: it as the re-entry guard would skip teardown entirely and leave
        #: worker threads running into window destruction — a qFatal abort.
        self._teardown_complete: bool = False
        #: Source file of the in-flight offline decode (audit #4): its
        #: mtime stamps the logbook draft instead of "now", and the
        #: draft gets no rig frequency.
        self._offline_decode_source: Path | None = None
        #: Rig whose teardown was deferred because a TX was still
        #: unwinding when the involuntary-disconnect handler ran
        #: (v0.4.0 audit high #1).  The TX worker's ``_unkey_with_retry``
        #: owns the backend until it reports idle; closing it (or
        #: killing rigctld) underneath the retry loop can leave the
        #: radio keyed.  Finished by ``_finish_deferred_rig_teardown``
        #: from the TX completion/abort handlers.
        self._deferred_rig_teardown: Rig | None = None

        # --- Menu bar ---
        self._build_menu_bar()

        # In-flight connect-thread state — cleared by _on_connect_thread_finished
        # and _abort_connect.  All five point at the same logical "current
        # connect attempt"; they are always set and cleared together.
        self._connect_cancel: threading.Event | None = None
        self._connect_thread: QThread | None = None
        self._connect_worker: _RigConnectWorker | None = None
        self._connect_timeout_timer: QTimer | None = None
        self._connect_relay: _RigConnectRelay | None = None

        # In-flight offline encode/decode workers.  Stored as instance
        # attributes (not locals) for the same reason as _connect_worker:
        # PySide6 signal connections only hold a weak ref to the receiver,
        # so a local-variable worker is garbage-collected the moment the
        # slot returns — before QMetaObject.invokeMethod can dispatch the
        # queued call.  The result is a silent failure: status bar shows
        # "Encoding…" then nothing.  See v0.3.10 changelog.
        self._offline_encode_thread: QThread | None = None
        self._offline_encode_worker: OfflineEncodeWorker | None = None
        self._offline_decode_thread: QThread | None = None
        self._offline_decode_worker: OfflineDecodeWorker | None = None

        # --- Radio panel (toolbar strip above TX/RX) ---
        self._radio_panel = RadioPanel(self)
        self._radio_panel.set_callsign(self._config.callsign)
        self._radio_panel.connect_requested.connect(self._on_rig_connect)
        self._radio_panel.disconnect_requested.connect(self._on_rig_disconnect)
        self._radio_panel.cancel_requested.connect(self._on_connect_cancel)
        # Band-plan tune: relay from GUI thread → rig-poll thread via queued
        # connection so the CAT write runs on the same thread as poll(), which
        # prevents them from racing on a shared serial port or WebSocket.
        self._radio_panel.tune_requested.connect(self._on_tune_requested)

        # Run v0.2 → v0.3 template migration once at startup.  Safe to call
        # every launch — it returns immediately if templates are already
        # present (already_populated).  Runs before TxPanel so the gallery
        # has templates to load on first launch.
        try:
            run_migration()
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Template migration failed: %s", exc)

        # Push callsign and full config to TX panel so the gallery can render
        # token-resolved thumbnails on startup.
        self._tx_panel = TxPanel(
            templates=load_templates(),
            default_mode=self._config.default_tx_mode,
            app_config=self._config,
            parent=self,
        )
        self._tx_panel.set_callsign(self._config.callsign)

        # --- Panels inside a horizontal splitter ---
        self._rx_panel = RxPanel(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._tx_panel)
        splitter.addWidget(self._rx_panel)
        # v0.3.5: bias the initial split toward the TX panel so its
        # template gallery shows 4 cards in a row out of the box —
        # 4 × 140 px thumbnails + 3 × 8 px gutters + flow / panel
        # margins ≈ 632 px, so 640 gives a small buffer.  Stretch
        # factors stay equal so subsequent window resizes grow both
        # panels symmetrically.
        splitter.setSizes([640, 540])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        # Stack radio panel + splitter into the central widget.
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._radio_panel)
        central_layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        # --- TX worker on its own thread ---
        self._tx_thread = QThread(self)
        self._tx_thread.setObjectName("sstv-app-tx-worker")
        self._tx_worker = TxWorker(
            rig=self._rig,
            output_device=output_device,
            sample_rate=self._config.sample_rate,
            ptt_delay_s=self._config.ptt_delay_s,
        )
        # v0.1.33: seed worker state from the persisted config BEFORE
        # moveToThread so the workers are born in the state the user
        # left them in.  Prior to this, ``_apply_config`` only ran on
        # Settings dialog save, so the first launch after a fresh edit
        # ignored every field that doesn't have a constructor kwarg
        # (output gain, CW ID, TX banner, etc).  User-reported as "the
        # app does not respect previously set mic gain levels."
        # Direct setter calls while the worker is still on the GUI
        # thread avoid the queued-signal-during-teardown segfault that
        # emitting via ``_apply_config`` from ``__init__`` produced.
        self._tx_worker.set_output_gain(self._config.audio_output_gain)
        self._tx_worker.set_cw_id(
            self._config.cw_id_enabled,
            self._config.callsign,
            self._config.cw_id_wpm,
            self._config.cw_id_tone_hz,
        )
        self._tx_worker.set_tx_banner(
            self._config.tx_banner_enabled,
            self._config.callsign,
            self._config.tx_banner_bg_color,
            self._config.tx_banner_text_color,
            self._config.tx_banner_size,
        )
        # L-2: two-tone test frequencies from Settings (defaults 700/1900 Hz).
        self._tx_worker.set_test_tone_freqs(
            self._config.test_tone_freq_lo,
            self._config.test_tone_freq_hi,
        )
        self._tx_worker.moveToThread(self._tx_thread)
        # Standard Qt cleanup pattern: when the thread finishes, schedule
        # the worker for deletion. Without this the worker is left as an
        # orphaned QObject on a dead thread, and Qt's queued-connection
        # machinery hits "current thread's event dispatcher has already
        # been destroyed" warnings during app shutdown.
        self._tx_thread.finished.connect(self._tx_worker.deleteLater)
        self._tx_thread.start()

        # --- RX audio worker on its own thread ---
        self._audio_thread = QThread(self)
        self._audio_thread.setObjectName("sstv-app-rx-audio")
        self._audio_worker = InputStreamWorker()
        self._audio_worker.moveToThread(self._audio_thread)
        self._audio_thread.finished.connect(self._audio_worker.deleteLater)
        self._audio_thread.start()

        # --- RX decode worker on its own thread ---
        self._rx_thread = QThread(self)
        self._rx_thread.setObjectName("sstv-app-rx-decode")
        self._rx_worker = RxWorker(
            sample_rate=self._config.sample_rate,
            weak_signal=self._config.rx_weak_signal_mode,
            watchdog_timeout_s=self._config.rx_watchdog_timeout_s,
            final_slant_correction=self._config.apply_final_slant_correction,
            incremental_decode=self._config.incremental_decode,
        )
        # v0.1.33: seed RX input gain from config BEFORE moveToThread
        # (see matching TX block above for the full rationale).  This is
        # the direct user-reported symptom — "the app does not respect
        # previously set mic gain levels" — because audio_input_gain
        # wasn't a RxWorker constructor kwarg and the only code path
        # that pushed it to the worker was ``_apply_config``, which
        # only fired when the user re-opened Settings.
        self._rx_worker.set_input_gain(self._config.audio_input_gain)
        self._rx_worker.moveToThread(self._rx_thread)
        self._rx_thread.finished.connect(self._rx_worker.deleteLater)
        self._rx_thread.start()

        # --- Wire TX signals ---
        # panel → flag-setter on GUI thread, then dispatch via queued signal
        self._tx_panel.transmit_requested.connect(self._on_transmit_requested)
        self._tx_panel.stop_requested.connect(self._on_stop_requested)
        # v0.3.13: removed the ``template_composited → set_v3_template_active``
        # wire.  The banner stamp no longer cares whether a v0.3 template is
        # active — if it's enabled in Settings, it always stamps.  The
        # ``template_composited`` signal still fires on selection change in
        # case future code wants to listen, it's just no longer plumbed to
        # the worker's banner gating.
        # v0.3.10: Export to Audio button → offline encode worker.
        self._tx_panel.export_to_audio_requested.connect(
            self._on_export_to_audio_requested
        )
        # v0.4: [Logbook…] button on the QSO bar — same destination as
        # Tools → Logbook… (Cmd/Ctrl+L).
        self._tx_panel.logbook_requested.connect(self._open_logbook)
        # v0.6.7: [External Log] button — UDP-only QSO broadcast, does not touch
        # the local logbook database.
        self._tx_panel.udp_log_requested.connect(self._on_udp_log_requested)
        self._radio_panel.test_tone_requested.connect(self._on_test_tone_requested)
        # Private dispatch signals → worker slots (QueuedConnection across thread)
        self._request_transmit.connect(self._tx_worker.transmit)
        self._request_test_tone.connect(self._tx_worker.transmit_test_tone)
        self._request_rx_gate.connect(self._rx_worker.set_tx_active)
        self._tx_worker.transmission_started.connect(self._on_tx_started)
        self._tx_worker.transmission_progress.connect(self._tx_panel.show_tx_progress)
        self._tx_worker.transmission_progress.connect(self._on_tx_progress)
        self._tx_worker.transmission_complete.connect(self._on_tx_complete)
        self._tx_worker.transmission_aborted.connect(self._on_tx_aborted)
        # v0.6 (Phase 3c): whenever a transmission ends, return the remote
        # control plane to idle (a no-op unless a remote TX was in flight).
        self._tx_worker.transmission_complete.connect(self._remote_tx_ended)
        self._tx_worker.transmission_aborted.connect(self._remote_tx_ended)
        # v0.2.8: stash the composited TX image (banner already applied) so
        # ``_on_tx_complete`` can auto-save it when ``autosave_tx`` is on.
        self._tx_worker.tx_image_prepared.connect(self._on_tx_image_prepared)
        self._tx_worker.watchdog_fired.connect(self._on_watchdog_fired)
        self._tx_worker.error.connect(self._on_tx_error)
        self._tx_worker.error_occurred.connect(self._handle_worker_error)
        self._tx_worker.rig_disconnected.connect(self._on_radio_disconnected)

        # --- Wire RX signals ---
        # Private start/stop dispatch signals → audio worker slots.
        # Cross-thread, so Qt auto-promotes these to QueuedConnection
        # and the PortAudio open runs on the audio worker thread.
        self._request_start_capture.connect(self._audio_worker.start)
        self._request_stop_capture.connect(self._audio_worker.stop)
        self._request_rx_reset.connect(self._rx_worker.reset)
        self._request_rx_shutdown.connect(self._rx_worker.shutdown)
        # Settings dispatchers — connect BEFORE _apply_config is ever called.
        # Because rx_worker lives on rx_thread, Qt auto-promotes these to
        # QueuedConnection, so the decoder rebuilds happen on the worker thread.
        self._rx_weak_signal_changed.connect(self._rx_worker.set_weak_signal)
        self._rx_watchdog_timeout_changed.connect(self._rx_worker.set_watchdog_timeout)
        self._rx_incremental_decode_changed.connect(self._rx_worker.set_incremental_decode)
        self._rx_sample_rate_changed.connect(self._rx_worker.set_sample_rate)
        # OP-09: previously-direct calls now flow through queued signals too.
        self._rx_final_slant_correction_changed.connect(
            self._rx_worker.set_final_slant_correction
        )
        self._tx_sample_rate_changed.connect(self._tx_worker.set_sample_rate)

        # Panel -> window (we translate capture_requested into the
        # dispatch signals above, because ``start`` needs the device
        # argument the panel doesn't know about).
        self._rx_panel.capture_requested.connect(self._on_capture_requested)
        self._rx_panel.clear_requested.connect(self._on_rx_clear)
        self._rx_panel.image_saved.connect(self._on_rx_image_saved)
        self._rx_panel.rx_image_selected.connect(self._tx_panel.set_rx_image)
        # v0.3.10: Decode Audio button → offline decode worker.
        self._rx_panel.decode_audio_file_requested.connect(
            self._on_decode_audio_file_requested
        )
        # v0.4: gallery right-click → Log QSO… — deliberate logging for
        # monitoring stations (most decodes on a calling frequency are
        # other people's exchanges; this logs only the one that's yours).
        self._rx_panel.log_qso_requested.connect(self._on_gallery_log_qso)

        # Audio worker -> RX worker (chunks flow across the thread
        # boundary via queued connection; Qt handles the marshalling).
        self._audio_worker.chunk_ready.connect(self._rx_worker.feed_chunk)
        # Tail flush: when audio stops, force whatever's left in the
        # scratch buffer through the decoder so the last sub-second of
        # an in-flight image isn't discarded.
        self._audio_worker.stopped.connect(self._rx_worker.flush)
        # Audio -> UI.
        self._audio_worker.started.connect(self._on_rx_started)
        self._audio_worker.stopped.connect(self._on_rx_stopped)
        self._audio_worker.error.connect(self._on_rx_audio_error)
        self._audio_worker.stream_error.connect(self._on_audio_device_lost)

        # RX worker -> UI.
        self._rx_worker.image_started.connect(self._rx_panel.show_image_started)
        self._rx_worker.image_progress.connect(self._rx_panel.show_image_progress)
        self._rx_worker.rx_audio_ready.connect(self._on_rx_audio_ready)
        self._rx_worker.image_complete.connect(self._rx_panel.show_image_complete)
        self._rx_worker.image_complete.connect(self._on_rx_image_complete)
        self._rx_worker.status_update.connect(self._on_rx_status_update)
        self._rx_worker.error.connect(self._on_rx_error)
        self._rx_worker.error_occurred.connect(self._handle_worker_error)
        # v0.6 (Phase 2): mirror live RX to connected remote viewers. These
        # publish to the event hub (a no-op when no browser is connected),
        # so they add nothing to the decode path.  ``image_complete`` is
        # handled inside ``_on_rx_image_complete`` after auto-save.
        self._rx_worker.image_started.connect(self._remote_on_rx_started)
        self._rx_worker.image_progress.connect(self._remote_on_rx_progress)

        # --- Wire waterfall signals ---
        # Both workers emit audio chunks that the waterfall window consumes.
        # The window is created lazily on first show; we connect the signals
        # unconditionally so they can be routed when the window exists.
        self._rx_worker.waterfall_chunk.connect(self._on_rx_waterfall_chunk)
        self._tx_worker.tx_audio_chunk.connect(self._on_tx_waterfall_chunk)

        # Show waterfall on startup if it was open when the user last quit.
        if self._config.show_waterfall:
            QTimer.singleShot(0, lambda: self._on_toggle_waterfall(True))

        # --- Rig poll: lightweight 1 Hz timer on GUI thread dispatches to
        #     _RigPollWorker on its own thread so blocking serial/socket calls
        #     never stall the event loop. ---
        self._rig_poll_timer = QTimer(self)
        self._rig_poll_timer.setInterval(1000)

        self._rig_poll_thread = QThread(self)
        self._rig_poll_thread.setObjectName("sstv-app-rig-poll")
        self._rig_poll_worker = _RigPollWorker()
        self._rig_poll_worker.moveToThread(self._rig_poll_thread)
        self._rig_poll_thread.finished.connect(self._rig_poll_worker.deleteLater)
        self._rig_poll_thread.start()

        # Queued connection: timeout fires on GUI thread → slot runs on poll thread.
        self._rig_poll_timer.timeout.connect(self._rig_poll_worker.poll)
        self._rig_poll_worker.poll_result.connect(self._on_poll_result)
        self._rig_poll_worker.poll_error.connect(self._radio_panel.set_connection_error)
        self._rig_poll_worker.radio_disconnected.connect(self._on_radio_disconnected)
        self._rig_poll_worker.tune_failed.connect(self._on_tune_failed)
        # Band-plan tune relay: GUI thread emits _request_tune → rig-poll thread
        # executes tune().  Cross-thread → auto QueuedConnection.
        self._request_tune.connect(self._rig_poll_worker.tune)

        # --- Update checker on its own thread (one-shot HTTP GET at startup) ---
        self._update_thread = QThread(self)
        self._update_thread.setObjectName("sstv-app-update-checker")
        self._update_worker = UpdateCheckerWorker()
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.finished.connect(self._update_worker.deleteLater)
        self._update_thread.start()
        self._request_update_check.connect(self._update_worker.check)
        self._update_worker.update_available.connect(self._on_update_available)

        # --- Keyboard shortcuts ---
        # L (v0.3 audit): Ctrl+S/Cmd+S now lives on the File → Save
        # Received Image… menu action (see _build_menu_bar) so it's
        # discoverable; a duplicate QShortcut here would make the key
        # ambiguous and Qt would fire neither.

        # v0.1.33: the TX/RX panel-level defaults (sample-rate label,
        # default TX mode) were previously only applied through
        # ``_apply_config`` on Settings save.  Seeding them directly
        # here so the first launch already reflects the persisted
        # config without emitting queued cross-thread signals from
        # __init__ (which caused a teardown-race segfault in tests).
        self._tx_panel.set_sample_rate(self._config.sample_rate)
        self._tx_panel.set_default_mode(self._config.default_tx_mode)

        # M2 (v0.3 audit): a corrupt config used to reset every setting
        # (callsign, devices, rig setup) with only a log-file trace.
        # ``load_config`` now banishes the corpse to a ``.corrupt``
        # sibling; tell the user where it went so a hand-edit typo is
        # recoverable.  Deferred so the window paints first.
        corrupt_backup = last_corrupt_backup() if loaded_config_from_disk else None
        if corrupt_backup is not None:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self,
                    "Settings could not be read",
                    "Your settings file was corrupt and has been reset to "
                    f"defaults.\n\nThe old file was saved to:\n{corrupt_backup}"
                    "\n\nIf you recently hand-edited it, fix the syntax "
                    "error there and copy it back. Otherwise, re-enter "
                    "your callsign and devices in Settings.",
                ),
            )

        # OP-18: surface saved-but-missing audio devices so the user
        # knows their previously-selected device fell back to system
        # default rather than silently using the wrong one.
        if self._missing_devices:
            self.statusBar().showMessage(
                f"Saved audio device(s) not found: {', '.join(self._missing_devices)}"
                " — using system default. Open Settings → Audio to reselect.",
                10000,
            )
        else:
            self.statusBar().showMessage("Ready")

        # v0.2.7: fresh install → prompt for callsign.  Deferred via
        # ``QTimer.singleShot(0, …)`` so the main window paints first
        # and the dialog layers on top.  ``load_config`` grandfathers
        # pre-v0.2.7 users by injecting ``first_launch_seen = True``
        # whenever a TOML file exists without the key, so upgraders
        # never see this dialog.
        if not self._config.first_launch_seen:
            QTimer.singleShot(0, self._show_first_launch_dialog)
        elif self._config.check_for_updates:
            # Fresh installs trigger the check from _show_first_launch_dialog
            # after the user dismisses the welcome dialog.
            QTimer.singleShot(2000, self._trigger_update_check)

        # v0.6 (Phase 1): bring up the read-only remote gallery server if
        # the operator opted in via TOML.  Never blocks launch.
        self._apply_remote_server()

    # === Remote gallery server (Phase 1) ===

    def _apply_remote_server(self) -> None:
        """Start / stop the embedded remote server to match the live config.

        Called at startup and again after every settings apply so toggling
        ``remote_enabled`` (or changing host/port/token) takes effect
        without a relaunch.  A bind failure (port in use, permission) is
        logged and surfaced to the status bar but never blocks the app —
        the desktop side keeps working with or without the server.
        """
        cfg = self._config
        want = bool(cfg.remote_enabled)
        running = self._remote_server is not None

        # Stop first if disabled, or if a running server's binding params
        # changed (simplest correct behaviour: stop-and-recreate).  The
        # configured token "" means "mint a random one", which never
        # equals the server's resolved token — so an unchanged blank token
        # must not count as a change, or the server would restart on every
        # settings save and churn the URL.
        srv = self._remote_server
        token_changed = bool(cfg.remote_token) and srv is not None and srv.token != cfg.remote_token
        if running and srv is not None and (
            not want
            or srv.host != cfg.remote_host
            or srv.port != cfg.remote_port
            or token_changed
        ):
            self._stop_remote_server()
            running = False

        if not want or running:
            return

        try:
            self._remote_server = RemoteServer(
                self._remote_service,
                host=cfg.remote_host,
                port=cfg.remote_port,
                token=cfg.remote_token,
                hub=self._remote_hub,
                control=self._remote_control,
                compose=self._remote_compose,
            )
            self._remote_server.start()
        except OSError as exc:
            self._remote_server = None
            _log.warning("remote gallery server could not start: %s", exc)
            self.statusBar().showMessage(
                f"Remote gallery server failed to start on "
                f"{cfg.remote_host}:{cfg.remote_port} — {exc}",
                10000,
            )
            return
        # Log the bind only — the URL carries the token, and logs get
        # bundled into diagnostics exports.  The token-bearing URL still
        # goes to the (ephemeral, on-screen) status bar and indicator.
        _log.info("remote gallery server started on %s:%d", cfg.remote_host, cfg.remote_port)
        self.statusBar().showMessage(
            f"Remote gallery server running — open {self._remote_server.url}", 10000
        )
        self._show_remote_indicator(self._remote_server.url)

    def _show_remote_indicator(self, url: str) -> None:
        """Light the persistent status-bar indicator for a running server.

        The click-through link uses a loopback host so it always opens on
        this machine even when the server is bound to the LAN (0.0.0.0),
        while the tooltip shows the real bind URL for phones on the network.
        """
        local = url.replace("0.0.0.0", "127.0.0.1")
        self._remote_status_label.setText(
            f'<a href="{local}" style="color:#2fa36b; text-decoration:none">'
            "\N{LARGE GREEN CIRCLE} Remote on</a>"
        )
        self._remote_status_label.setToolTip(
            f"Remote gallery server is running.\n{url}\nClick to open in a browser."
        )
        self._remote_status_label.setVisible(True)

    def _stop_remote_server(self) -> None:
        """Stop the remote server if running (safe to call unconditionally)."""
        # Reclaim first: stopping the server also stops the tick thread that
        # drives the dead-man's-switch, so any in-flight remote TX must be
        # unkeyed and the lease dropped here rather than outliving its
        # watchdog across a stop/restart.
        self._remote_control.reclaim_local()
        # Always drop the indicator, even if there's no server object left
        # (e.g. a failed start already cleared it).
        self._remote_status_label.setVisible(False)
        if self._remote_server is None:
            return
        try:
            self._remote_server.stop()
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            _log.warning("remote gallery server stop failed: %s", exc)
        finally:
            self._remote_server = None

    # --- RX → remote live bridge (Phase 2) ---
    # These run on the GUI thread (queued signal delivery) and only take
    # short-lived locks / enqueue onto the event hub, so they never touch
    # or block the decode path.  The PNG encode is deferred to the request
    # thread in GalleryService.live_frame — never done here on the GUI.

    @Slot(object, int)
    def _remote_on_rx_started(self, mode: object, vis_code: int) -> None:
        self._remote_last_preview_t = 0.0  # force the first progress frame out
        self._remote_hub.publish({"type": "rx.started", "mode": str(mode)})

    @Slot(object, object, int, int, int)
    def _remote_on_rx_progress(
        self, image: object, mode: object, vis_code: int, lines: int, total: int
    ) -> None:
        # Throttle: the preview repaints a few times a second, not per line.
        now = time.monotonic()
        if now - self._remote_last_preview_t < 0.3:
            return
        self._remote_last_preview_t = now
        # Hand the service a private copy — the decoder keeps mutating the
        # original frame, and encoding happens later on the request thread.
        try:
            self._remote_service.set_live_image(image.copy())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — a bad frame must not break RX
            _log.debug("remote live-image copy failed: %s", exc)
        pct = int(lines / total * 100) if total else 0
        self._remote_hub.publish(
            {"type": "rx.progress", "mode": str(mode), "lines": lines,
             "total": total, "pct": pct}
        )

    # --- remote TX control bridge (Phase 3c) ---

    def _remote_tx_unkey(self, reason: str) -> None:
        """ControlPlane ``unkey`` callback — drop PTT and stop the rig.

        Ordering matters for safety.  We command PTT **off directly first**
        via ``emergency_unkey`` and only then ask the audio worker to stop.

        Why not rely on ``request_stop`` alone?  ``request_stop`` sets the
        stop flag and aborts PortAudio, then trusts the TX worker thread to
        unwind out of ``play_blocking`` and drop PTT in its ``finally``.  If
        that worker is wedged in a blocking ``stream.write()`` that the
        cross-thread ``abort()`` fails to interrupt (observed on a real USB
        CODEC), the ``finally`` never runs and the rig stays keyed until app
        close — the worst failure this app has.  The wedged worker holds no
        rig lock, so ``emergency_unkey`` can command the CAT backend
        directly, under its own lock, from this thread and unkey the
        transmitter within one tick regardless of the audio thread's state.

        Both calls are thread-safe and independent of the GUI event loop, so
        the dead-man's-switch fires even if the UI or the worker has stalled.
        ``request_stop`` still runs so the audio actually stops in the common
        (non-wedged) case; a redundant second ``set_ptt(False)`` from the
        worker's own unwind is harmless (idempotent).
        """
        _log.warning("remote TX unkey (%s) — dropping PTT + stopping TX", reason)
        self._tx_worker.emergency_unkey()  # PTT off NOW, independent of audio
        self._tx_worker.request_stop()     # then stop the audio (best-effort)

    @Slot(str, str)
    def _on_remote_tx_request(self, image_id: str, mode: str) -> None:
        """ControlPlane ``transmit`` landing on the GUI thread → key the rig.

        Re-checks the control-plane state here: if an abort or the dead-
        man's-switch moved it out of TRANSMITTING between confirm and now,
        we must NOT key.  Then resolve the id → gallery image and dispatch
        through the very same ``_request_transmit`` the local Send uses.
        """
        from PIL import Image  # noqa: PLC0415

        if self._remote_control.status().get("state") != "transmitting":
            return  # superseded by an abort / dead-man's-switch — don't key
        try:
            mode_enum = Mode(mode)
        except ValueError:
            _log.warning("remote TX: rejected unknown mode %r", mode)
            self._remote_tx_abandon()
            return
        if self._remote_compose.is_staged_id(image_id):
            # A browser-composed image, held in memory (no gallery/disk).
            img = self._remote_compose.staged_image(image_id)
            if img is None:
                _log.warning("remote TX: staged image %s expired", image_id)
                self._remote_tx_abandon()
                return
            what = "composed image"
        else:
            path = self._remote_service.image_path(image_id)
            if path is None:
                _log.warning("remote TX: image id %s no longer resolvable", image_id)
                self._remote_tx_abandon()
                return
            try:
                img = Image.open(path)
                img.load()
            except Exception as exc:  # noqa: BLE001 — bad file must not key the rig
                _log.warning("remote TX: cannot load %s: %s", path, exc)
                self._remote_tx_abandon()
                return
            what = path.name
        # RE-CHECK the state a SECOND time, immediately before keying — the
        # image load above is the widest window in which an abort / reclaim
        # could have landed (workers.py clears its stop flag at TX entry, so
        # a request_stop that fired during the load would otherwise be lost).
        # Checking here shrinks that window to the microseconds between this
        # line and the emit.  (The residual — an abort in that microsecond
        # gap before the worker keys PTT — is bounded to a single image and
        # requires machine-precision timing; the dead-man's-switch cannot
        # hit it because confirm() just refreshed the heartbeat clock.)
        if self._remote_control.status().get("state") != "transmitting":
            return
        _log.info("remote TX: transmitting %s in %s", what, mode)
        self._last_tx_was_test_tone = False
        self._request_transmit.emit(img, mode_enum)

    def _remote_tx_abandon(self) -> None:
        """A remote transmit couldn't proceed (bad image/mode) — return the
        control plane to idle and tell viewers, without keying anything."""
        self._remote_control.on_tx_finished()
        self._remote_hub.publish({"type": "tx.state", **self._remote_control.status()})

    @Slot()
    def _remote_tx_ended(self) -> None:
        """Any transmission ended → return the control plane to idle.

        Idempotent: a no-op unless a remote TX was in flight.  Normal
        completion does not unkey (the worker already stopped).
        """
        self._remote_control.on_tx_finished()

    # === Menu bar ===

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")

        settings_action = QAction("&Settings…", self)
        # NoRole prevents macOS from moving this into the app menu and
        # leaving File empty (which hides the entire menu).
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)
        # Keep a reference so TX start/stop can enable/disable it.
        self._settings_action = settings_action

        file_menu.addSeparator()

        # L (v0.3 audit): the Ctrl+S/Cmd+S save-RX-image shortcut existed
        # since v0.1 but appeared nowhere in the UI — a menu item is the
        # discovery path (and shows the platform shortcut next to it).
        save_image_action = QAction("&Save Received Image…", self)
        save_image_action.setMenuRole(QAction.MenuRole.NoRole)
        save_image_action.setShortcut(QKeySequence.StandardKey.Save)
        save_image_action.triggered.connect(self._on_save_shortcut)
        file_menu.addAction(save_image_action)

        file_menu.addSeparator()

        # Offline encode / decode were briefly menu items in v0.3.9 but
        # moved to in-panel buttons in v0.3.10 ("Export to Audio" on the
        # TX panel, "Decode Audio" on the RX panel) — single discovery
        # path, and the encode side picks up the live TX-panel composite
        # (template + photo + QSO state) so the WAV matches what would
        # have been transmitted.

        quit_action = QAction("&Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # v0.4: detached logbook window, per established ham-app
        # convention (MMSSTV / fldigi / WSJT-X all use one).
        tools_menu = mb.addMenu("&Tools")
        logbook_action = QAction("&Logbook…", self)
        # NoRole for the same macOS reason as Settings above.
        logbook_action.setMenuRole(QAction.MenuRole.NoRole)
        logbook_action.setShortcut(QKeySequence("Ctrl+L"))  # Cmd+L on macOS
        logbook_action.triggered.connect(self._open_logbook)
        tools_menu.addAction(logbook_action)
        self._logbook_action = logbook_action

        # v0.5: detached image gallery, paired with the logbook window.
        gallery_action = QAction("&Gallery…", self)
        gallery_action.setMenuRole(QAction.MenuRole.NoRole)
        gallery_action.setShortcut(QKeySequence("Ctrl+G"))  # Cmd+G on macOS
        gallery_action.triggered.connect(self._open_gallery)
        tools_menu.addAction(gallery_action)
        self._gallery_action = gallery_action

        view_menu = mb.addMenu("&View")
        waterfall_action = QAction("&Waterfall", self)
        waterfall_action.setCheckable(True)
        waterfall_action.setChecked(self._config.show_waterfall)
        waterfall_action.triggered.connect(self._on_toggle_waterfall)
        view_menu.addAction(waterfall_action)
        self._waterfall_action = waterfall_action

        help_menu = mb.addMenu("&Help")
        about_action = QAction("&About Open-SSTV", self)
        about_action.setMenuRole(QAction.MenuRole.NoRole)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    @Slot()
    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Open-SSTV",
            f"<h3>Open-SSTV v{__version__}</h3>"
            "<p>Open-source SSTV transceiver for amateur radio.</p>"
            "<p>22 modes: Robot 36, Martin M1/M2/M3/M4, Scottie S1/S2/S3/S4/DX, "
            "PD-50/90/120/160/180/240/290, Wraase SC2-120/SC2-180, Pasokon P3/P5/P7.</p>"
            "<p>Created by Kevin &mdash; W0AEZ</p>"
            '<p><a href="https://bucknova.github.io/Open-SSTV/">'
            "bucknova.github.io/Open-SSTV</a> &middot; "
            '<a href="https://github.com/bucknova/Open-SSTV">'
            "github.com/bucknova/Open-SSTV</a></p>"
            "<p>GPL-3.0-or-later</p>",
        )

    # === Logbook (v0.4) ===

    @Slot()
    def _open_logbook(self) -> None:
        """Tools → Logbook… (Cmd/Ctrl+L): show the detached logbook window."""
        try:
            _ = self._logbook_coordinator.store  # force lazy open
        except Exception as exc:  # noqa: BLE001 — SchemaTooNew, locked file, …
            QMessageBox.warning(
                self,
                "Logbook unavailable",
                f"Could not open the logbook database:\n\n{exc}",
            )
            return
        if self._logbook_dialog is None:
            self._logbook_dialog = LogbookDialog(self._logbook_coordinator, parent=self)
            # v0.5: Logbook "Show in Gallery" → focus the gallery.
            self._logbook_dialog.show_in_gallery_requested.connect(
                self._on_show_in_gallery
            )
        else:
            self._logbook_dialog.refresh()
        self._logbook_dialog.show()
        self._logbook_dialog.raise_()
        self._logbook_dialog.activateWindow()

    @Slot()
    def _on_udp_log_requested(self) -> None:
        """[External Log] button: broadcast the current QSO bar over UDP.

        UDP-only — never touches the local logbook database (that's
        ``save_draft``'s per-image auto-capture flow, a separate
        concern).  Time is the moment of the click (UTC); mode is fixed
        to "SSTV" since this bar only ever describes an SSTV contact.
        """
        qso_state = self._tx_panel.get_qso_state()
        callsign = qso_state.tocall.strip().upper()
        if not callsign:
            self.statusBar().showMessage(
                "UDP log: enter a ToCall on the QSO bar first", 5000
            )
            return
        rst_received, qth, grid = self._tx_panel.get_udp_log_fields()
        qso = QSO(
            direction="TX",
            callsign=callsign,
            time_utc=datetime.datetime.now(datetime.UTC),
            mode="SSTV",
            frequency_hz=self._last_rig_freq_hz,
            rsv_sent=qso_state.rst,
            rsv_received=rst_received,
            name=qso_state.tocall_name,
            qth=qth,
            grid=grid,
            comment=qso_state.note,
        )
        logger = UdpQsoLogger(
            self._config.udp_log_host,
            self._config.udp_log_port,
            format=self._config.udp_log_format,
        )
        try:
            logger.log_qso(qso, self._logbook_coordinator.station_info())
        except QsoLoggingError as exc:
            self.statusBar().showMessage(f"UDP log failed: {exc}", 8000)
            return
        # UDP is fire-and-forget: a successful sendto() only means the
        # datagram left this machine, not that anything received it
        # (no ack, and an unconnected socket never sees ICMP
        # port-unreachable). "Sent" alone would read as delivery
        # confirmation the operator doesn't actually have.
        self.statusBar().showMessage(
            f"UDP log sent for {callsign} (not confirmed).", 5000
        )

    def _refresh_logbook_if_open(self) -> None:
        if self._logbook_dialog is not None and self._logbook_dialog.isVisible():
            self._logbook_dialog.refresh()

    @Slot()
    def _open_gallery(self) -> None:
        """Tools → Gallery… (Cmd/Ctrl+G): show the detached image gallery."""
        if self._gallery_dialog is None:
            self._gallery_dialog = GalleryDialog(
                self._logbook_coordinator,
                config_getter=lambda: self._config,
                parent=self,
            )
            # v0.5 cross-links: Gallery → Logbook row, and Gallery
            # "Re-send to TX" → load the image into the TX panel.
            self._gallery_dialog.open_qso_requested.connect(self._on_gallery_open_qso)
            self._gallery_dialog.resend_requested.connect(self._on_gallery_resend)
        else:
            self._gallery_dialog.refresh()
        self._gallery_dialog.show()
        self._gallery_dialog.raise_()
        self._gallery_dialog.activateWindow()

    @Slot(object)
    def _on_gallery_open_qso(self, qso: QSO) -> None:
        """Gallery → QSO: open the Logbook focused on this contact's row."""
        self._open_logbook()
        if self._logbook_dialog is not None and qso.id is not None:
            self._logbook_dialog.select_qso(qso.id)

    @Slot(object)
    def _on_gallery_resend(self, path: object) -> None:
        """Gallery "Re-send to TX": load the image into the TX panel."""
        from pathlib import Path  # noqa: PLC0415

        self._tx_panel.load_image(Path(path))  # type: ignore[arg-type]
        self.raise_()
        self.activateWindow()

    @Slot(object)
    def _on_show_in_gallery(self, path: object) -> None:
        """Logbook "Show in Gallery": open the Gallery focused on this image."""
        from pathlib import Path  # noqa: PLC0415

        self._open_gallery()
        if self._gallery_dialog is not None:
            self._gallery_dialog.focus_on_path(Path(path))  # type: ignore[arg-type]

    def _capture_qso(
        self,
        draft: QSO,
        preview_image: PILImage | None,
        mode: object,
        *,
        draft_when_busy: bool = True,
    ) -> None:
        """Dispatch a completion draft: modal dialog, or silent insert.

        ``auto_log_qsos`` writes everything silently.  When a capture
        dialog is already open, ``draft_when_busy`` decides the
        fallback: True (you're engaged — a partner's back-to-back
        image) → silent draft so nothing of *your* QSO is lost; False
        (third-party traffic on a monitored frequency) → drop with a
        status hint, because strangers' exchanges don't belong in the
        logbook — the image stays in the RX gallery for deliberate
        logging.  Logbook failures are status-bar noise, never
        dialogs: a broken logbook must not interrupt operating.
        """
        if self._closing:
            return  # audit #3: never capture during window teardown
        dialog_busy = (
            self._capture_context is not None
            and self._capture_context[0].isVisible()
        )
        if self._logbook_coordinator.auto_log or (dialog_busy and draft_when_busy):
            # Audit #2: the silent paths must persist the picture too.
            # Without this, an auto-logged or busy-drafted QSO keeps a
            # path-less row and the image is lost — TX images
            # immediately (they never enter the gallery), RX images at
            # gallery eviction or app exit.
            if draft.image_path is None and preview_image is not None:
                draft.image_path = self._autosave_image(
                    preview_image, mode, draft.direction, status_verb="Saved"
                )
            try:
                saved = self._logbook_coordinator.save_draft(draft)
            except Exception as exc:  # noqa: BLE001
                _log.warning("logbook draft save failed: %s", exc)
                self.statusBar().showMessage(f"Logbook write failed: {exc}", 8000)
                return
            who = saved.callsign or "draft"
            self.statusBar().showMessage(
                f"Logged {who} QSO — edit in Tools → Logbook (Ctrl+L)", 5000
            )
            self._refresh_logbook_if_open()
            return
        if dialog_busy:
            self.statusBar().showMessage(
                "Log dialog already open — image kept in the gallery "
                "(right-click it to log)",
                5000,
            )
            return
        self._open_capture_dialog(draft, preview_image, mode)

    def _open_capture_dialog(
        self, draft: QSO, preview_image: PILImage | None, mode: object
    ) -> None:
        """Open the window-modal LogQsoDialog for *draft*."""
        if (
            self._capture_context is not None
            and self._capture_context[0].isVisible()
        ):
            # The gallery path can't normally reach here (window
            # modality blocks the gallery while a capture dialog is
            # up), but guard against racing completions anyway.
            self.statusBar().showMessage("Finish the open log dialog first", 5000)
            return
        dlg = LogQsoDialog(draft, preview_image=preview_image, parent=self)
        self._capture_context = (dlg, preview_image, mode)
        dlg.finished.connect(self._on_capture_dialog_finished)
        # Window-modal + non-blocking: the RX-resume timer, rig unlock,
        # and decoder all keep running behind the dialog.
        dlg.open()

    @Slot(int)
    def _on_capture_dialog_finished(self, result: int) -> None:
        """Persist (or discard) the capture dialog's QSO.

        Esc / Cancel writes nothing — that's the contract that makes a
        noise-triggered RX completion cost one keypress.  On save, an
        image that was never auto-saved is written to the images dir
        now: a logbook row should keep its picture even when auto-save
        is off, and the row stores the path per the v0.4 plan.
        """
        ctx = self._capture_context
        self._capture_context = None
        if ctx is None:
            return
        dlg, preview_image, mode = ctx
        dlg.deleteLater()
        if result != int(LogQsoDialog.DialogCode.Accepted):
            return
        qso = dlg.result_qso()
        if qso.image_path is None and preview_image is not None:
            qso.image_path = self._autosave_image(
                preview_image, mode, qso.direction, status_verb="Saved"
            )
        try:
            saved = self._logbook_coordinator.save_draft(qso)
        except Exception as exc:  # noqa: BLE001
            _log.warning("logbook save failed: %s", exc)
            QMessageBox.warning(
                self,
                "Logbook save failed",
                f"The QSO could not be written to the logbook:\n\n{exc}",
            )
            return
        who = saved.callsign or "draft"
        self.statusBar().showMessage(f"Logged {who} ({saved.mode})", 5000)
        self._refresh_logbook_if_open()

    @Slot(object, object)
    def _on_gallery_log_qso(self, image: object, mode: object) -> None:
        """RX-gallery *Log QSO…*: deliberately log a kept decode.

        Bypasses both the capture gating (an explicit click always
        means "log this one") and the auto-log silent path (the user
        asked for the form, not a background draft).  The frequency
        snapshot is the *current* poll cache — right while you're
        still on frequency, stale if you QSYed since the decode; same
        for the timestamp, which is log-time rather than decode-time
        because gallery thumbnails don't carry their decode clock.
        Close enough for the log-right-after-the-QSO workflow this
        exists for.
        """
        pil_image: PILImage = image  # type: ignore[assignment]
        draft = self._logbook_coordinator.build_rx_draft(
            mode=mode,
            frequency_hz=self._last_rig_freq_hz,
        )
        self._open_capture_dialog(draft, pil_image, mode)

    def _trigger_update_check(self) -> None:
        """Dispatch the one-shot update check to the worker thread."""
        self._request_update_check.emit()

    @Slot(str, str)
    def _on_update_available(self, version: str, url: str) -> None:
        """Show a persistent status-bar link when a newer release is found."""
        from PySide6.QtWidgets import QLabel

        lbl = QLabel(f'<a href="{url}">Open-SSTV {version} available</a>')
        lbl.setOpenExternalLinks(True)
        lbl.setContentsMargins(0, 0, 8, 0)
        self.statusBar().addPermanentWidget(lbl)

    @Slot()
    def _show_first_launch_dialog(self) -> None:
        """Prompt the operator for their callsign on a truly fresh install.

        Scheduled via ``QTimer.singleShot(0, …)`` from ``__init__`` so
        this fires on the next event-loop tick — the main window has
        already painted by that point and the modal layers on top
        instead of preceding the UI.

        Either button dismisses the dialog permanently:

        * *Save* with a non-empty callsign → persist it, push to the
          radio and TX panels, mark ``first_launch_seen``.
        * *Save* with an empty field, or *Skip for now* → leave the
          existing callsign untouched (which on a fresh install is
          also empty), mark ``first_launch_seen`` anyway so we don't
          re-prompt on the next launch.

        If ``save_config`` fails (read-only config dir, etc.) the
        status bar carries the failure — we don't block startup and
        we don't re-raise, the user just gets the dialog again next
        launch.
        """
        dlg = FirstLaunchDialog(self)
        accepted = dlg.exec() == FirstLaunchDialog.DialogCode.Accepted
        typed = dlg.callsign() if accepted else ""
        # v0.3.4: optional operator-info fields. Only persist on Accept;
        # on Skip we leave the existing values (empty for fresh install)
        # untouched so the user isn't surprised by partial state.
        typed_name = dlg.operator_name() if accepted else ""
        typed_grid = dlg.grid_square() if accepted else ""
        typed_qth = dlg.qth() if accepted else ""

        self._config.first_launch_seen = True
        # M5: only honour the update-checker checkbox when the user
        # clicked Save.  On Skip we leave every preference untouched —
        # previously the typed callsign was discarded (correct intent
        # "dismiss without saving") but ``check_updates_enabled`` was
        # read unconditionally, so a user who typed their callsign,
        # unchecked the updates box, and clicked Skip lost their
        # callsign AND saved the unchecked preference.  Asymmetric.
        if accepted:
            self._config.check_for_updates = dlg.check_updates_enabled()
            if typed:
                self._config.callsign = typed
            if typed_name:
                self._config.operator_name = typed_name
            if typed_grid:
                self._config.grid_square = typed_grid
            if typed_qth:
                self._config.qth = typed_qth

        try:
            save_config(self._config)
        except OSError as exc:
            self.statusBar().showMessage(
                f"Welcome settings could not be saved to disk: {exc}", 8000
            )
            return

        if self._config.check_for_updates:
            QTimer.singleShot(1000, self._trigger_update_check)

        if typed:
            # Push the newly-set callsign into the panels that embed
            # it (TX banner, CW ID, image editor overlay default).
            self._radio_panel.set_callsign(typed)
            self._tx_panel.set_callsign(typed)
            self.statusBar().showMessage(f"Callsign set to {typed}.", 5000)

    @Slot()
    def _open_settings(self) -> None:
        # Capture current gain so Cancel can revert any live-pushed value.
        _original_output_gain = self._config.audio_output_gain
        dlg = SettingsDialog(
            self._config,
            rig_connected=self._radio_panel.connected,
            tx_image=self._tx_panel.current_image,
            parent=self,
        )
        # Route Test Tone from the dialog through the same path as the Radio
        # panel button (queued signal → TxWorker on its own thread).
        dlg.test_tone_requested.connect(self._on_test_tone_requested)
        # Keep the dialog's button in sync with live TX state while it's open.
        self._tx_worker.transmission_started.connect(dlg.on_tx_started)
        self._tx_worker.transmission_complete.connect(dlg.on_tx_ended)
        self._tx_worker.transmission_aborted.connect(dlg.on_tx_ended)
        self._tx_worker.error.connect(dlg.on_tx_error)
        # Store lambda references so the finally block can disconnect them
        # explicitly — you can't disconnect an anonymous lambda by identity.
        _gain_lambda = lambda gain: self._tx_worker.set_output_gain(gain)  # noqa: E731
        _revert_lambda = lambda: self._tx_worker.set_output_gain(_original_output_gain)  # noqa: E731
        # Live-push TX gain on each slider tick (no disk write).
        dlg.output_gain_changed.connect(_gain_lambda)
        # Revert the live gain if the user cancels.
        dlg.rejected.connect(_revert_lambda)
        try:
            result = dlg.exec()
        finally:
            # Disconnect ALL wired signals immediately.  The dialog is about
            # to go out of scope; if tx_worker → dlg connections linger,
            # PySide6's C++ side holds a stale reference and the
            # QDialogWrapper destructor segfaults during Python finalization
            # (atexit → destroyQCoreApplication → PySide::destructionVisitor
            # on an already-freed Python wrapper).  The try/finally guarantees
            # the disconnects fire even if exec() raises.
            # tx_worker → dlg (emitter outlives dialog)
            self._tx_worker.transmission_started.disconnect(dlg.on_tx_started)
            self._tx_worker.transmission_complete.disconnect(dlg.on_tx_ended)
            self._tx_worker.transmission_aborted.disconnect(dlg.on_tx_ended)
            self._tx_worker.error.disconnect(dlg.on_tx_error)
            # dlg → self (emitter dies with dialog, but disconnect for symmetry)
            dlg.test_tone_requested.disconnect(self._on_test_tone_requested)
            dlg.output_gain_changed.disconnect(_gain_lambda)
            dlg.rejected.disconnect(_revert_lambda)

        if result == SettingsDialog.DialogCode.Accepted:
            old_input_device = self._config.audio_input_device
            old_sample_rate = self._config.sample_rate

            self._config = dlg.result_config()
            # Adopt any rigctld process the dialog launched before trying to
            # persist, so a save failure doesn't orphan the process.
            #
            # H2: previously this assignment overwrote ``self._rigctld_proc``
            # unconditionally — if the main window already owned a rigctld
            # (auto-launched on Connect), the old process was leaked: still
            # holding the serial port and TCP socket until the OS reaped it.
            # Kill the existing one first so adoption replaces rather than
            # orphans.
            if dlg.rigctld_process is not None:
                if self._rigctld_proc is not None:
                    self._kill_rigctld()
                self._rigctld_proc = dlg.rigctld_process
            # Always apply to in-memory state so the session works even if
            # the disk write fails.
            self._apply_config()
            try:
                save_config(self._config)
            except OSError as exc:
                # M6 (v0.3 audit): a save failure used to be a transient
                # status-bar message that was easy to miss — the settings
                # applied in memory, then silently reverted on the next
                # launch, which reads as "the app forgot my settings".
                # A modal makes the disk problem impossible to overlook.
                # OP2-18: if rigctld was just adopted, say it's running
                # despite the save failure.
                rigctld_note = (
                    "\n\nrigctld is running — disconnect the rig to stop it."
                    if self._rigctld_proc is not None
                    else ""
                )
                QMessageBox.warning(
                    self,
                    "Settings not saved",
                    "Your changes are active for this session but could "
                    f"not be written to disk:\n\n{exc}\n\nThey will be "
                    "lost when the app closes. Check the config "
                    f"directory's permissions and free space.{rigctld_note}",
                )
                return

            # If audio input device or sample rate changed while capture is
            # active, the running stream is still using the old settings.
            # Notify the user — we don't auto-restart because that would
            # discard any partially decoded in-flight image.
            audio_restart_needed = (
                self._config.audio_input_device != old_input_device
                or self._config.sample_rate != old_sample_rate
            )
            if audio_restart_needed and self._capture_running:
                self.statusBar().showMessage(
                    "Audio settings changed — restart capture to apply.", 5000
                )
            else:
                self.statusBar().showMessage("Settings saved.", 3000)

    # === Offline encode / decode (in-panel buttons) ===
    #
    # Both workers live on their own one-shot QThread and are stored as
    # instance attributes (``self._offline_{encode,decode}_{thread,worker}``)
    # rather than locals.  This matters: PySide6 signal connections hold
    # only a weak ref to the receiver QObject, so a local-variable worker
    # would be garbage-collected the moment the slot returns — before
    # ``QMetaObject.invokeMethod`` could dispatch the queued call.  Symptom
    # in v0.3.9 was a silent failure: status bar showed "Encoding…" then
    # nothing.  Cleanup happens in ``_on_offline_{encode,decode}_thread_finished``.
    # Re-entrant clicks while a worker is in flight are dropped to avoid
    # interleaving two encodes / decodes on top of each other.

    @Slot()
    def _on_decode_audio_file_requested(self) -> None:
        """RX panel → Decode Audio: pick a WAV/FLAC, decode off-thread.

        On success the resulting image lands in the gallery via the same
        ``_on_rx_image_complete`` path used for live RX, so the user
        sees it appear in the thumbnail strip and can save / drag-out
        normally.  On failure the status bar carries the explanation.
        """
        if self._offline_decode_thread is not None:
            # Previous decode still in flight — ignore re-entrant click.
            self.statusBar().showMessage(
                "Decode already in progress — wait for it to finish.", 3000
            )
            return

        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Decode SSTV audio file",
            self._config.images_save_dir or "",
            "Audio (*.wav *.flac);;All files (*)",
        )
        if not path:
            return

        self.statusBar().showMessage(f"Decoding {Path(path).name}…")

        # Audit #4: remember the source file so the logbook draft can be
        # stamped with the recording's own clock instead of "now".
        self._offline_decode_source = Path(path)

        thread = QThread(self)
        thread.setObjectName("offline-decode-thread")
        # Constructor-args pattern: path lives on the worker before
        # thread.start, so we don't need QMetaObject.invokeMethod /
        # Q_ARG to ship it across the thread boundary.
        worker = OfflineDecodeWorker(path)
        # Strong refs survive past the end of this slot so the worker
        # isn't GC'd before thread.started → worker.run fires.
        self._offline_decode_thread = thread
        self._offline_decode_worker = worker

        worker.image_complete.connect(self._rx_panel.show_image_complete)
        # Audit #4: offline decodes route through a wrapper that strips
        # the live-rig context — the rig's current dial frequency says
        # nothing about a WAV recorded last month.
        worker.image_complete.connect(self._on_offline_image_complete)
        worker.image_complete.connect(self._on_offline_decode_complete)
        worker.error.connect(self._on_offline_decode_error)
        # One-shot cleanup: worker.finished → thread.quit → instance-attr
        # release in _on_offline_decode_thread_finished + deleteLater.
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_offline_decode_thread_finished)

        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.start()

    @Slot()
    def _on_offline_decode_thread_finished(self) -> None:
        """Release strong refs to the offline-decode worker + thread."""
        self._offline_decode_thread = None
        self._offline_decode_worker = None

    @Slot(object, object, int)
    def _on_offline_decode_complete(
        self, image: object, mode: object, vis_code: int  # noqa: ARG002
    ) -> None:
        """Status-bar confirmation for the offline-decode path.

        ``image_complete`` is already wired to the gallery via the live-RX
        slots; this hook only adds the user-visible status line so the
        operation feels distinct from a passive live decode arriving in
        the background.
        """
        mode_name = getattr(mode, "value", str(mode))
        self.statusBar().showMessage(
            f"Decoded {mode_name} from file.", 5000
        )

    @Slot(str)
    def _on_offline_decode_error(self, message: str) -> None:
        """Surface decode failures via the status bar (5 s timeout)."""
        self.statusBar().showMessage(f"Decode failed: {message}", 5000)

    @Slot(object, object)
    def _on_export_to_audio_requested(self, image: PILImage, mode: Mode) -> None:
        """TX panel → Export to Audio: ask for a path, encode off-thread.

        The TX panel hands us the same composited image its Transmit
        button would emit (template + photo + QSO overlays already
        applied), so the resulting WAV contains exactly what would have
        gone over the air — including the legacy TX banner strip when
        the user has it enabled in Settings.  Banner gating mirrors
        ``TxWorker.transmit`` (the live-TX path): banner stamps whenever
        ``tx_banner_enabled`` is True.  v0.3.13 removed the previous
        "skip banner when v0.3 template active" gate — per user
        feedback, banner-on means banner-always-on regardless of
        template.
        """
        if self._offline_encode_thread is not None:
            # Previous encode still in flight — ignore re-entrant click.
            self.statusBar().showMessage(
                "Encode already in progress — wait for it to finish.", 3000
            )
            return

        # Banner stamp — same gating as TxWorker.transmit.  The live-TX path
        # runs this inside TxWorker after the panel has emitted the image;
        # the export-to-audio path bypasses TxWorker, so we have to apply
        # it here or the WAV would lack the banner that live TX includes.
        # v0.3.10 shipped without this; v0.3.12 added the gated form;
        # v0.3.13 removed the template-active gate.
        if self._config.tx_banner_enabled:
            try:
                from open_sstv.core.banner import (
                    apply_tx_banner,
                    scaled_banner_params,
                )
                _bh, _fs = scaled_banner_params(
                    self._config.tx_banner_size, image.width
                )
                image = apply_tx_banner(
                    image,
                    __version__,
                    self._config.callsign,
                    self._config.tx_banner_bg_color,
                    self._config.tx_banner_text_color,
                    banner_height=_bh,
                    font_size=_fs,
                )
            except Exception as exc:  # noqa: BLE001
                # Same recovery as TxWorker: surface failure to the user
                # and bail out rather than silently encoding without a banner.
                self.statusBar().showMessage(
                    f"TX banner failed: {exc}", 5000
                )
                QMessageBox.warning(
                    self, "Export failed", f"TX banner failed: {exc}"
                )
                return

        # Suggest a sensible default filename based on callsign + mode +
        # timestamp; lands in the configured images-save dir by default.
        save_dir = self._config.images_save_dir or str(Path.home())
        callsign = self._config.callsign or "open-sstv"
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_slug = mode.value.replace(" ", "_")
        suggested = str(Path(save_dir) / f"{callsign}_{mode_slug}_{timestamp}.wav")

        output_path, _filter = QFileDialog.getSaveFileName(
            self,
            "Save encoded audio as",
            suggested,
            "WAV audio (*.wav);;All files (*)",
        )
        if not output_path:
            return
        # Ensure .wav suffix even if the user typed something else; the
        # wave module silently writes whatever path we give it.
        if not output_path.lower().endswith(".wav"):
            output_path = output_path + ".wav"

        self.statusBar().showMessage(
            f"Encoding as {mode.value}…"
        )

        thread = QThread(self)
        thread.setObjectName("offline-encode-thread")
        # Constructor-args pattern: image + mode + path live on the
        # worker before thread.start, so we don't need
        # QMetaObject.invokeMethod / Q_ARG to ship them across — and
        # in fact PySide6 6.11's Q_ARG cannot marshal PIL.Image or
        # the Mode StrEnum across a queued invocation.
        worker = OfflineEncodeWorker(
            image, mode, self._config.sample_rate, output_path
        )
        # Strong refs survive past the end of this slot so the worker
        # isn't GC'd before thread.started → worker.run fires.
        self._offline_encode_thread = thread
        self._offline_encode_worker = worker

        worker.encode_complete.connect(self._on_offline_encode_complete)
        worker.error.connect(self._on_offline_encode_error)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_offline_encode_thread_finished)

        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        thread.start()

    @Slot()
    def _on_offline_encode_thread_finished(self) -> None:
        """Release strong refs to the offline-encode worker + thread."""
        self._offline_encode_thread = None
        self._offline_encode_worker = None

    @Slot(str, float, object)
    def _on_offline_encode_complete(
        self, output_path: str, duration_s: float, mode: object
    ) -> None:
        """Status-bar confirmation for the offline-encode path."""
        mode_name = getattr(mode, "value", str(mode))
        self.statusBar().showMessage(
            f"Wrote {Path(output_path).name} — {mode_name}, {duration_s:.1f} s",
            5000,
        )

    @Slot(str)
    def _on_offline_encode_error(self, message: str) -> None:
        """Surface encode failures via a QMessageBox (more visible than status bar
        because encode failures usually mean the user picked an unloadable
        image or hit a permission error on the output path)."""
        self.statusBar().showMessage(f"Encode failed: {message}", 5000)
        QMessageBox.warning(self, "Encode failed", message)

    def _apply_config(self) -> None:
        """Push the current ``_config`` into all live workers and UI elements.

        Called both on a successful settings save and on a disk-write failure
        so the session always reflects the user's latest choices.
        """
        self._radio_panel.set_callsign(self._config.callsign)
        self._tx_panel.set_callsign(self._config.callsign)
        self._tx_panel.set_default_mode(self._config.default_tx_mode)
        # v0.3: push full config so template gallery can re-render thumbnails
        # with updated callsign / grid / op-name token values.
        self._tx_panel.set_app_config(self._config)
        new_output = find_output_device_by_name(
            self._config.audio_output_device
        ) or find_pipewire_sink_by_name(self._config.audio_output_device)
        self._tx_worker.set_output_device(new_output)
        self._tx_worker.set_output_gain(self._config.audio_output_gain)
        self._tx_worker.set_ptt_delay(self._config.ptt_delay_s)
        new_input = find_input_device_by_name(self._config.audio_input_device)
        self._input_device = new_input
        self._rx_worker.set_input_gain(self._config.audio_input_gain)
        # Emit via queued signals so decoder rebuilds happen on the worker
        # thread, not the GUI thread (H-02 fix; OP-09 extended to cover
        # set_final_slant_correction too).
        self._rx_weak_signal_changed.emit(self._config.rx_weak_signal_mode)
        self._rx_watchdog_timeout_changed.emit(self._config.rx_watchdog_timeout_s)
        self._rx_final_slant_correction_changed.emit(
            self._config.apply_final_slant_correction
        )
        self._rx_incremental_decode_changed.emit(self._config.incremental_decode)
        self._tx_worker.set_tx_banner(
            self._config.tx_banner_enabled,
            self._config.callsign,
            self._config.tx_banner_bg_color,
            self._config.tx_banner_text_color,
            self._config.tx_banner_size,
        )
        self._tx_worker.set_cw_id(
            self._config.cw_id_enabled,
            self._config.callsign,
            self._config.cw_id_wpm,
            self._config.cw_id_tone_hz,
        )
        # L-2: refresh test-tone frequencies from the just-saved config.
        self._tx_worker.set_test_tone_freqs(
            self._config.test_tone_freq_lo,
            self._config.test_tone_freq_hi,
        )
        # Propagate sample rate to both workers (takes effect on the next
        # encode/capture start).  Both go via queued signals so the
        # change lands on the receiving worker's own event loop (OP-09).
        self._tx_sample_rate_changed.emit(self._config.sample_rate)
        self._rx_sample_rate_changed.emit(self._config.sample_rate)
        # TX panel needs the rate too so the progress-bar elapsed/total
        # seconds label is correct at 44.1 kHz (OP-06).
        self._tx_panel.set_sample_rate(self._config.sample_rate)
        # v0.6 (Phase 3c): if remote transmit was just turned off, reclaim
        # control now (unkeying any in-flight remote TX) instead of waiting
        # for the control plane's continuous gate check to catch it.
        if not self._config.remote_tx_enabled:
            self._remote_control.reclaim_local()
        # v0.6 (Phase 1): start/stop/restart the remote server to match
        # the freshly-saved config (the service reads the live config, so
        # host/port/token changes are picked up on restart).
        self._apply_remote_server()

    # === TX slots ===

    @Slot(object, object)
    def _on_transmit_requested(self, image: PILImage, mode: Mode) -> None:
        """Set the test-tone flag and dispatch via queued signal to TX thread."""
        # The operator at the radio always wins: taking the local Send
        # reclaims control from any remote holder (and unkeys a remote TX).
        self._remote_control.reclaim_local()
        self._last_tx_was_test_tone = False
        self._request_transmit.emit(image, mode)

    @Slot()
    def _on_test_tone_requested(self) -> None:
        """Set the test-tone flag and dispatch via queued signal to TX thread."""
        self._remote_control.reclaim_local()  # local keying wins over remote
        self._last_tx_was_test_tone = True
        self._request_test_tone.emit()

    @Slot()
    def _on_stop_requested(self) -> None:
        self._tx_worker.request_stop()

    @Slot(int, int)
    def _on_tx_progress(self, samples_played: int, samples_total: int) -> None:
        """Status-bar countdown for the test tone, and TX progress to remote
        viewers so the browser can show an elapsed/remaining bar."""
        # Feed the browser's ON AIR progress bar, but only for a remote-driven
        # transmission (the control plane is TRANSMITTING only then — a local
        # Send leaves it IDLE, so this stays quiet for local TX).
        if samples_total > 0 and self._remote_control.status().get("state") == "transmitting":
            rate = self._config.sample_rate or 48000
            self._remote_hub.publish({
                "type": "tx.progress",
                "elapsed_s": round(samples_played / rate, 2),
                "total_s": round(samples_total / rate, 2),
            })
        if not self._last_tx_was_test_tone or samples_total <= 0:
            return
        remaining_s = max(
            0, int((samples_total - samples_played) / self._config.sample_rate)
        )
        self.statusBar().showMessage(
            f"Transmitting test tone… {remaining_s}s remaining"
        )

    @Slot()
    def _on_tx_started(self) -> None:
        self._tx_panel.set_transmitting(True)
        if self._last_tx_was_test_tone:
            self._tx_panel.set_status("Transmitting test tone…")
            self.statusBar().showMessage("Transmitting test tone…")
        else:
            self._tx_panel.set_status("Transmitting…")
            self.statusBar().showMessage("Transmitting")
        # Lock rig controls for the duration of the transmission so the user
        # can't swap or disconnect the rig while PTT is keyed.
        self._radio_panel.set_tx_active(True)
        self._settings_action.setEnabled(False)
        # OP-47: suspend the 1 Hz rig-status poll while TX is active.
        # Otherwise get_freq / get_mode / get_strength reads race against
        # the PTT write on the same serial port. pyserial's lock serialises
        # them at the Python level, but on Windows the rapid CAT + PTT
        # interleave while the radio is mid-transmit causes the USB CODEC
        # (IC-7300, IC-705, FT-991A etc.) to renegotiate and drop both
        # the virtual COM port *and* the USB audio device — which surfaces
        # to the user as "the radio dropped out and I got a connection
        # error". RX works fine before this point because no concurrent
        # writer exists. Remember the pre-TX state so we only resume if
        # the rig was connected to begin with.
        self._rig_poll_was_active = self._rig_poll_timer.isActive()
        if self._rig_poll_was_active:
            self._rig_poll_timer.stop()
        # Gate the RX decoder to prevent self-decode through loopback (R-2).
        self._request_rx_gate.emit(True)
        self._rx_panel.set_status("RX paused during TX.")

    def _unlock_rig_controls(self) -> None:
        """Re-enable rig UI after TX completes, aborts, or errors.

        Also resumes the 1 Hz rig-status poll if it was running at TX
        start (suspended in ``_on_tx_started`` — see OP-47 comment there).
        If the rig was disconnected mid-TX, the poll timer stays stopped;
        the existing disconnect path (``_on_rig_disconnect``) is what
        clears it, and re-starting here would poll a dead port.
        """
        # v0.4.0 audit high #1: if an involuntary disconnect happened
        # mid-TX, its backend teardown was deferred so the worker's
        # unkey retries kept a live rig.  The completion/abort signal
        # that brought us here fires only after _run_tx fully unwound
        # (unkey included) — safe to finish the teardown now.
        self._finish_deferred_rig_teardown()
        self._radio_panel.set_tx_active(False)
        self._settings_action.setEnabled(True)
        # Only resume polling if the rig is still a real backend. If the
        # user (or a disconnect handler) swapped back to ManualRig during
        # TX, restarting the timer would poll a no-op backend and also
        # override the disconnect intent.
        if self._rig_poll_was_active and not isinstance(self._rig, ManualRig):
            self._rig_poll_timer.start()
        self._rig_poll_was_active = False

    def _schedule_rx_resume(self) -> None:
        """Lift the RX gate 50 ms after TX ends.

        The brief delay lets any trailing RF (and the PortAudio callback
        that was already queued) drain before the decoder resumes, so no
        TX-period audio bleeds into the next RX attempt.  The gate-off
        also calls RxWorker.reset() so the counter and decoder start clean.
        """
        # OP2-19: guard against post-close firing — isVisible() is False
        # once closeEvent has run, so the callback becomes a no-op.
        QTimer.singleShot(50, lambda: self.isVisible() and self._request_rx_gate.emit(False))

    @Slot(object, object)
    def _on_tx_image_prepared(self, image: object, mode: object) -> None:
        """Stash the composited TX image (post-banner) for optional auto-save.

        Fired by ``TxWorker`` after the banner has been stamped but
        before encoding begins, so the saved file matches what's
        actually on the air.  Test-tone transmissions never emit this
        signal — the stash is cleared on every TX kickoff and after
        every save, so a follow-up test tone can't accidentally
        re-save the previous real image.
        """
        pil_image: PILImage = image  # type: ignore[assignment]
        self._last_tx_image = pil_image
        self._last_tx_mode = mode  # type: ignore[assignment]

    @Slot()
    def _on_tx_complete(self) -> None:
        self._tx_panel.set_transmitting(False)
        if self._last_tx_was_test_tone:
            self._last_tx_was_test_tone = False
            alc_msg = (
                "Test tone complete. "
                "If ALC didn't move, check: "
                "(1) Radio's USB MOD Level menu, "
                "(2) this app's TX gain slider, "
                "(3) computer output volume."
            )
            self._tx_panel.set_status(alc_msg)
            self.statusBar().showMessage(alc_msg, 10000)
        else:
            # v0.2.8: auto-save the transmitted image when enabled. We
            # save here (rather than in ``_on_tx_image_prepared``) so a
            # cancelled or errored TX doesn't produce a file that was
            # never actually put on the air.
            tx_image_path: Path | None = None
            if (
                self._config.autosave_tx
                and self._last_tx_image is not None
                and self._last_tx_mode is not None
            ):
                tx_image_path = self._autosave_image(
                    self._last_tx_image,
                    self._last_tx_mode,
                    "TX",
                    status_verb="TX saved",
                )
            # v0.4: draft a logbook entry for the completed TX.  The
            # QSO-state bar (ToCall / RST / Name / Note) pre-fills the
            # contact side; frequency comes from the rig-poll cache
            # (still holding the pre-TX value — see _last_rig_freq_hz).
            if self._last_tx_image is not None and self._last_tx_mode is not None:
                qso_state = self._tx_panel.get_qso_state()
                draft = self._logbook_coordinator.build_tx_draft(
                    mode=self._last_tx_mode,
                    frequency_hz=self._last_rig_freq_hz,
                    image_path=tx_image_path,
                    tocall=qso_state.tocall,
                    rst_sent=qso_state.rst,
                    to_name=qso_state.tocall_name,
                    note=qso_state.note,
                )
                self._capture_qso(draft, self._last_tx_image, self._last_tx_mode)
            self._last_tx_image = None
            self._last_tx_mode = None
            self._tx_panel.set_status("Transmission complete.")
            self.statusBar().showMessage("Ready")
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    @Slot(float)
    def _on_watchdog_fired(self, duration_s: float) -> None:
        """Watchdog tripped: record the fact so _on_tx_aborted can display
        a persistent message instead of the generic "Ready".

        ``duration_s`` is the budget (seconds) the firing timer was
        created with — stage 1 (encode) or stage 2 (per-transmission
        playback).  Forwarded from ``TxWorker.watchdog_fired`` so the
        UI message can quote the actual number.
        """
        self._last_abort_was_watchdog = True
        self._last_watchdog_duration_s = duration_s

    @Slot()
    def _on_tx_aborted(self) -> None:
        self._tx_panel.set_transmitting(False)
        # v0.2.8: drop any stashed image — aborted transmissions must
        # not be auto-saved on the next successful TX.
        self._last_tx_image = None
        self._last_tx_mode = None
        if self._tx_error_pending:
            # _on_tx_error already set the status and status bar message;
            # don't overwrite it — the error text is more useful than
            # "Transmission aborted." and the status bar already has an
            # 8-second timeout on the error string.
            self._tx_error_pending = False
        elif self._last_abort_was_watchdog:
            self._last_abort_was_watchdog = False
            msg = (
                f"TX watchdog: exceeded {self._last_watchdog_duration_s:.0f} s "
                "— rig unkeyed automatically"
            )
            self._tx_panel.set_status(msg)
            self.statusBar().showMessage(msg)
        else:
            if self._last_tx_was_test_tone:
                self._last_tx_was_test_tone = False
                self._tx_panel.set_status("Test tone stopped.")
            else:
                self._tx_panel.set_status("Transmission aborted.")
            self.statusBar().showMessage("Ready")
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    @Slot(str)
    def _on_tx_error(self, message: str) -> None:
        self._last_tx_was_test_tone = False
        # v0.2.8: same reasoning as ``_on_tx_aborted`` — a failed TX
        # must not leak into the next successful one's auto-save.
        self._last_tx_image = None
        self._last_tx_mode = None
        self._tx_panel.set_transmitting(False)
        self._tx_panel.set_status(f"Error: {message}")
        self.statusBar().showMessage(message, 8000)
        self._tx_error_pending = True
        self._unlock_rig_controls()
        self._schedule_rx_resume()

    # === RX slots ===

    @Slot(bool)
    def _on_capture_requested(self, start: bool) -> None:
        """Translate the panel's Start/Stop toggle into worker calls.

        Emission goes via the private ``_request_start_capture`` /
        ``_request_stop_capture`` signals so the audio-worker slots
        actually run on the audio worker thread (queued connection)
        rather than on the GUI thread.

        On start, the audio capture is deferred until the RxWorker's
        ``reset_done`` fires (OP-05): emitting ``_request_rx_reset`` and
        ``_request_start_capture`` simultaneously from the GUI thread
        races on two different worker threads, and a pre-queued chunk
        from an already-warm device can reach ``feed_chunk`` before the
        reset slot runs.  The one-shot ``reset_done → start_capture``
        connection sequences the two steps deterministically.
        """
        if start:
            # Clear any stale device-loss / stream-error state from the
            # previous session so _on_rx_started and _on_rx_stopped behave
            # correctly for this new attempt.
            self._last_rx_disconnect_msg = ""
            self._last_rx_audio_error_msg = ""
            # Defensive: if _on_rx_started never fired (e.g. stream-open
            # failed last time), the suppress flag may still be True.
            # Clear it here so the next status updates are visible.
            self._suppress_rx_status_updates = False
            # Reset the decoder + sample counter so each new capture session
            # starts from zero rather than accumulating across stop/restart
            # cycles (bug R-1: counter climbed past 127s with no image).
            # Defer the start-capture request until reset_done arrives so
            # the two worker threads are ordered correctly (OP-05).
            #
            # H7: rapid Start / Stop / Start used to leave stale closures
            # connected to ``reset_done`` because the disconnect inside
            # each closure only removed itself.  A second click before
            # the first reset completed enqueued a second closure;
            # both fired on the next ``reset_done`` → two
            # ``_request_start_capture`` emissions → InputStreamWorker
            # raised "already running" via the error signal.  Disconnect
            # any prior closure by reference (not nuclear ``disconnect()``,
            # which prints a RuntimeWarning from PySide6 when there's
            # nothing connected) before installing the new one.
            if self._start_once_closure is not None:
                try:
                    self._rx_worker.reset_done.disconnect(
                        self._start_once_closure
                    )
                except (RuntimeError, TypeError):
                    pass
                self._start_once_closure = None

            def _start_once() -> None:
                # Disconnect ourselves before emitting so a later reset()
                # (e.g. user clicks Clear) doesn't retrigger start_capture.
                try:
                    self._rx_worker.reset_done.disconnect(_start_once)
                except (RuntimeError, TypeError):
                    pass
                if self._start_once_closure is _start_once:
                    self._start_once_closure = None
                # Re-enumerate AFTER _pa_reset (which fires before reset_done)
                # so a USB replug gets the new PortAudio device index rather
                # than the stale pre-reset index captured before the reset.
                #
                # M16: only re-resolve if the device is actually known to
                # have changed (``_input_device_needs_relookup`` is set by
                # ``_on_audio_device_lost``).  ``sd.query_devices()`` can
                # block the GUI thread for 50–500 ms on macOS Core Audio
                # after a USB event; gating on the lost-flag means the
                # common case (clean start, no replug) skips the lookup
                # entirely.
                if (
                    self._input_device_needs_relookup
                    and self._config.audio_input_device
                ):
                    fresh = find_input_device_by_name(
                        self._config.audio_input_device
                    )
                    if fresh is not None:
                        self._input_device = fresh
                    self._input_device_needs_relookup = False
                self._request_start_capture.emit(
                    self._input_device, self._config.sample_rate, DEFAULT_BLOCKSIZE
                )

            self._start_once_closure = _start_once
            self._rx_worker.reset_done.connect(_start_once)
            self._request_rx_reset.emit()
        else:
            # Cancel any in-flight decode before stopping audio so the tail
            # flush triggered by audio_worker.stopped doesn't block the worker
            # thread for several seconds on a large buffer.
            self._rx_worker.request_cancel()
            self._request_stop_capture.emit()

    @Slot()
    def _on_rx_started(self) -> None:
        if self._last_rx_disconnect_msg:
            # stream_error arrived at the GUI before this started signal —
            # the stream opened and immediately closed due to device loss.
            # Don't let this late started clobber the disconnect message.
            return
        self._capture_running = True
        self._suppress_rx_status_updates = False
        self._rx_panel.set_capturing(True)
        self.statusBar().showMessage("Capturing")

    @Slot()
    def _on_rx_stopped(self) -> None:
        self._capture_running = False
        self._suppress_rx_status_updates = True
        self._rx_panel.set_capturing(False)
        if self._last_rx_disconnect_msg:
            msg = self._last_rx_disconnect_msg
            self._last_rx_disconnect_msg = ""
            self._rx_panel.set_status(msg)
            self.statusBar().showMessage(msg)
        elif self._last_rx_audio_error_msg:
            # stream-open failed: keep the error visible instead of
            # overwriting with "Not listening…"
            msg = self._last_rx_audio_error_msg
            self._last_rx_audio_error_msg = ""
            self._rx_panel.set_status(msg)
            self.statusBar().showMessage(msg, 5000)
        else:
            self._rx_panel.set_status("Not listening — click Start to begin.")
            self.statusBar().showMessage("Ready")

    @Slot(str)
    def _on_audio_device_lost(self, message: str) -> None:
        """Device-loss path: store message so _on_rx_stopped can re-show it."""
        self._last_rx_disconnect_msg = message
        self._suppress_rx_status_updates = True
        self._rx_panel.set_status(message)
        self.statusBar().showMessage(message)  # sticky — no timeout
        # M16: USB replug typically reassigns PortAudio device indices.
        # Signal _start_once that it must re-resolve the saved device
        # name on the next capture start.
        self._input_device_needs_relookup = True

    @Slot(str)
    def _on_rx_status_update(self, text: str) -> None:
        """Gate for RxWorker.status_update — suppressed when capture is idle.

        RxWorker emits "Listening… Xs buffered, waiting for signal." on a
        periodic timer.  Without this gate, those updates arrive at the GUI
        *after* _on_rx_stopped and overwrite the "Not listening" / disconnect
        messages that _on_rx_stopped just set.
        """
        if not self._suppress_rx_status_updates:
            self._rx_panel.set_status(text)

    @Slot()
    def _on_rx_clear(self) -> None:
        # Cancel any in-flight decode immediately (thread-safe flag set)
        # before the queued reset() slot clears the buffer on the worker thread.
        self._rx_worker.request_cancel()
        self._request_rx_reset.emit()
        self._rx_panel.set_status("Cleared — waiting for VIS header.")

    def _build_save_context(
        self, mode: object, direction: str
    ) -> TokenContext:
        """Build a :class:`TokenContext` from the current config + runtime.

        Centralised so RX auto-save, TX auto-save, and the manual save
        path all resolve the same token vocabulary against the same
        fixture-friendly clock.  ``mode`` may arrive as either a
        :class:`Mode` enum or a bare ``str`` (Qt unwraps ``StrEnum``
        through queued signals), so we normalise it here.
        """
        mode_name = mode.value if isinstance(mode, Mode) else str(mode)
        return TokenContext(
            callsign=self._config.callsign or "",
            mode=mode_name,
            direction=direction,  # type: ignore[arg-type]
            now_utc=datetime.datetime.now(datetime.UTC),
        )

    def _autosave_image(
        self,
        image: PILImage,
        mode: object,
        direction: str,
        *,
        status_verb: str = "Auto-saved",
    ) -> Path | None:
        """Resolve the filename template and write ``image`` to disk.

        Returns the saved path on success, ``None`` on failure (a
        warning dialog is shown for OSError).  Shared by the RX and TX
        auto-save call sites so both consume the same
        ``autosave_filename_pattern`` and ``autosave_file_format``
        config fields.
        """
        save_dir = Path(self._config.images_save_dir)
        ctx = self._build_save_context(mode, direction)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            path = build_autosave_filename(
                self._config.autosave_filename_pattern,
                save_dir,
                ctx,
                file_format=self._config.autosave_file_format,
            )
            image.save(str(path))
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return None
        self.statusBar().showMessage(f"{status_verb} {path.name}", 3000)
        return path

    @Slot(object, object, int, int)
    def _on_rx_audio_ready(
        self, audio: object, mode: object, vis_code: int, sample_rate: int
    ) -> None:
        """Buffer raw RX audio until the companion image_complete signal fires.

        ``rx_audio_ready`` is emitted just before ``image_complete`` in the
        same ``_dispatch`` call on the worker thread, so both signals arrive
        on the GUI thread in order — the audio is always buffered by the time
        ``_on_rx_image_complete`` runs.
        """
        self._pending_rx_audio = (audio, sample_rate)

    @Slot(object, object, int)
    def _on_rx_image_complete(
        self, image: object, mode: object, vis_code: int, *, offline: bool = False
    ) -> None:
        """Auto-save a newly decoded image, then draft a logbook entry.

        Fires for live RX, offline WAV decode (via the
        ``_on_offline_image_complete`` wrapper), and watchdog-truncated
        partial images alike — all of them are receptions worth
        logging, and the capture dialog costs one Esc to dismiss when
        they aren't (noise trigger, test decode).
        """
        pending_audio = self._pending_rx_audio
        self._pending_rx_audio = None

        pil_image: PILImage = image  # type: ignore[assignment]
        save_path: Path | None = None
        if self._config.auto_save:
            save_path = self._autosave_image(pil_image, mode, "RX")

        audio_path: Path | None = None
        if self._config.autosave_rx_audio and pending_audio is not None:
            audio_arr, sr = pending_audio
            fmt = self._config.rx_audio_format
            audio_path = self._save_rx_audio(
                audio_arr, mode, sr, fmt, alongside=save_path
            )

        # v0.6 (Phase 2): the decode finished — clear the live preview and
        # tell connected remote viewers.  ``gallery.new`` only fires when a
        # file was actually written (the browser fetches it from the
        # gallery), so it's gated on auto-save having produced a path.
        self._remote_service.set_live_image(None)
        if save_path is not None:
            self._remote_hub.publish({"type": "gallery.new"})
        self._remote_hub.publish({"type": "rx.complete", "mode": str(mode)})

        # Audit #3: a stop-flush can deliver one last image_complete
        # during closeEvent's shutdown drain.  The image auto-save
        # above still runs (data preservation, matches v0.3), but the
        # capture flow must not — the logbook store is being closed
        # and a dialog would outlive the window.
        if self._closing:
            return

        # v0.4: capture the reception in the logbook — gated, because
        # SSTV calling frequencies are party lines and most of what a
        # monitoring station decodes is other people's exchanges.
        # "Engaged" = the TX panel's ToCall is filled in (you're
        # working someone).  auto_log_qsos overrides the gate and
        # drafts everything silently.
        engaged = bool(self._tx_panel.get_qso_state().tocall.strip())
        prompt_mode = self._config.rx_capture_prompt
        if not self._logbook_coordinator.auto_log and (
            prompt_mode == "never" or (prompt_mode == "in_qso" and not engaged)
        ):
            self.statusBar().showMessage(
                "Image decoded — right-click it in the gallery to log a QSO", 5000
            )
            return
        # Audit #4: live RX stamps the rig-poll frequency cache (≤1 s
        # old, None with no rig) and the current clock.  An offline
        # file decode gets NO frequency (the rig's dial says nothing
        # about a recording) and the file's modified time — the best
        # available stand-in for when the signal was actually on the
        # air.
        time_utc: datetime.datetime | None = None
        frequency_hz = self._last_rig_freq_hz
        if offline:
            frequency_hz = None
            if self._offline_decode_source is not None:
                try:
                    time_utc = datetime.datetime.fromtimestamp(
                        self._offline_decode_source.stat().st_mtime,
                        tz=datetime.UTC,
                    )
                except OSError:
                    time_utc = None  # file vanished — fall back to now
        draft = self._logbook_coordinator.build_rx_draft(
            mode=mode,
            frequency_hz=frequency_hz,
            image_path=save_path,
            audio_path=audio_path,
            time_utc=time_utc,
        )
        self._capture_qso(draft, pil_image, mode, draft_when_busy=engaged)

    @Slot(object, object, int)
    def _on_offline_image_complete(
        self, image: object, mode: object, vis_code: int
    ) -> None:
        """Offline-decode completions → capture flow minus live-rig context.

        Separate slot (rather than ``sender()`` sniffing) so the
        offline path is explicit at the connect site and directly
        testable (audit #4).
        """
        self._on_rx_image_complete(image, mode, vis_code, offline=True)

    def _save_rx_audio(
        self,
        audio_f64: object,
        mode: object,
        sample_rate: int,
        fmt: str,
        *,
        alongside: Path | None = None,
    ) -> Path | None:
        """Write *audio_f64* (float64, [-1,1]) to an audio file.

        *fmt* is ``"wav"`` (stdlib, 16-bit PCM) or ``"flac"`` (soundfile,
        lossless compressed — ~40% smaller than WAV at no quality cost).
        Both are lossless; lossy formats are excluded because compression
        artefacts degrade SSTV re-decode quality.

        If *alongside* is a path to an already-saved image, the audio file
        is written next to it with the same stem. Otherwise a filename is
        resolved from the save-directory template.

        Returns the written path, or ``None`` when nothing was written
        (empty buffer or save failure) — v0.4 stores it on the QSO row.
        """
        arr: np.ndarray = np.asarray(audio_f64, dtype=np.float64)
        if arr.size == 0:
            return None

        fmt = fmt.lower().lstrip(".")
        if fmt not in ("wav", "flac"):
            fmt = "wav"

        if alongside is not None:
            out_path = alongside.with_suffix(f".{fmt}")
        else:
            save_dir = Path(self._config.images_save_dir)
            ctx = self._build_save_context(mode, "RX")
            try:
                save_dir.mkdir(parents=True, exist_ok=True)
                out_path = build_autosave_filename(
                    self._config.autosave_filename_pattern,
                    save_dir,
                    ctx,
                    file_format=fmt,
                )
            except OSError as exc:
                QMessageBox.warning(self, "Audio save failed", str(exc))
                return None

        try:
            if fmt == "wav":
                pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
                # H-4 (audit 4.7/v0.2.9): open via pathlib.Path.open so
                # non-ASCII characters in *out_path* on Windows go through
                # the wide-char OS API.  Passing ``str(out_path)`` directly
                # to wave.open would encode through the active ANSI code
                # page and mangle paths containing emoji or non-ANSI
                # characters under non-UTF-8 locales.
                with out_path.open("wb") as raw, wave.open(raw, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm.tobytes())
            else:
                import soundfile as sf  # noqa: PLC0415 — lazy optional dep
                sf.write(str(out_path), arr, sample_rate, subtype="PCM_16")
        except OSError as exc:
            QMessageBox.warning(self, "Audio save failed", str(exc))
            return None
        except ImportError:
            QMessageBox.warning(
                self,
                "Audio save failed",
                "FLAC recording requires the 'soundfile' package.\n"
                'Install it with:  pip install "open-sstv[flac]"',
            )
            return None
        self.statusBar().showMessage(f"Audio saved {out_path.name}", 3000)
        return out_path

    @Slot(object, object)
    def _on_rx_image_saved(self, image: object, mode: object) -> None:
        """Save a decoded image to disk.

        If auto-save is enabled, writes directly to the configured save
        directory using the filename template. Otherwise, opens a
        ``QFileDialog`` so the user can choose where to save — but the
        template-resolved name is pre-populated so the user still gets
        their configured convention as the default.
        """
        pil_image: PILImage = image  # type: ignore[assignment]

        if self._config.auto_save:
            self._autosave_image(pil_image, mode, "RX", status_verb="Saved")
            return

        # Manual save: seed the dialog with the template-resolved name so
        # the user's filename convention is honoured by default, but they
        # can still override it (and pick a different directory / format).
        save_dir = Path(self._config.images_save_dir)
        ctx = self._build_save_context(mode, "RX")
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            suggested = build_autosave_filename(
                self._config.autosave_filename_pattern,
                save_dir,
                ctx,
                file_format=self._config.autosave_file_format,
            )
        except OSError:
            suggested = save_dir / f"sstv.{self._config.autosave_file_format}"
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save decoded image",
            str(suggested),
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;All files (*)",
        )
        if path_str:
            try:
                pil_image.save(path_str)
                self.statusBar().showMessage(
                    f"Saved {Path(path_str).name}", 3000
                )
            except OSError as exc:
                QMessageBox.warning(self, "Save failed", str(exc))

    @Slot()
    def _on_save_shortcut(self) -> None:
        """Ctrl+S: save the most recent decoded image."""
        self._rx_panel.save_current_image()

    @Slot(str)
    def _on_rx_error(self, message: str) -> None:
        self._rx_panel.set_status(f"RX: {message}")
        self.statusBar().showMessage(message, 5000)

    @Slot(str, object)
    def _handle_worker_error(self, slot_name: str, exc: object) -> None:
        """Centralized worker-error sink (M4 audit follow-up).

        TxWorker / RxWorker emit ``error_occurred(slot_name, exc)`` for
        failures that previously got logged-and-swallowed.  Surfacing them
        through one slot means we get:

        * a single place to log the full traceback (via ``exc_info=True``)
        * a consistent status-bar message format
        * an obvious target for tests to assert against

        ``slot_name`` identifies the worker entry point that raised so the
        log is grep-able; ``exc`` is the live exception object so the log
        captures the type and message and traceback without the worker
        having to stringify them.
        """
        _log.error(
            "Worker error in %s: %s", slot_name, exc, exc_info=exc if isinstance(exc, BaseException) else None,
        )
        self.statusBar().showMessage(f"{slot_name}: {exc}", 5000)

    @Slot(str)
    def _on_rx_audio_error(self, message: str) -> None:
        """Audio-worker error slot that survives the _on_rx_stopped overwrite.

        When stream-open fails, InputStreamWorker emits error then stopped in
        the same event-loop cycle.  _on_rx_stopped would clobber the error
        message with "Not listening…" before the user sees it.  We store the
        message here so _on_rx_stopped can re-show it in its elif branch.
        """
        formatted = f"RX: {message}"
        if not self._capture_running:
            # start-time failure — preserve for _on_rx_stopped
            self._last_rx_audio_error_msg = formatted
        self._rx_panel.set_status(formatted)
        self.statusBar().showMessage(message, 5000)

    # === Rig connect / disconnect / poll ===

    @Slot()
    def _on_rig_connect(self) -> None:
        """Create a rig backend from the current config and start polling.

        Dispatches to either a Direct Serial backend or a rigctld TCP
        client depending on ``rig_connection_mode``. For rigctld, if
        ``auto_launch_rigctld`` is enabled and a radio model is configured,
        spawns rigctld automatically before connecting.
        """
        mode = self._config.rig_connection_mode

        # OP-28: dispatch via RigConnectionMode rather than string literals
        # so a future enum rename can't silently break one of the three
        # call sites that used to carry bare strings.
        if mode == RigConnectionMode.MANUAL:
            # Shouldn't normally reach here, but handle gracefully
            self.statusBar().showMessage(
                "Rig mode set to Manual — configure a connection in Settings first.", 5000,
            )
            return

        if mode == RigConnectionMode.SERIAL:
            self._connect_serial()
        elif mode == RigConnectionMode.TCI:
            self._connect_tci()
        elif mode == RigConnectionMode.FLEX:
            self._connect_flex()
        else:
            self._connect_rigctld()

    def _start_rig_connect_thread(
        self,
        rig: Rig,
        on_success: Callable[[Rig], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Spin up a one-shot QThread to run rig.open() + rig.ping().

        Results are delivered via ``_RigConnectRelay``, a QObject that lives
        on the GUI thread.  AutoConnection from the worker thread promotes to
        QueuedConnection automatically, so on_success/on_error always execute
        on the GUI event loop — never on the worker thread where widget
        mutations would be silently dropped on macOS.

        A ``_CONNECT_TIMEOUT_S`` QTimer fires on_error with a "timed out"
        message if the rig never responds.  The cancel event lets the caller
        (cancel button, timeout, closeEvent) suppress any late emit.
        """
        cancel = threading.Event()
        self._connect_cancel = cancel

        worker = _RigConnectWorker(rig, cancel)
        thread = QThread(self)
        thread.setObjectName("rig-connect-thread")
        self._connect_thread = thread
        # Keep a strong Python reference to the worker so CPython's reference
        # counting doesn't destroy it (and its signal connections) between
        # _start_rig_connect_thread returning and the thread actually starting.
        # PySide6 signal connections only hold a *weak* reference to the
        # receiver object; if the last strong ref drops, the C++ QObject is
        # deleted and thread.started → worker.run becomes a dead connection.
        self._connect_worker = worker

        timeout_timer = QTimer(self)
        timeout_timer.setSingleShot(True)
        self._connect_timeout_timer = timeout_timer

        # Relay lives on the GUI thread (no moveToThread).  AutoConnection
        # from the worker thread → QueuedConnection → slots run on GUI thread.
        relay = _RigConnectRelay(on_success, on_error, thread, timeout_timer, cancel)
        self._connect_relay = relay

        # Connect all signals BEFORE moveToThread.  AutoConnection re-evaluates
        # at emit time (Qt docs), so QueuedConnection is still used for
        # cross-thread emits after the move.  Connecting first is cleaner and
        # avoids a window where the thread is running but signals are unwired.
        thread.started.connect(worker.run)
        worker.succeeded.connect(relay.on_succeeded)
        worker.failed.connect(relay.on_failed)
        # Do NOT connect worker.deleteLater or relay.deleteLater here.
        # worker.deleteLater called from thread.finished (worker thread) posts
        # a DeferredDelete to the exiting thread; Qt processes it when the
        # thread exits — before _on_connect_thread_finished clears the Python
        # ref.  That races with CPython's refcount destructor and produces a
        # QObjectWrapper use-after-free.  Python GC via ref-clearing in
        # _on_connect_thread_finished is the sole owner; deleteLater is not
        # needed.  thread.deleteLater is safe because thread lives on the GUI
        # thread and its DeferredDelete processes on the GUI event loop.
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_connect_thread_finished)

        worker.moveToThread(thread)

        def _on_timeout() -> None:
            if not cancel.is_set():
                cancel.set()
                thread.quit()
                on_error(
                    "Connection timed out — check that your radio is "
                    "connected and powered on."
                )

        timeout_timer.timeout.connect(_on_timeout)

        thread.start()
        timeout_timer.start(int(self._CONNECT_TIMEOUT_S * 1000))

    @Slot()
    def _on_connect_thread_finished(self) -> None:
        """Clear connect-thread state when the thread exits normally."""
        thread = self._connect_thread
        worker = self._connect_worker
        # Explicitly break thread.started → worker.run before releasing Python
        # refs.  This removes Qt's internal connection record (which holds a
        # ref to worker) so Qt doesn't touch the object after Python GC frees
        # it when we set self._connect_worker = None below.
        if thread is not None and worker is not None:
            try:
                thread.started.disconnect(worker.run)
            except (RuntimeError, TypeError):
                pass
        self._connect_thread = None
        self._connect_cancel = None
        self._connect_worker = None
        self._connect_relay = None
        if self._connect_timeout_timer is not None:
            self._connect_timeout_timer.stop()
            self._connect_timeout_timer = None

    @Slot()
    def _on_connect_cancel(self) -> None:
        """User clicked Cancel during a connect attempt."""
        self._abort_connect()
        self._radio_panel.set_connected(False)
        self.statusBar().showMessage("Connection cancelled.", 3000)

    def _abort_connect(self) -> None:
        """Cancel any in-flight connect attempt and wait for its thread to stop.

        Safe to call when no connect is in flight (all refs will be None).
        Called from closeEvent to prevent QThread::~QThread() fatal() when
        the window is destroyed while the worker is still blocking in open().
        Uses objectName to find *all* lingering connect threads, handling the
        edge case where a timeout fired and the user started a second attempt
        before the first thread finished its blocking C call.
        """
        timer = self._connect_timeout_timer
        cancel = self._connect_cancel
        self._connect_thread = None
        self._connect_cancel = None
        self._connect_timeout_timer = None
        self._connect_relay = None

        if timer is not None:
            timer.stop()
        if cancel is not None:
            cancel.set()

        for thread in self.findChildren(QThread):
            if thread.objectName() == "rig-connect-thread" and thread.isRunning():
                thread.quit()
                if not thread.wait(2000):
                    thread.terminate()
                    thread.wait(500)

        # Drop the worker reference only after all threads have stopped.
        # Clearing it earlier would destroy the C++ QObject while the worker
        # thread might still be executing worker.run(), which crashes.
        self._connect_worker = None

    def _connect_serial(self) -> None:
        """Create a direct serial rig backend and start polling.

        OP2-02: open() + ping() run on a background thread via
        ``_RigConnectWorker`` so the GUI never freezes for the CAT round-trip.
        """
        port = self._config.rig_serial_port
        if not port:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                "No serial port configured — open Settings > Radio.", 5000,
            )
            return

        try:
            rig = create_serial_rig(
                protocol=self._config.rig_serial_protocol,
                port=port,
                baud_rate=self._config.rig_baud_rate,
                ci_v_address=self._config.rig_civ_address,
                ptt_line=self._config.rig_ptt_line,
            )
        except RigError as exc:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Serial connection failed on {port} — {exc}", 5000,
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(f"Serial connection failed: {exc}", 5000)
            return

        protocol = self._config.rig_serial_protocol
        self._radio_panel.set_connecting()
        self.statusBar().showMessage(
            f"Connecting via {protocol} on {port}…"
        )

        def _on_success(connected_rig: Rig) -> None:
            if not self.isVisible():
                try:
                    connected_rig.close()
                except RigError:
                    pass
                return
            self._rig = connected_rig
            self._tx_worker.set_rig(connected_rig)
            self._rig_poll_worker.set_rig(connected_rig)
            self._radio_panel.set_connected(True)
            self._rig_poll_timer.start()
            self.statusBar().showMessage(
                f"Connected via {protocol} on {port}", 3000,
            )

        def _on_error(message: str) -> None:
            if not self.isVisible():
                return
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Serial connection failed on {port} — {message}", 5000,
            )

        self._start_rig_connect_thread(rig, _on_success, _on_error)

    def _connect_rigctld(self) -> None:
        """Create a RigctldClient and start polling, optionally launching rigctld."""
        host = self._config.rigctld_host
        port = self._config.rigctld_port

        # Auto-launch rigctld if configured
        if (
            self._config.auto_launch_rigctld
            and self._config.rig_model_id > 0
            and self._rigctld_proc is None
        ):
            # OP-13: reject leading-dash values so a hand-edited config
            # can't slip an arbitrary rigctld flag into the argv.
            if not is_safe_rigctld_arg(self._config.rig_serial_port):
                self.statusBar().showMessage(
                    f"Refusing to launch rigctld with unsafe serial port "
                    f"{self._config.rig_serial_port!r} — "
                    "edit Settings → Radio → Serial port.",
                    8000,
                )
                return
            cmd = [
                "rigctld",
                "-m", str(self._config.rig_model_id),
                "-t", str(port),
            ]
            if self._config.rig_serial_port:
                cmd += ["-r", self._config.rig_serial_port]
            if self._config.rig_baud_rate:
                cmd += ["-s", str(self._config.rig_baud_rate)]
            try:
                # OP2-14: start_new_session=True on POSIX isolates the child
                # into its own process group so a GUI crash/SIGKILL doesn't
                # orphan rigctld holding the serial port.
                self._rigctld_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                # v0.4.0 audit high #2: the stderr pipe MUST be drained.
                # hamlib chatters per-transaction errors on a flaky CAT
                # cable; once the OS pipe buffer (~64 KB) fills, rigctld
                # blocks on write(2) and stops servicing its TCP socket
                # entirely — worst case while PTT is keyed.  Draining
                # into our log also lands hamlib's diagnostics in the
                # diagnostics zip, where they belong.
                _drain_subprocess_stderr(self._rigctld_proc, "rigctld")
            except FileNotFoundError:
                self.statusBar().showMessage(
                    "rigctld not found — install Hamlib, or switch to Direct Serial in Settings", 5000,
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.statusBar().showMessage(f"Failed to launch rigctld: {exc}", 5000)
                return
            # Give rigctld 500 ms to bind the port without freezing the GUI.
            QTimer.singleShot(500, lambda: self._finish_rigctld_connect(host, port))
            return

        self._finish_rigctld_connect(host, port)

    def _finish_rigctld_connect(self, host: str, port: int) -> None:
        """Attempt the actual socket connection to rigctld.

        Called either immediately (no auto-launch) or after a 500 ms
        ``QTimer.singleShot`` delay when rigctld was just spawned.

        OP2-02: client.open() + ping() are pushed to a background thread so
        the GUI is never frozen for the TCP connect + CAT round-trip.
        OP2-09: guard against post-close firing from the 500 ms singleShot.
        """
        if not self.isVisible():
            return
        client = RigctldClient(host=host, port=port)
        self._radio_panel.set_connecting()
        self.statusBar().showMessage(f"Connecting to rigctld at {host}:{port}…")

        def _on_success(connected_rig: Rig) -> None:
            if not self.isVisible():
                try:
                    connected_rig.close()
                except RigError:
                    pass
                return
            self._rig = connected_rig
            self._tx_worker.set_rig(connected_rig)
            self._rig_poll_worker.set_rig(connected_rig)
            self._radio_panel.set_connected(True)
            self._rig_poll_timer.start()
            self.statusBar().showMessage(
                f"Connected to rigctld at {host}:{port}", 3000,
            )

        def _on_error(message: str) -> None:
            if not self.isVisible():
                return
            # v0.4.0 audit medium #9: if the rigctld we auto-launched
            # died at startup (wrong -m model, serial port held by
            # another program), keeping the dead Popen in
            # ``_rigctld_proc`` made the ``is None`` guard skip
            # auto-launch on every retry — the connection was
            # unrecoverable without restarting the app, even after the
            # user fixed the root cause.  ``poll()`` also reaps the
            # zombie.  A still-running daemon is left alone: the retry
            # will dial it again.
            proc = self._rigctld_proc
            if proc is not None and proc.poll() is not None:
                _log.warning(
                    "auto-launched rigctld exited with code %s — clearing "
                    "so the next Connect can respawn it",
                    proc.returncode,
                )
                self._rigctld_proc = None
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"Could not connect to rigctld at {host}:{port} — {message}",
                5000,
            )

        self._start_rig_connect_thread(client, _on_success, _on_error)

    def _connect_tci(self) -> None:
        """Create a TciRig + TciInputStreamWorker and start audio + CAT."""
        from open_sstv.radio.tci import TciRig

        host = self._config.tci_host
        port = self._config.tci_port
        rig = TciRig(host, port)

        self._radio_panel.set_connecting()
        self.statusBar().showMessage(f"Connecting to TCI server at {host}:{port}…")

        def _on_success(connected_rig: object) -> None:
            if not self.isVisible():
                try:
                    connected_rig.close()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
                return
            self._rig = connected_rig  # type: ignore[assignment]
            self._tx_worker.set_rig(connected_rig)  # type: ignore[arg-type]
            self._tx_worker.set_tci_connection(connected_rig.connection)  # type: ignore[union-attr]
            self._rig_poll_worker.set_rig(connected_rig)  # type: ignore[arg-type]
            # Hot-swap the audio worker to the TCI input stream.
            tci_audio = TciInputStreamWorker(connected_rig)
            self._swap_audio_worker(tci_audio)
            self._radio_panel.set_connected(True)
            self._rig_poll_timer.start()
            self.statusBar().showMessage(
                f"Connected to TCI at {host}:{port}", 3000
            )

        def _on_error(message: str) -> None:
            if not self.isVisible():
                return
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"TCI connection failed at {host}:{port} — {message}", 5000
            )

        self._start_rig_connect_thread(rig, _on_success, _on_error)

    def _connect_flex(self) -> None:
        """Connect a FlexRadio directly over the SmartSDR TCP API.

        Unlike TCI this is CAT only — the Flex's audio still arrives via
        DAX (or any sound device), so the audio worker is left alone.
        """
        from open_sstv.radio.flex import FlexRig

        host = self._config.flex_host.strip()
        port = self._config.flex_port
        slice_index = self._config.flex_slice
        if not host:
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                "No FlexRadio address configured — set it in Settings → Radio.",
                5000,
            )
            return

        rig = FlexRig(host, port, slice_index=slice_index)
        self._radio_panel.set_connecting()
        self.statusBar().showMessage(f"Connecting to FlexRadio at {host}:{port}…")

        def _on_success(connected_rig: object) -> None:
            if not self.isVisible():
                try:
                    connected_rig.close()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    pass
                return
            self._rig = connected_rig  # type: ignore[assignment]
            self._tx_worker.set_rig(connected_rig)  # type: ignore[arg-type]
            self._rig_poll_worker.set_rig(connected_rig)  # type: ignore[arg-type]
            self._radio_panel.set_connected(True)
            self._rig_poll_timer.start()
            self.statusBar().showMessage(
                f"Connected to FlexRadio at {host}:{port} (slice {slice_index})",
                3000,
            )

        def _on_error(message: str) -> None:
            if not self.isVisible():
                return
            self._radio_panel.set_connection_error()
            self.statusBar().showMessage(
                f"FlexRadio connection failed at {host}:{port} — {message}", 5000
            )

        self._start_rig_connect_thread(rig, _on_success, _on_error)

    def _swap_audio_worker(self, new_worker: object) -> None:
        """Hot-swap the audio input worker on the audio thread.

        Stops the current worker cleanly, disconnects its signals, moves the
        new worker to the audio thread, wires the same signal set, and
        re-starts capture if it was already running.

        Two correctness requirements drive the implementation:

        **C1 — stop before teardown.**  Simply calling ``deleteLater()`` on
        the old worker without first stopping it leaves the TCI subscription
        alive (``audio_stop:0;`` never reaches the server) and leaks the
        PortAudio stream.  ``QMetaObject.invokeMethod`` with
        ``BlockingQueuedConnection`` runs the ``stop`` slot on the audio
        thread and blocks the GUI thread until it completes, so the worker
        is fully quiesced before we touch its signal connections.

        **C2 — restore capture state.**  If RX was active when the swap was
        triggered (TCI connect/disconnect while capturing), the new worker
        must be started immediately after wiring so the user sees no
        interruption.  We snapshot ``_capture_running`` before teardown and
        re-emit ``_request_start_capture`` on the new worker afterward.
        """
        old = self._audio_worker

        # C1: stop the old worker on its own thread before disconnecting,
        # so the PortAudio stream and any TCI subscription are torn down
        # cleanly (audio_stop:0; reaches the server, stream.close() runs).
        #
        # H8: BlockingQueuedConnection would freeze the GUI forever if
        # stream.stop()/close() hangs — a known macOS Core Audio behaviour
        # after USB device removal.  Use a bounded wait instead: post the
        # stop() slot via QueuedConnection and pump the GUI event loop
        # until either the worker emits ``stopped`` or _SWAP_STOP_TIMEOUT_S
        # elapses.  On timeout we proceed with teardown anyway — the
        # wedged worker will leak its stream until process exit, but the
        # GUI doesn't hang and the user keeps control of the app.
        was_capturing = self._capture_running

        _SWAP_STOP_TIMEOUT_MS = 2000  # 2 s is generous for any real stop
        _stop_completed = threading.Event()

        def _on_swap_stop_done() -> None:
            _stop_completed.set()

        old.stopped.connect(  # type: ignore[union-attr]
            _on_swap_stop_done, Qt.ConnectionType.QueuedConnection
        )
        QMetaObject.invokeMethod(
            old,  # type: ignore[arg-type]
            "stop",
            Qt.ConnectionType.QueuedConnection,
        )

        # Pump events for up to _SWAP_STOP_TIMEOUT_MS while waiting for the
        # worker to finish.  QEventLoop runs nested event processing on
        # the GUI thread so queued ``stopped`` signals (and any other
        # housekeeping slots) still fire; a QTimer enforces the hard cap.
        _swap_loop = QEventLoop()
        _swap_timer = QTimer()
        _swap_timer.setSingleShot(True)
        _swap_timer.timeout.connect(_swap_loop.quit)
        old.stopped.connect(_swap_loop.quit, Qt.ConnectionType.QueuedConnection)  # type: ignore[union-attr]
        _swap_timer.start(_SWAP_STOP_TIMEOUT_MS)
        if not _stop_completed.is_set():
            _swap_loop.exec()
        _swap_timer.stop()
        try:
            old.stopped.disconnect(_on_swap_stop_done)  # type: ignore[union-attr]
        except (RuntimeError, TypeError):
            pass
        try:
            old.stopped.disconnect(_swap_loop.quit)  # type: ignore[union-attr]
        except (RuntimeError, TypeError):
            pass

        if not _stop_completed.is_set():
            _log.warning(
                "Audio worker stop() did not complete within %d ms — "
                "proceeding with teardown; the wedged worker will leak "
                "its stream until process exit, but the GUI stays "
                "responsive.",
                _SWAP_STOP_TIMEOUT_MS,
            )

        # Disconnect old worker from consumers.
        for signal, slot in (
            (old.chunk_ready, self._rx_worker.feed_chunk),
            (old.stopped, self._rx_worker.flush),
            (old.started, self._on_rx_started),
            (old.stopped, self._on_rx_stopped),
            (old.error, self._on_rx_audio_error),
            (old.stream_error, self._on_audio_device_lost),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        # Wire the new worker.
        new = new_worker  # type: ignore[assignment]
        new.moveToThread(self._audio_thread)
        new.chunk_ready.connect(self._rx_worker.feed_chunk)
        new.stopped.connect(self._rx_worker.flush)
        new.started.connect(self._on_rx_started)
        new.stopped.connect(self._on_rx_stopped)
        new.error.connect(self._on_rx_audio_error)
        new.stream_error.connect(self._on_audio_device_lost)

        # Reconnect the dispatch signals to the new worker.
        self._request_start_capture.disconnect()
        self._request_stop_capture.disconnect()
        self._request_start_capture.connect(new.start)
        self._request_stop_capture.connect(new.stop)

        self._audio_worker = new  # type: ignore[assignment]

        # Schedule old worker deletion on the audio thread.
        try:
            old.deleteLater()
        except Exception:  # noqa: BLE001
            pass

        # H1: clear the RX decoder's accumulated state before the new
        # audio source starts feeding chunks.  PortAudio and TCI samples
        # have different clock domains; concatenating them through the
        # incremental decoder's _scratch / _total_samples / half-buffered
        # VIS would almost guarantee a corrupted first decode after a
        # hot-swap.  reset() is idempotent and safe to call when the
        # decoder is already idle.
        try:
            self._rx_worker.reset()
        except Exception as exc:  # noqa: BLE001 — never block the swap
            _log.warning("RxWorker reset after swap failed: %s", exc)

        # C2: if capture was running before the swap, restart it on the
        # new worker so the user sees no gap in the RX stream.
        if was_capturing:
            self._request_start_capture.emit(
                self._input_device, self._config.sample_rate, DEFAULT_BLOCKSIZE
            )

    # === Waterfall ===

    @Slot(bool)
    def _on_toggle_waterfall(self, checked: bool) -> None:
        """Show or hide the floating waterfall window."""
        if checked:
            if self._waterfall_window is None:
                self._waterfall_window = WaterfallWindow(self)
                self._waterfall_window.set_sample_rate(self._config.sample_rate)
                self._waterfall_window.closed.connect(
                    lambda: self._waterfall_action.setChecked(False)
                )
                self._waterfall_window.closed.connect(
                    lambda: self._set_waterfall_config(False)
                )
            self._waterfall_window.show()
            self._waterfall_window.raise_()
        else:
            if self._waterfall_window is not None:
                self._waterfall_window.hide()
        self._set_waterfall_config(checked)
        # Sender-side gate: skip cross-thread tx_audio_chunk signals when
        # the window is hidden so ~10 Hz signal overhead doesn't accumulate
        # across a multi-minute SSTV transmission.
        self._tx_worker.set_waterfall_active(checked)

    def _set_waterfall_config(self, visible: bool) -> None:
        """Persist the waterfall visibility — both in-memory and to TOML.

        H3: the View → Waterfall menu toggle previously only wrote to
        ``self._config`` in memory and ``closeEvent`` never called
        ``save_config()`` for that mutation, so a bare toggle was forgotten
        across restarts unless the user also opened (and OK'd) Settings.
        Saving here makes the menu toggle stick the same way Settings does.
        """
        self._config.show_waterfall = visible
        try:
            save_config(self._config)
        except Exception as exc:  # noqa: BLE001 — UI toggle never blocks
            _log.warning("Could not persist waterfall visibility: %s", exc)

    @Slot(object)
    def _on_rx_waterfall_chunk(self, chunk: object) -> None:
        if self._waterfall_window is not None and self._waterfall_window.isVisible():
            self._waterfall_window.add_rx_column(chunk)

    @Slot(object)
    def _on_tx_waterfall_chunk(self, chunk: object) -> None:
        if self._waterfall_window is not None and self._waterfall_window.isVisible():
            self._waterfall_window.add_tx_column(chunk)

    @Slot()
    def _on_rig_disconnect(self) -> None:
        """Stop polling and tear down the rig link."""
        self._rig_poll_timer.stop()
        # v0.4: no rig → no trustworthy frequency for logbook drafts.
        self._last_rig_freq_hz = None
        try:
            self._rig.close()
        except RigError:
            pass
        self._rig = ManualRig()
        self._tx_worker.set_rig(self._rig)
        self._tx_worker.set_tci_connection(None)
        self._rig_poll_worker.set_rig(self._rig)
        self._radio_panel.set_connected(False)
        # If we were using TCI, swap the audio worker back to PortAudio.
        if isinstance(self._audio_worker, TciInputStreamWorker):
            self._swap_audio_worker(InputStreamWorker())
        # Stop rigctld if we launched it
        self._kill_rigctld()
        self.statusBar().showMessage("Rig disconnected.", 3000)

    @Slot()
    def _on_radio_disconnected(self) -> None:
        """USB unplug detected: stop polling and revert to disconnected state.

        Fired by ``_RigPollWorker`` after ``_POLL_FAIL_THRESHOLD`` consecutive
        poll failures.  Mirrors ``_on_rig_disconnect`` but shows a different
        status message so the user knows the disconnect was involuntary.
        """
        if isinstance(self._rig, ManualRig):
            return  # already disconnected — guard against a queued double-fire
        self._rig_poll_timer.stop()
        # v0.4: no rig → no trustworthy frequency for logbook drafts.
        self._last_rig_freq_hz = None
        old_rig = self._rig
        self._rig = ManualRig()
        self._tx_worker.set_rig(self._rig)
        self._tx_worker.set_tci_connection(None)
        self._rig_poll_worker.set_rig(self._rig)
        self._radio_panel.set_connected(False)
        # If we were using TCI, swap the audio worker back to PortAudio.
        if isinstance(self._audio_worker, TciInputStreamWorker):
            self._swap_audio_worker(InputStreamWorker())
        # Abort any in-flight TX immediately so audio doesn't continue
        # playing through Mac speakers after the USB device is gone.
        self._tx_worker.request_stop()
        if not self._tx_worker.wait_for_idle(0.0):
            # v0.4.0 audit high #1: a TX is still unwinding, and its
            # _unkey_with_retry is about to close/re-open THIS backend
            # to drop PTT.  Killing rigctld / closing the rig here
            # destroys that unkey path (rigctld dead before attempt 1;
            # serial close racing the retry's open→set_ptt) and can
            # leave the radio keyed after a recoverable glitch.  Defer:
            # the TX complete/abort handler finishes the teardown once
            # the worker reports idle.
            self._deferred_rig_teardown = old_rig
        else:
            self._kill_rigctld()
            try:
                old_rig.close()
            except Exception:  # noqa: BLE001 — dead port may raise termios.error
                pass
        self.statusBar().showMessage(
            "Radio disconnected — check USB connection", 8000
        )

    def _finish_deferred_rig_teardown(self) -> None:
        """Complete a rig teardown deferred by ``_on_radio_disconnected``.

        Called from the TX completion/abort handlers (which fire after
        ``_run_tx`` has fully unwound, unkey retries included) and from
        ``closeEvent`` after the worker threads join — whichever comes
        first.  No-op when nothing was deferred.
        """
        old_rig = self._deferred_rig_teardown
        if old_rig is None:
            return
        self._deferred_rig_teardown = None
        self._kill_rigctld()
        try:
            old_rig.close()
        except Exception:  # noqa: BLE001 — dead port may raise termios.error
            pass

    def _kill_rigctld(self) -> None:
        """Terminate any rigctld process we spawned.

        Defensive against a process that already exited on its own
        (e.g. rigctld rejected its CLI args and quit) — ``terminate()``
        / ``wait()`` / ``kill()`` can raise ``ProcessLookupError`` (POSIX)
        or generic ``OSError`` in that case.  We always clear
        ``_rigctld_proc`` so the next launch attempt starts fresh
        regardless of how the cleanup went (OP-19).
        """
        if self._rigctld_proc is None:
            return
        try:
            self._rigctld_proc.terminate()
            try:
                self._rigctld_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._rigctld_proc.kill()
                    # v0.4.0 audit low #14: reap after kill().  Without
                    # this wait the child lingers as a zombie and a
                    # respawned rigctld can race the dying one for the
                    # serial port and TCP listen port.
                    self._rigctld_proc.wait(timeout=2)
                except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                    pass
        except (ProcessLookupError, OSError):
            # Already gone — nothing to do.
            pass
        finally:
            self._rigctld_proc = None

    @Slot(int, str, int)
    def _on_poll_result(self, freq: int, mode_name: str, strength: int) -> None:
        """Receive a successful rig poll result from ``_RigPollWorker``.

        Called via queued connection from the poll worker thread; runs
        on the GUI thread. ``poll_error`` from the worker connects directly
        to ``_radio_panel.set_connection_error``.
        """
        # v0.4: cache for the logbook frequency snapshot (0 = ManualRig
        # placeholder, not a real reading).
        self._last_rig_freq_hz = freq if freq > 0 else None
        self._radio_panel.update_rig_status(freq, mode_name, strength)

    @Slot(int, str, int)
    def _on_tune_requested(self, freq_hz: int, mode: str, passband_hz: int) -> None:
        """Forward a band-plan tune request to the rig-poll thread.

        Shows a brief status-bar message on the GUI thread, then relays the
        command via ``_request_tune`` (a queued cross-thread signal) so the
        actual CAT write runs on the rig-poll thread alongside ``poll()``.

        The band-plan entry's *mode* is a plain ``"USB"``/``"LSB"``/``"FM"``
        literal; it's resolved through ``resolve_tune_mode`` against the
        user's "SSTV mode" policy (Settings → Radio → Direct Serial) before
        being sent, so a "data" policy asks the rig for its data-mode variant
        (e.g. Yaesu ``DATA-U``/``DATA-L``) instead of forcing plain USB/LSB.
        Only applies to Direct Serial connections — the policy is keyed by
        ``rig_serial_protocol``, which other connection modes don't use.
        """
        if freq_hz >= 1_000_000:
            freq_str = f"{freq_hz / 1_000_000:.3f} MHz"
        else:
            freq_str = f"{freq_hz / 1_000:.3f} kHz"
        if self._config.rig_connection_mode == RigConnectionMode.SERIAL:
            mode = resolve_tune_mode(
                mode, self._config.rig_serial_protocol, self._config.rig_tune_mode_policy
            )
        self.statusBar().showMessage(f"Tuning to {freq_str} ({mode or 'mode unchanged'})…", 3000)
        self._request_tune.emit(freq_hz, mode, passband_hz)

    @Slot(str)
    def _on_tune_failed(self, reason: str) -> None:
        """Show a band-plan tune failure on the GUI thread.

        Connected to ``_RigPollWorker.tune_failed`` (cross-thread queued
        connection). Replaces the transient "Tuning to…" message — without
        this, a rejected frequency/mode change looked identical to success.
        """
        self.statusBar().showMessage(f"Tune failed: {reason}", 6000)

    # === lifecycle ===

    def _abort_offline_workers(self) -> None:
        """Drain any in-flight offline encode/decode threads before shutdown.

        Both offline worker QThreads are parented to MainWindow
        (``QThread(self)``), so if either is still running when the
        window's destructor walks its child list,
        ``QObjectPrivate::deleteChildren`` invokes ``~QThread()`` on a
        live thread and Qt aborts the process with
        ``QThread: Destroyed while thread is still running``.  The
        same can happen at Python interpreter shutdown via
        ``PySide::destroyQCoreApplication`` if any deferred-delete
        events for a recently-finished thread haven't been processed
        yet.

        Three-stage drain:

        1. ``thread.quit()`` to ask the worker thread's event loop to
           exit at the next opportunity.  No-op if the worker is
           mid-``encode()`` because the event loop is blocked on
           ``run()`` returning.
        2. ``thread.wait(timeout)`` — block the GUI thread for up to
           10 s to let an in-flight encode complete.  Covers Robot
           36 / PD / Wraase / Scottie / Martin / Pasokon P3-P5.  A
           Pasokon P7 mid-encode may exceed this; we fall through.
        3. ``thread.terminate() + wait(1000)`` as a last resort.  Qt
           docs warn that ``terminate`` can leave the worker in a
           half-deinit state, but a half-deinit worker on a process
           about to ``exit()`` anyway is preferable to ``qFatal``.

        Same shape as the ``_abort_connect`` shutdown drain for the
        ``_RigConnectWorker``.  Safe to call when no worker is in
        flight (refs are already ``None``).
        """
        for attr_thread, attr_worker in (
            ("_offline_encode_thread", "_offline_encode_worker"),
            ("_offline_decode_thread", "_offline_decode_worker"),
        ):
            thread = getattr(self, attr_thread, None)
            if thread is None:
                continue
            try:
                thread.quit()
                if not thread.wait(10_000):
                    # Stage 3: force-terminate.  We prefer a slightly
                    # ugly process exit over a qFatal abort.
                    _log.warning(
                        "%s did not exit cleanly in 10 s; terminating",
                        attr_thread,
                    )
                    thread.terminate()
                    thread.wait(1000)
            except RuntimeError:
                # Thread C++ object already destroyed (e.g. closeEvent
                # firing twice via aboutToQuit + the X button).
                pass
            setattr(self, attr_thread, None)
            setattr(self, attr_worker, None)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt API
        # closeEvent fires twice in ordinary use: once for the window's own
        # close, once from ``app.aboutToQuit`` (wired in app.py).  The first
        # pass stops the worker threads and deletes the worker QObjects, so
        # the second reaches freed C++ objects.  Individual statements below
        # carry ``except RuntimeError`` for this, but they cannot cover
        # everything — the disconnect loop resolves ``self._tx_worker`` while
        # building its iterable, outside the guard — and the error escaped as
        # a SystemError out of the Qt virtual override.  Nothing remains to
        # do on a second pass, so return before touching any of it.
        if self._teardown_complete:
            event.accept()
            return

        # Audit #3: flag first.  The shutdown drain below can deliver
        # one final queued image_complete (stop-flush, RX watchdog);
        # the capture flow checks this and stands down instead of
        # opening dialogs / re-opening the logbook store mid-teardown.
        self._closing = True

        # v0.6 (Phase 3c): reclaim control first — unkeys any in-flight
        # remote TX and drops the lease — then stop the read-only server.
        # Both are independent of the rig/worker teardown below.
        self._remote_control.reclaim_local()
        self._stop_remote_server()

        # Abort any in-flight rig connect first — the QThread is a child of
        # this window and Qt calls fatal() if it is still running when the
        # parent is destroyed (OP2-02 regression fix).
        self._abort_connect()
        # Same reason for the offline encode/decode worker threads — they're
        # parented to MainWindow too, so a window-close mid-encode would
        # fatal() on ~QThread().  (v0.3.10 regression.)
        self._abort_offline_workers()

        # Stop rig polling first to avoid timer fires during teardown.
        self._rig_poll_timer.stop()

        # Disconnect inbound user signals first so a click that races
        # with shutdown can't queue fresh work onto a thread we're about
        # to tear down. Qt raises RuntimeError / TypeError if the
        # connection is already gone (e.g. closeEvent firing twice via
        # aboutToQuit + the X button), so we swallow both.
        for signal, slot in (
            (self._request_transmit, self._tx_worker.transmit),
            (self._request_test_tone, self._tx_worker.transmit_test_tone),
            (self._audio_worker.chunk_ready, self._rx_worker.feed_chunk),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

        # Abort any in-flight TX before tearing down the worker thread.
        # request_stop is explicitly thread-safe (threading.Event +
        # sounddevice.stop), so calling it from the UI thread is fine.
        self._tx_worker.request_stop()
        self.statusBar().showMessage("Closing…")
        # Give the TX worker up to 1 s to unwind out of play_blocking
        # (chunked-write path checks stop_event only between 0.1 s chunks).
        # thread.wait(3000) below would handle it anyway, but waiting here
        # first makes the shutdown ordering explicit and avoids the edge case
        # where the thread.quit() drains queued events before the worker exits.
        # v0.4.0 audit high #5: this must wait for the worker to be IDLE
        # (fully unwound, unkey done) — the old wait_for_stop waited on
        # the stop flag request_stop had just set, returning instantly.
        self._tx_worker.wait_for_idle(timeout=1.0)

        # Stop RX audio capture via the queued signal so the actual
        # PortAudio/QTimer teardown runs on the audio worker thread
        # (touching a QTimer across thread affinity is illegal and
        # raises warnings).
        #
        # M2: previously the stop emission was followed immediately by
        # ``_audio_thread.quit()`` later in this method; if quit posted
        # before the worker had drained the stop event, the thread's
        # event loop could exit without running stop() — leaving the
        # TCI ``audio_stop:0;`` unsent and the server still subscribed
        # when ``self._rig.close()`` ran below.  Mirror the H8 pattern:
        # wait for the worker's ``stopped`` signal with a bounded event
        # loop before letting the thread quit.
        _stop_done = threading.Event()

        def _on_close_stop_done() -> None:
            _stop_done.set()

        try:
            self._audio_worker.stopped.connect(  # type: ignore[union-attr]
                _on_close_stop_done, Qt.ConnectionType.QueuedConnection
            )
        except Exception:  # noqa: BLE001
            pass
        self._request_stop_capture.emit()

        _close_loop = QEventLoop()
        _close_timer = QTimer()
        _close_timer.setSingleShot(True)
        _close_timer.timeout.connect(_close_loop.quit)
        try:
            self._audio_worker.stopped.connect(  # type: ignore[union-attr]
                _close_loop.quit, Qt.ConnectionType.QueuedConnection
            )
        except Exception:  # noqa: BLE001
            pass
        _close_timer.start(2000)
        if not _stop_done.is_set():
            _close_loop.exec()
        _close_timer.stop()
        for _slot in (_on_close_stop_done, _close_loop.quit):
            try:
                self._audio_worker.stopped.disconnect(_slot)  # type: ignore[union-attr]
            except (RuntimeError, TypeError):
                pass
        if not _stop_done.is_set():
            # Name the backend: this fires on every quit for some
            # Windows/MME setups, and the old message blamed TCI even for
            # PortAudio users, which sent at least one bug report down the
            # wrong path.  input_stream.stop() now logs which blocking call
            # ran long, so the two lines together identify the culprit.
            _backend = type(self._audio_worker).__name__
            _log.warning(
                "closeEvent: audio worker stop() did not complete in 2 s "
                "(worker=%s) — proceeding with shutdown anyway. See the "
                "'input stream teardown slow' line above for which PortAudio "
                "call blocked; on TCI this may mean audio_stop never reached "
                "the server.",
                _backend,
            )
        # Same reasoning for RxWorker's wall-clock watchdog QTimer —
        # it lives on the RX decode thread (created lazily in
        # ``_ensure_watchdog_timer``) and has no implicit stop path.
        # The queued shutdown slot runs before the event loop quits
        # below; without it, the timer is still active on worker-
        # thread destruction and the GUI-thread destructor emits
        # "QObject::killTimer: Timers cannot be stopped from another
        # thread".
        self._request_rx_shutdown.emit()

        self._tx_thread.quit()
        if not self._tx_thread.wait(3000):
            import logging as _logging
            import threading as _threading
            _logging.getLogger(__name__).warning(
                "TX worker thread did not finish within timeout — "
                "attempting emergency PTT unkey"
            )
            # Run emergency_unkey in a daemon thread with a join so a
            # dead-rig serial timeout can't freeze the GUI for the full
            # timeout while we're trying to quit (OP-08).  The thread is
            # daemon=True so even if the unkey itself hangs, the
            # interpreter exits cleanly.
            #
            # L8: bumped from 1.5 s to 3.0 s — the old budget was sized
            # to match the serial backend's own write timeout (1.0 s)
            # plus margin, but a USB-CAT chain can stack several layers
            # of timeout (driver, kernel, application) and 1.5 s wasn't
            # enough for the unkey to complete on slow USB hubs.  3 s
            # still feels snappy at app close, gives the unkey a real
            # chance to finish, and is well below the OS-imposed kill
            # latency Qt enforces on the main thread.
            t = _threading.Thread(
                target=self._tx_worker.emergency_unkey,
                name="sstv-app-emergency-unkey",
                daemon=True,
            )
            t.start()
            t.join(timeout=3.0)
            # v0.4.0 audit high #4: if the TX thread is STILL running
            # after the emergency unkey, detach it from this window —
            # it is a QThread(self) child, and ~QThread on a live
            # thread is a qFatal process abort at window destruction.
            # A leaked thread at exit beats an abort (same policy the
            # offline-worker drain documents).
            if not self._tx_thread.wait(500):
                _logging.getLogger(__name__).warning(
                    "TX worker thread still running at close — detaching "
                    "from the window to avoid QThread destruction abort"
                )
                self._tx_thread.setParent(None)

        for thread in (
            self._audio_thread,
            self._rx_thread,
            self._rig_poll_thread,
            self._update_thread,
        ):
            thread.quit()
            if not thread.wait(4000):
                # v0.4.0 audit high #4: same detach-over-abort policy as
                # the TX thread above — a wedged Core Audio stop() or a
                # long P7 decode must not turn quit into a qFatal.
                import logging as _logging2
                _logging2.getLogger(__name__).warning(
                    "%s did not stop within 4 s at close — detaching from "
                    "the window to avoid QThread destruction abort",
                    thread.objectName() or "worker thread",
                )
                thread.setParent(None)

        # v0.4 (audit #3): close the logbook's SQLite connection only
        # now — every worker thread that could emit a completion has
        # been joined, so nothing can lazily re-open the store after
        # this point.  The logbook/capture dialogs are children of
        # this window; Qt tears them down.
        try:
            self._logbook_coordinator.close()
        except Exception:  # noqa: BLE001 — never block shutdown on the logbook
            pass

        # v0.5: best-effort thumbnail-cache housekeeping on the way out.
        if self._gallery_dialog is not None:
            try:
                self._gallery_dialog.prune_cache()
            except Exception:  # noqa: BLE001 — never block shutdown on the gallery
                pass

        # v0.4.1 (audit high #1): if a mid-TX disconnect deferred its
        # backend teardown and the app is quitting before the TX
        # completion signal was processed, finish it here — the TX
        # thread has joined (or been detached), so the unkey path is
        # no longer using the old rig.
        self._finish_deferred_rig_teardown()

        try:
            self._rig.close()
        except Exception:  # noqa: BLE001
            # Closing should never throw to the user — they're already
            # quitting.  Catch Exception (not just RigError): a dead USB
            # port raises raw OSError/termios.error, and an exception
            # escaping a Qt virtual override aborts shutdown midway,
            # leaving the app hung and the rigctld child orphaned.
            _log.warning("closeEvent: rig.close() failed", exc_info=True)
        self._kill_rigctld()
        # Teardown really did run to completion — only now is a second pass
        # safe to skip.
        self._teardown_complete = True
        super().closeEvent(event)


__all__ = ["MainWindow"]
