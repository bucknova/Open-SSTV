# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings schema (plain dataclasses, no Pydantic).

``AppConfig`` holds every user-facing setting. The TOML store loads it
from disk (filling missing keys with the dataclass defaults) and writes
it back when the user clicks "Save" in the settings dialog.

The config is intentionally a *value object* with no Qt dependency — it
can be constructed and round-tripped in headless tests without importing
PySide6.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

from open_sstv.radio.base import RigConnectionMode

_log = logging.getLogger(__name__)

#: Canonical serial baud rates the app supports.  The Settings dialog's
#: baud combo imports this list so schema validation and the UI can't
#: drift (M1, v0.3 audit).  Serial rates must match the rig exactly —
#: clamping a bad value would still fail, so unknown rates fall back to
#: the 9600 default instead.
VALID_BAUD_RATES: tuple[int, ...] = (4800, 9600, 19200, 38400, 57600, 115200)

#: Valid values for ``tx_banner_size`` — mirrors ``banner.SIZE_TABLE``.
_VALID_BANNER_SIZES: tuple[str, ...] = ("small", "medium", "large")

#: Valid values for ``rig_ptt_line`` (SerialPttRig control lines).
_VALID_PTT_LINES: tuple[str, ...] = ("DTR", "RTS")

#: Valid values for ``rig_tune_mode_policy`` — how Band Plan tuning picks
#: the CAT mode to send, mirroring WSJT-X's rig "Mode" setting:
#: "none" leaves the rig's mode alone, "voice" sends the band-plan
#: entry's plain USB/LSB/FM literal (today's behavior, and the default so
#: upgrades don't change anyone's setup), "data" resolves the protocol's
#: data-mode variant (e.g. Yaesu DATA-U/DATA-L) via
#: ``radio.band_plan.resolve_tune_mode``.
_VALID_TUNE_MODE_POLICIES: tuple[str, ...] = ("none", "voice", "data")


def _default_images_dir() -> str:
    """XDG-correct pictures directory, e.g. ``~/Pictures/open_sstv``."""
    base = Path(platformdirs.user_pictures_dir())
    return str(base / "open_sstv")


