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

    # --- Logging (v0.4) ---
    # Root log level for both the stderr and rotating-file handlers.
    # Applied at startup by ``app._setup_logging``; changing it in
    # Settings takes effect on next launch.  ``OPEN_SSTV_DEBUG=1``
    # still forces DEBUG regardless of this field.
    log_level: str = "INFO"

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
            _log.info(
                "AppConfig: cw_id_wpm %d out of range [15, 30] — clamped to %d",
                self.cw_id_wpm,
                clamped_wpm,
            )
        self.cw_id_wpm = clamped_wpm
        clamped_tone = max(400, min(1200, self.cw_id_tone_hz))
        if clamped_tone != self.cw_id_tone_hz:
            _log.info(
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


__all__ = ["AppConfig"]