@dataclass
class AppConfig:
    """Top-level application config, one field per user-facing setting.

    Every field has a sensible default so ``AppConfig()`` is valid on a
    fresh install with no TOML file on disk.
    """

    # --- Audio ---
    audio_input_device: str | None = None
    audio_output_device: str | None = None
    sample_rate: int = 48_000

    # --- TX ---
    default_tx_mode: str = "martin_m1"
    # v0.3.17 (audit L-2): two-tone test signal frequencies, Hz.  Defaults
    # are the ARRL twin-tone standard (700 / 1900) used for sideband
    # linearity testing.  Settings → Audio exposes these as spinboxes so
    # operators on narrower passbands (CW filters, BPSK-tuned IFs, etc.)
    # can move them inside the audible band without code changes.
    # ``__post_init__`` clamps to [300, 3000] Hz to keep them within any
    # reasonable SSB passband.
    test_tone_freq_lo: float = 700.0
    test_tone_freq_hi: float = 1900.0

    # --- Radio ---
    # Connection mode: one of ``RigConnectionMode.MANUAL`` / ``.SERIAL`` /
    # ``.RIGCTLD`` (string values "manual" / "serial" / "rigctld"; kept
    # as ``str`` on the dataclass for TOML forward-compat).  OP-28 in
    # v0.1.29 centralised these literals into the enum so schema,
    # settings dialog, and main-window dispatch no longer drift.
    rig_connection_mode: str = RigConnectionMode.MANUAL.value
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    tci_host: str = "127.0.0.1"
    tci_port: int = 40001
    #: FlexRadio direct control (SmartSDR TCP API).  Host is the radio's
    #: own IP — not localhost — because we talk to the radio, not to a
    #: daemon on this machine.  ``flex_slice`` picks which receiver to
    #: follow (0 = slice A).
    flex_host: str = ""
    flex_port: int = 4992
    flex_slice: int = 0
    ptt_delay_s: float = 0.2
    rig_model_id: int = 0
    rig_serial_port: str = ""
    rig_baud_rate: int = 9600
    auto_launch_rigctld: bool = False

    # --- Direct serial rig control ---
    # Protocol: "PTT Only (DTR/RTS)", "Icom CI-V", "Kenwood / Elecraft", "Yaesu CAT"
    rig_serial_protocol: str = "PTT Only (DTR/RTS)"
    rig_civ_address: int = 0x94
    rig_ptt_line: str = "DTR"
    #: Band-plan tuning mode policy: "none" / "voice" / "data" — see
    #: ``_VALID_TUNE_MODE_POLICIES`` and ``radio.band_plan.resolve_tune_mode``.
    rig_tune_mode_policy: str = "voice"

    # --- Audio gain ---
    audio_input_gain: float = 1.0
    audio_output_gain: float = 1.0
    # v0.1.13: overdrive unlocks the TX output gain slider ceiling from 100%
    # to 200%. Off by default — the typical USB-audio rig only needs ~10-15%.
    tx_output_overdrive: bool = False
    # v0.1.13: relaxes VIS leader presence (0.40 → 0.25) and start-bit
    # minimum duration (20 ms → 15 ms) for weak/fading signal conditions.
    rx_weak_signal_mode: bool = False
    # v0.3.7: how many seconds without a new decoded line before the RX
    # watchdog abandons an in-progress decode and keeps the partial image.
    # The total-elapsed guard (mode duration × 1.5, min 15 s) is separate
    # and fires regardless.  10 s is the default; raise in Settings for
    # propagation conditions with longer, deeper QSB fading cycles.
    rx_watchdog_timeout_s: int = 5
    # v0.1.18: when True, the completed image is re-decoded in a single pass
    # with slant correction (np.polyfit across all sync candidates). Off by
    # default because polyfit has no outlier rejection — on weak/marginal
    # signals the least-squares fit is corrupted by false-positive sync
    # detections, producing an image worse than the progressive decode.
    # Opt-in for clean, timing-drifted signals only.
    apply_final_slant_correction: bool = False

    # --- TX banner ---
    # v0.1.19: when True, a thin identification strip is stamped across the
    # top of every transmitted image (not test tone). The strip shows
    # "Open-SSTV v{version}" centred and the callsign flush-right.
    tx_banner_enabled: bool = False
    tx_banner_bg_color: str = "#202020"
    tx_banner_text_color: str = "#FFFFFF"
    # v0.1.22: "small" (24 px / 18 pt, default), "medium" (32 px / 24 pt),
    # "large" (40 px / 30 pt). Unknown values fall back to "small".
    # (Pre-v0.1.22 default was "medium" with a 14/20/26 pt scale; all three
    # font sizes were bumped +4 pt in v0.1.22 so the new "small" preset has
    # a fuller fill ratio than the old one did.)
    tx_banner_size: str = "small"

    # --- CW station ID ---
    # v0.1.14: appended after every SSTV TX (not test tone). Uses the
    # callsign field below. Skipped with a warning if callsign is empty.
    cw_id_enabled: bool = True
    cw_id_wpm: int = 20     # valid range 15–30
    cw_id_tone_hz: int = 800  # valid range 400–1200

    # --- Receive decoder ---
    # v0.1.24: per-line incremental decoder promoted to default.  Covers all
    # 22 supported modes (Scottie, Martin, PD, Wraase SC2, Pasokon, Robot 36).
    # Set to False to fall back to the legacy batch decoder.
    # Old config key "experimental_incremental_decode" is migrated in store.py.
    incremental_decode: bool = True

    # --- UI ---
    show_waterfall: bool = False

    # --- Update checker ---
    # v0.2.16: when True, a background HTTPS GET to the GitHub releases API
    # runs once at startup. No data is sent — it is a read-only request.
    check_for_updates: bool = True

    # --- Identity ---
    callsign: str = ""
    # v0.3.4: persistent operator-info defaults.  Empty by default so
    # existing configs roundtrip unchanged; the first-launch dialog and
    # the General settings tab let the user fill them in.  Template
    # tokens (``{name}`` / ``%n``, ``{grid}`` / ``%g``, ``{qth}``) read
    # from these fields when QSO state has nothing to say.
    operator_name: str = ""
    grid_square: str = ""
    qth: str = ""
    # v0.2.7: one-shot flag for the welcome-callsign dialog.  False on a
    # truly fresh install (no config file on disk); True for any user
    # upgrading from ≤ v0.2.6 (see ``store.load_config`` — the migration
    # auto-grandfathers anyone who already has a config file).  The
    # dialog flips this to True whether the user saves their callsign
    # or clicks *Skip*, so we never nag on subsequent launches.
    first_launch_seen: bool = False

    # --- Directories ---
    images_save_dir: str = field(default_factory=_default_images_dir)
    auto_save: bool = False
    # v0.3.6: save the raw received audio alongside every decoded image.
    # Lets the operator re-decode later (e.g. with the CLI) if the live
    # incremental decoder missed something.  Off by default; uses the same
    # save directory and filename template as image auto-save.
    autosave_rx_audio: bool = False
    # v0.3.6: container format for saved RX audio.  "wav" uses the stdlib
    # wave module (16-bit PCM, no extra deps).  "flac" uses soundfile
    # (lossless compression, ~40% smaller files, requires libsndfile).
    # Both are lossless — lossy formats (MP3, AAC) are deliberately excluded
    # because compression artefacts degrade re-decode quality.
    rx_audio_format: str = "wav"
    # v0.2.8: TX auto-save is independent of RX.  Some operators want to
    # keep a log of every image they transmitted (for station-portfolio
    # or contest purposes); others don't.  Default off so upgraders'
    # behaviour is unchanged — RX auto-save continues to follow
    # ``auto_save`` above.
    autosave_tx: bool = False
    # v0.2.8: filename template shared by RX and TX auto-save.  See
    # ``open_sstv.templates.tokens`` for the token vocabulary.  Default
    # ``%d_%t_%m`` resolves to e.g. ``2026-04-17_213512_Scottie-S1.png``
    # — filename-sortable, unambiguous across time zones (UTC), and
    # filename-safe on all three target platforms.  Existing users
    # upgrading from ≤ v0.2.7 were on ``sstv_{mode}_{YYYYMMDD_HHMMSS}``;
    # the new default is a light cosmetic change and still clearly
    # identifies the file as an SSTV decode.
    autosave_filename_pattern: str = "%d_%t_%m"
    # v0.2.8: output format for auto-saved images.  PNG preserves every
    # decoded pixel losslessly and is the right default for archival;
    # JPG is offered for operators who receive high volumes and want
    # smaller files.  Constrained by the Settings UI to "png" or "jpg".
    autosave_file_format: str = "png"

    # v0.5: extra folders the image Gallery scans in addition to
    # ``images_save_dir``.  Advanced / no UI in v0.5 (TOML-only, mirrors
    # ``logbook_db_path``) — for operators who keep received images in
    # more than one place.  ``__post_init__`` coerces a hand-edited
    # non-list value back to an empty list so a bad TOML can't crash the
    # gallery scan.
    gallery_extra_dirs: list[str] = field(default_factory=list)

    # --- Logbook (v0.4) ---
    # When True, draft QSOs are written silently at TX/RX completion and
    # edited later from the Logbook window.  Default False → a modal
    # LogQsoDialog opens at completion so the contact is captured while
    # it's fresh (Esc dismisses without writing a row).
    auto_log_qsos: bool = False
    # When does an RX completion offer the log dialog?  SSTV calling
    # frequencies are party lines — most of what a monitoring station
    # decodes is *other people's* exchanges, which don't belong in the
    # logbook.  "always" = dialog after every decode (Esc dismisses);
    # "in_qso" = only while the TX panel's ToCall is filled in (you're
    # working someone); "never" = no dialog — log deliberately via the
    # RX gallery's right-click → Log QSO….  TX completions always
    # offer the dialog (your own transmissions are always yours), and
    # ``auto_log_qsos=True`` overrides this entirely.
    rx_capture_prompt: str = "always"
    # Override for the logbook SQLite file.  Empty → the platform
    # default, ``platformdirs.user_data_dir("open_sstv")/logbook.db``.
    # Kept as ``str`` (not Path) for TOML round-trip, matching
    # ``images_save_dir``.
    logbook_db_path: str = ""

    # --- Remote web access (Phase 1 — read-only gallery) ---
    # Embedded HTTP server that lets a browser on the LAN *view* decoded
    # images.  Read-only: no compose, no camera, no transmit.  Advanced /
    # TOML-only for now (no Settings UI yet — a Settings → Remote tab
    # arrives with the Phase 2 view plane), mirroring how gallery_extra_dirs
    # and logbook_db_path shipped config-first.  See
    # ``design/remote/architecture.md``.
    remote_enabled: bool = False
    # Bind address.  Defaults to loopback so enabling the server is
    # local-only until the operator deliberately binds a LAN interface
    # (e.g. "0.0.0.0" or the host's LAN IP) — the design's "don't bind
    # 0.0.0.0 blindly" stance, taken to the safe extreme for the spike.
    remote_host: str = "127.0.0.1"
    remote_port: int = 8730
    # Dev access token.  Empty → the server mints a random one at startup
    # and logs the full URL (http://host:port/?token=…).  Set a value to
    # keep a stable URL across restarts.
    remote_token: str = ""
    # Phase 3 (control plane): allow a paired browser to *transmit* an
    # image remotely.  SEPARATE from remote_enabled and default OFF — you
    # can allow remote viewing while forbidding remote transmit.  Even when
    # true, every transmit still requires holding the single-writer lease
    # and a per-transmit confirmation, and a lost heartbeat unkeys the rig
    # (dead-man's-switch).  This keys your transmitter over the network, so
    # it is opt-in and off by default.  See design/remote/architecture.md.
    remote_tx_enabled: bool = False

    # --- Logging (v0.4) ---
    # Root log level for both the stderr and rotating-file handlers.
    # Applied at startup by ``app._setup_logging``; changing it in
    # Settings takes effect on next launch.  ``OPEN_SSTV_DEBUG=1``
    # still forces DEBUG regardless of this field.
    log_level: str = "INFO"

    # --- UDP QSO log (v0.6.7) ---
    # Fire-and-forget UDP broadcast of a single logged contact, for
    # third-party ham-radio logging tools (QLog, JTAlert, GridTracker,
    # Log4OM, N1MM…) — triggered manually from the "External Log" button on
    # the TX panel's QSO-state bar, independent of the local SQLite
    # logbook.  Ported from the ``cwrobot`` sister project.
    udp_log_host: str = "127.0.0.1"
    # Default matches WSJT-X's own UDP logging port — most third-party
    # loggers already listen here out of the box.
    udp_log_port: int = 2237
    # "adif" = bare ADIF record (Log4OM-style); "wsjtx" = WSJT-X's framed
    # binary Network Message protocol (QLog, JTAlert, GridTracker, N1MM).
    udp_log_format: str = "wsjtx"

    def __post_init__(self) -> None:
        # v0.1.12: slider ceiling reverted from 500% to 200%.
        # Clamp any stored value so users who raised it to ≤500% on v0.1.11
        # don't get unexpected clipping on next open.
        if self.audio_output_gain > 2.0:
            self.audio_output_gain = 2.0
        # v0.1.13: default slider ceiling is now 100%.  If an existing config
        # has a value above 100% but overdrive was never persisted (missing
        # field, old config file), auto-enable overdrive so the user's
        # calibrated gain is preserved rather than being silently clamped on
        # the next Settings open.
        if self.audio_output_gain > 1.0 and not self.tx_output_overdrive:
            self.tx_output_overdrive = True
            _log.info(
                "AppConfig: audio_output_gain %.0f%% > 100%% — "
                "overdrive auto-enabled (migrated from pre-v0.1.13 config).",
                self.audio_output_gain * 100,
            )
        # v0.1.14: clamp CW fields to their valid ranges so hand-edited
        # TOML files can't push WPM or tone outside what the UI allows.
        # OP2-08: log the clamp so a user who hand-edits TOML understands
        # why their value was silently overridden on next save.
        clamped_wpm = max(15, min(30, self.cw_id_wpm))
        if clamped_wpm != self.cw_id_wpm:
            _log.warning(
                "AppConfig: cw_id_wpm %d out of range [15, 30] — clamped to %d",
                self.cw_id_wpm,
                clamped_wpm,
            )
        self.cw_id_wpm = clamped_wpm
        clamped_tone = max(400, min(1200, self.cw_id_tone_hz))
        if clamped_tone != self.cw_id_tone_hz:
            _log.warning(
                "AppConfig: cw_id_tone_hz %d out of range [400, 1200] — clamped to %d",
                self.cw_id_tone_hz,
                clamped_tone,
            )
        self.cw_id_tone_hz = clamped_tone
        # v0.2.8: normalise the auto-save file format to lowercase and
        # fall back to "png" for unknown values so a hand-edited TOML
        # can't put us into a state where the filename builder silently
        # produces files that no viewer can open.
        original_fmt = self.autosave_file_format
        fmt = (self.autosave_file_format or "").lower().lstrip(".")
        if fmt not in ("png", "jpg", "jpeg"):
            # M3: log so a user who hand-edited TOML to "webp" / future
            # formats sees why their preference was discarded.  Without
            # this the coercion silently rewrote the value on every
            # load→save cycle.
            if original_fmt and fmt:
                _log.warning(
                    "AppConfig: unknown autosave_file_format %r — falling back to 'png'",
                    original_fmt,
                )
            fmt = "png"
        self.autosave_file_format = "jpg" if fmt == "jpeg" else fmt

        # M3: same symmetry for rx_audio_format — previously had no
        # validation at all in __post_init__; the Settings combo
        # silently selected index 0 ("wav") for unknown values and
        # result_config() wrote that back, dropping user preferences
        # without trace.
        rx_fmt_original = self.rx_audio_format
        rx_fmt = (self.rx_audio_format or "").lower().lstrip(".")
        if rx_fmt not in ("wav", "flac"):
            if rx_fmt_original and rx_fmt:
                _log.warning(
                    "AppConfig: unknown rx_audio_format %r — falling back to 'wav'",
                    rx_fmt_original,
                )
            rx_fmt = "wav"
        self.rx_audio_format = rx_fmt

        # v0.3.7: clamp rx_watchdog_timeout_s to the spinbox range [5, 300]
        # so a hand-edited TOML can't set an absurd value (0, negative, or
        # thousands of seconds).
        clamped_wdt = max(5, min(300, self.rx_watchdog_timeout_s))
        if clamped_wdt != self.rx_watchdog_timeout_s:
            _log.info(
                "AppConfig: rx_watchdog_timeout_s %d out of range [5, 300]"
                " — clamped to %d",
                self.rx_watchdog_timeout_s,
                clamped_wdt,
            )
        self.rx_watchdog_timeout_s = clamped_wdt

        # L-2 (audit 4.7/v0.2.9): clamp two-tone test frequencies to
        # [300, 3000] Hz so a hand-edited TOML can't set values outside
        # any reasonable SSB passband.  Also re-order if the user
        # accidentally sets lo > hi so downstream code never has to
        # worry about it.
        for attr, lo, hi in (
            ("test_tone_freq_lo", 300.0, 3000.0),
            ("test_tone_freq_hi", 300.0, 3000.0),
        ):
            v = float(getattr(self, attr))
            clamped = max(lo, min(hi, v))
            if clamped != v:
                _log.info(
                    "AppConfig: %s %.1f out of range [%.0f, %.0f] — clamped to %.1f",
                    attr, v, lo, hi, clamped,
                )
            setattr(self, attr, clamped)
        if self.test_tone_freq_lo > self.test_tone_freq_hi:
            self.test_tone_freq_lo, self.test_tone_freq_hi = (
                self.test_tone_freq_hi, self.test_tone_freq_lo
            )
            _log.info(
                "AppConfig: test_tone_freq_lo > test_tone_freq_hi — swapped"
            )

        # v0.5: a hand-edited TOML could set gallery_extra_dirs to a
        # scalar or a list with non-string entries; coerce to a clean
        # list[str] so the gallery scan never chokes.
        if not isinstance(self.gallery_extra_dirs, list):
            _log.warning(
                "AppConfig: gallery_extra_dirs is not a list (%r) — ignoring",
                self.gallery_extra_dirs,
            )
            self.gallery_extra_dirs = []
        else:
            self.gallery_extra_dirs = [
                str(d) for d in self.gallery_extra_dirs if str(d).strip()
            ]

        # v0.4: normalise rx_capture_prompt; unknown values fall back to
        # "always" (the most conservative mode — never silently drops a
        # capture opportunity).
        rxp_original = self.rx_capture_prompt
        rxp = (self.rx_capture_prompt or "").strip().lower()
        if rxp not in ("always", "in_qso", "never"):
            if rxp_original and rxp:
                _log.warning(
                    "AppConfig: unknown rx_capture_prompt %r — falling back to 'always'",
                    rxp_original,
                )
            rxp = "always"
        self.rx_capture_prompt = rxp

        # v0.4: normalise log_level and fall back to INFO for unknown
        # values so a hand-edited TOML can't silence logging entirely
        # (an invalid level passed to logging would raise at startup).
        lvl_original = self.log_level
        lvl = (self.log_level or "").strip().upper()
        if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            if lvl_original and lvl:
                _log.warning(
                    "AppConfig: unknown log_level %r — falling back to 'INFO'",
                    lvl_original,
                )
            lvl = "INFO"
        self.log_level = lvl

        # v0.6.7: same fallback pattern for the UDP QSO-log wire format —
        # an unrecognised value would otherwise reach UdpQsoLogger and
        # silently send the wrong datagram shape.
        fmt_original = self.udp_log_format
        fmt = (self.udp_log_format or "").strip().lower()
        if fmt not in ("adif", "wsjtx"):
            if fmt_original and fmt:
                _log.warning(
                    "AppConfig: unknown udp_log_format %r — falling back to 'wsjtx'",
                    fmt_original,
                )
            fmt = "wsjtx"
        self.udp_log_format = fmt

        # M1 (v0.3 audit): the remaining hand-editable fields had no
        # validation at all — a bad value loaded fine and then failed
        # later (socket/serial errors at connect time) or silently
        # dispatched to the wrong backend, with nothing pointing back
        # at the config as the root cause.  Same clamp-and-log pattern
        # as the fields above.

        # TCP ports: [1, 65535].
        for attr in (
            "rigctld_port", "tci_port", "remote_port", "flex_port", "udp_log_port",
        ):
            v = int(getattr(self, attr))
            clamped_port = max(1, min(65535, v))
            if clamped_port != v:
                _log.warning(
                    "AppConfig: %s %d out of range [1, 65535] — clamped to %d",
                    attr, v, clamped_port,
                )
            setattr(self, attr, clamped_port)

        # Baud rate must match the rig exactly, so unknown values fall
        # back to the default rather than clamping to a neighbour.
        if self.rig_baud_rate not in VALID_BAUD_RATES:
            _log.warning(
                "AppConfig: rig_baud_rate %d not a supported rate %s — "
                "falling back to 9600",
                self.rig_baud_rate, list(VALID_BAUD_RATES),
            )
            self.rig_baud_rate = 9600

        # Input gain mirrors the Settings slider range [0%, 200%] —
        # output gain has been clamped this way since v0.1.12 but input
        # gain was never validated, so a hand-edited 1000.0 silently
        # drove the DSP front-end into clipping.
        clamped_in_gain = max(0.0, min(2.0, float(self.audio_input_gain)))
        if clamped_in_gain != self.audio_input_gain:
            _log.warning(
                "AppConfig: audio_input_gain %.2f out of range [0.0, 2.0] "
                "— clamped to %.2f",
                self.audio_input_gain, clamped_in_gain,
            )
        self.audio_input_gain = clamped_in_gain

        # String-enum fields: unknown values used to mis-dispatch
        # silently (unknown rig_connection_mode fell through to the
        # rigctld branch; unknown serial protocol fell back to PTT-only;
        # unknown TX mode left the mode combo on its first entry).
        # Reset to the documented default and say so.
        valid_conn_modes = tuple(m.value for m in RigConnectionMode)
        if self.rig_connection_mode not in valid_conn_modes:
            _log.warning(
                "AppConfig: unknown rig_connection_mode %r (valid: %s) — "
                "falling back to 'manual'",
                self.rig_connection_mode, list(valid_conn_modes),
            )
            self.rig_connection_mode = RigConnectionMode.MANUAL.value

        # Import here, not at module top: config must stay importable
        # without dragging in the DSP stack (numpy) for headless tools.
        from open_sstv.core.modes import Mode  # noqa: PLC0415
        valid_tx_modes = tuple(m.value for m in Mode)
        if self.default_tx_mode not in valid_tx_modes:
            _log.warning(
                "AppConfig: unknown default_tx_mode %r — falling back to "
                "'martin_m1'",
                self.default_tx_mode,
            )
            self.default_tx_mode = "martin_m1"

        from open_sstv.radio.serial_rig import SERIAL_RIG_PROTOCOLS  # noqa: PLC0415
        if self.rig_serial_protocol not in SERIAL_RIG_PROTOCOLS:
            _log.warning(
                "AppConfig: unknown rig_serial_protocol %r (valid: %s) — "
                "falling back to 'PTT Only (DTR/RTS)'",
                self.rig_serial_protocol, list(SERIAL_RIG_PROTOCOLS),
            )
            self.rig_serial_protocol = "PTT Only (DTR/RTS)"

        if self.rig_tune_mode_policy not in _VALID_TUNE_MODE_POLICIES:
            _log.warning(
                "AppConfig: unknown rig_tune_mode_policy %r (valid: %s) — "
                "falling back to 'voice'",
                self.rig_tune_mode_policy, list(_VALID_TUNE_MODE_POLICIES),
            )
            self.rig_tune_mode_policy = "voice"

        ptt_line = (self.rig_ptt_line or "").upper()
        if ptt_line not in _VALID_PTT_LINES:
            _log.warning(
                "AppConfig: unknown rig_ptt_line %r (valid: DTR, RTS) — "
                "falling back to 'DTR'",
                self.rig_ptt_line,
            )
            ptt_line = "DTR"
        self.rig_ptt_line = ptt_line

        size = (self.tx_banner_size or "").lower()
        if size not in _VALID_BANNER_SIZES:
            _log.warning(
                "AppConfig: unknown tx_banner_size %r (valid: %s) — "
                "falling back to 'small'",
                self.tx_banner_size, list(_VALID_BANNER_SIZES),
            )
            size = "small"
        self.tx_banner_size = size


__all__ = ["AppConfig", "VALID_BAUD_RATES"]
