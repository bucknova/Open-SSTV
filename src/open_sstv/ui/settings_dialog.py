# SPDX-License-Identifier: GPL-3.0-or-later
"""Modal settings dialog.

Edits an ``AppConfig`` instance from ``open_sstv.config.schema``. On accept,
the caller reads the updated config via ``result_config()`` and persists it.
Lays out fields by section: Audio, Radio, Images.

Uses ``QDialogButtonBox`` with OK/Cancel so the user can back out without
saving. The caller (``MainWindow``) is responsible for calling
``save_config`` and applying any live changes (e.g. toggling rig polling)
after the dialog is accepted.
"""
from __future__ import annotations

import datetime
import logging
import subprocess

import serial.tools.list_ports

_log = logging.getLogger(__name__)

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from open_sstv import __version__ as _APP_VERSION
from open_sstv.audio.devices import (
    AudioDevice,
    list_input_devices,
    list_output_devices,
)
from open_sstv.config.schema import AppConfig
from open_sstv.core.banner import apply_tx_banner, scaled_banner_params
from open_sstv.core.modes import Mode
from open_sstv.radio.base import RigConnectionMode
from open_sstv.radio.exceptions import RigError
from open_sstv.radio.rigctld import RigctldClient, is_safe_rigctld_arg
from open_sstv.radio.serial_rig import (
    ICOM_ADDRESSES,
    SERIAL_RIG_PROTOCOLS,
    create_serial_rig,
)
from open_sstv.templates import TokenContext

#: Common Hamlib radio models (model_id, display_name).
_COMMON_RIG_MODELS: list[tuple[int, str]] = [
    (0, "None / Manual"),
    (1, "Hamlib Dummy"),
    (2, "Hamlib NET rigctl"),
    (1035, "Icom IC-7300"),
    (1036, "Icom IC-7610"),
    (1037, "Icom IC-9700"),
    (1039, "Icom IC-705"),
    (3073, "Kenwood TS-590SG"),
    (3085, "Kenwood TS-890S"),
    (2057, "Yaesu FT-991A"),
    (2055, "Yaesu FT-891"),
    (2063, "Yaesu FTDX10"),
    (2053, "Yaesu FT-710"),
    (2060, "Yaesu FTDX101"),
    (2028, "Yaesu FT-817/818"),
    (4010, "Elecraft K3"),
    (4013, "Elecraft KX3"),
    (4014, "Elecraft KX2"),
    (4015, "Elecraft K4"),
    (1029, "Icom IC-7100"),
    (1034, "Icom IC-7200"),
    (2040, "Yaesu FT-950"),
    (3077, "Kenwood TS-480"),
    (3061, "Kenwood TS-2000"),
]

_BAUD_RATES: list[int] = [4800, 9600, 19200, 38400, 57600, 115200]


class SettingsDialog(QDialog):
    """Modal dialog for editing ``AppConfig``."""

    #: Emitted when the user clicks Test Tone. MainWindow routes this to
    #: ``TxWorker.transmit_test_tone`` via the same queued-signal path as the
    #: Radio panel's Test Tone button.
    test_tone_requested = Signal()

    #: Emitted on every TX output gain slider tick so MainWindow can live-push
    #: the value to TxWorker without waiting for OK. Payload is the gain as a
    #: float multiplier (e.g. 1.5 for 150%). Does NOT write to disk.
    output_gain_changed = Signal(float)

    def __init__(
        self,
        config: AppConfig,
        rig_connected: bool = False,
        tx_image: object = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._config = config
        self._rig_connected = rig_connected
        self._tx_image = tx_image  # PIL Image from the TX panel, or None
        self._tx_active = False

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_audio_tab(), "Audio")
        tabs.addTab(self._build_radio_tab(), "Radio")
        tabs.addTab(self._build_images_tab(), "Images")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # === Tab builders ===

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        # Input device
        self._input_combo = QComboBox()
        self._input_combo.addItem("System default", None)
        self._input_devices: list[AudioDevice] = []
        for dev in list_input_devices():
            label = f"{dev.name} ({dev.host_api})"
            self._input_combo.addItem(label, dev.name)
            self._input_devices.append(dev)
            if dev.name == self._config.audio_input_device:
                self._input_combo.setCurrentIndex(self._input_combo.count() - 1)
        form.addRow("Input device:", self._input_combo)

        # Output device
        self._output_combo = QComboBox()
        self._output_combo.addItem("System default", None)
        self._output_devices: list[AudioDevice] = []
        for dev in list_output_devices():
            label = f"{dev.name} ({dev.host_api})"
            self._output_combo.addItem(label, dev.name)
            self._output_devices.append(dev)
            if dev.name == self._config.audio_output_device:
                self._output_combo.setCurrentIndex(
                    self._output_combo.count() - 1
                )
        form.addRow("Output device:", self._output_combo)

        # Sample rate
        self._sample_rate = QComboBox()
        for rate in (44_100, 48_000):
            self._sample_rate.addItem(f"{rate} Hz", rate)
        idx = self._sample_rate.findData(self._config.sample_rate)
        if idx >= 0:
            self._sample_rate.setCurrentIndex(idx)
        form.addRow("Sample rate:", self._sample_rate)

        # --- Audio gain controls ---
        gain_group = QGroupBox("Software Gain")
        gain_layout = QFormLayout(gain_group)

        # Input gain slider (0–200% in 1% steps)
        in_row = QHBoxLayout()
        self._input_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self._input_gain_slider.setRange(0, 200)
        self._input_gain_slider.setValue(int(self._config.audio_input_gain * 100))
        self._input_gain_label = QLabel(f"{self._config.audio_input_gain * 100:.0f}%")
        self._input_gain_label.setFixedWidth(45)
        self._input_gain_slider.valueChanged.connect(
            lambda v: self._input_gain_label.setText(f"{v}%")
        )
        in_row.addWidget(self._input_gain_slider)
        in_row.addWidget(self._input_gain_label)
        gain_layout.addRow("RX input gain:", in_row)

        # Output gain slider. Default ceiling is 100%; "Enable overdrive"
        # expands it to 200% for rigs that need higher digital drive.
        _overdrive_on = self._config.tx_output_overdrive
        out_row = QHBoxLayout()
        self._output_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self._output_gain_slider.setRange(0, 200 if _overdrive_on else 100)
        self._output_gain_slider.setValue(int(self._config.audio_output_gain * 100))
        self._output_gain_label = QLabel(f"{self._config.audio_output_gain * 100:.0f}%")
        self._output_gain_label.setFixedWidth(45)
        self._output_gain_slider.valueChanged.connect(
            lambda v: self._output_gain_label.setText(f"{v}%")
        )
        # Live-push the gain to TxWorker on every tick so slider adjustments
        # take effect immediately during a Test Tone (without closing the dialog).
        self._output_gain_slider.valueChanged.connect(
            lambda v: self.output_gain_changed.emit(v / 100.0)
        )
        out_row.addWidget(self._output_gain_slider)
        out_row.addWidget(self._output_gain_label)
        _range_str = "0–200%, overdrive" if _overdrive_on else "0–100%"
        self._output_gain_row_label = QLabel(f"TX output gain ({_range_str}):")
        gain_layout.addRow(self._output_gain_row_label, out_row)

        # Overdrive toggle — expands slider ceiling from 100% to 200%.
        self._overdrive_check = QCheckBox("Enable overdrive (up to 200%)")
        self._overdrive_check.setToolTip(
            "Most setups don't need above 100%.\n"
            "Enable only if ALC won't move at max gain."
        )
        self._overdrive_check.setChecked(_overdrive_on)
        self._overdrive_check.toggled.connect(self._on_overdrive_toggled)
        gain_layout.addRow("", self._overdrive_check)

        # Test Tone button — triggers a 5 s calibration transmission.
        # Only enabled when a rig is connected (enforced via _rig_connected /
        # _tx_active flags kept in sync with TxWorker TX lifecycle signals).
        self._test_tone_btn = QPushButton("Test Tone")
        self._test_tone_btn.setToolTip(
            "Transmit a 700 Hz + 1900 Hz two-tone signal for 5 s.\n"
            "Adjust TX output gain above until ALC just barely lights on peaks.\n"
            "The gain slider remains live while the tone plays."
        )
        self._test_tone_btn.clicked.connect(self._on_test_tone_clicked)
        gain_layout.addRow("", self._test_tone_btn)
        self._update_test_tone_btn()

        form.addRow(gain_group)

        # --- Receive options ---
        rx_group = QGroupBox("Receive")
        rx_layout = QFormLayout(rx_group)

        self._weak_signal_check = QCheckBox("Weak-signal mode")
        self._weak_signal_check.setToolTip(
            "Relaxes VIS leader detection threshold (40% → 25%) and minimum\n"
            "start-bit duration (20 ms → 15 ms). Use when a signal is audible\n"
            "in the static but VIS isn't triggering. Trade-off: slightly more\n"
            "false-positive VIS detections, which reset cleanly to IDLE."
        )
        self._weak_signal_check.setChecked(self._config.rx_weak_signal_mode)
        rx_layout.addRow(self._weak_signal_check)

        self._final_slant_check = QCheckBox(
            "Apply slant correction to final image (may worsen weak signals)"
        )
        self._final_slant_check.setToolTip(
            "When enabled, the completed image is re-decoded with slant correction\n"
            "after transmission. Helpful for clean signals with timing drift; can\n"
            "corrupt weak or marginal signals. Off by default."
        )
        self._final_slant_check.setChecked(self._config.apply_final_slant_correction)
        rx_layout.addRow(self._final_slant_check)

        # Per-line incremental decoder — on by default since v0.1.24.
        # Every mode routes through the incremental decoder for roughly
        # O(n) rather than O(n²) CPU cost.  Robot 36 additionally uses
        # the linear-chroma + interpolating upsampler path.
        self._incremental_check = QCheckBox(
            "Per-line incremental decode (all modes)"
        )
        self._incremental_check.setToolTip(
            "Decodes each line as its sync pulse arrives instead of\n"
            "reprocessing the whole buffer on every flush.\n\n"
            "Covers Scottie, Martin, PD, Wraase SC2, Pasokon, and Robot 36\n"
            "(both per-line and line-pair wire formats, auto-detected).\n"
            "Roughly a 50× CPU reduction on long receives, and keeps the\n"
            "decoder ahead of real-time on slower machines where the batch\n"
            "path falls behind mid-image.\n\n"
            "Robot 36 also uses linear (mean) chroma sampling and linear\n"
            "inter-row chroma upsampling — softer chroma edges vs. the\n"
            "batch decoder's median + nearest-neighbor copy.\n\n"
            "On by default.  Uncheck to fall back to the legacy batch\n"
            "decoder if a decode looks wrong."
        )
        self._incremental_check.setChecked(
            self._config.incremental_decode
        )
        rx_layout.addRow(self._incremental_check)

        form.addRow(rx_group)

        return tab

    def _build_radio_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # --- Connection mode selector ---
        mode_group = QGroupBox("Connection Mode")
        mode_form = QFormLayout(mode_group)

        self._conn_mode_combo = QComboBox()
        # OP-28: StrEnum values replace ad-hoc string literals so renames
        # are caught at the enum, not silently via combo→dispatch drift.
        self._conn_mode_combo.addItem(
            "Manual (no rig control)", RigConnectionMode.MANUAL.value,
        )
        self._conn_mode_combo.addItem(
            "Direct Serial (built-in)", RigConnectionMode.SERIAL.value,
        )
        self._conn_mode_combo.addItem(
            "rigctld (Hamlib daemon)", RigConnectionMode.RIGCTLD.value,
        )
        idx = self._conn_mode_combo.findData(self._config.rig_connection_mode)
        if idx >= 0:
            self._conn_mode_combo.setCurrentIndex(idx)
        self._conn_mode_combo.currentIndexChanged.connect(self._on_conn_mode_changed)
        mode_form.addRow("Mode:", self._conn_mode_combo)
        layout.addWidget(mode_group)

        # === Direct Serial group ===
        self._serial_group = QGroupBox("Direct Serial — Built-in Rig Control")
        serial_form = QFormLayout(self._serial_group)

        serial_help = QLabel(
            "Control your radio directly over its serial/USB port. "
            "No external software required."
        )
        serial_help.setWordWrap(True)
        serial_form.addRow(serial_help)

        # Protocol picker
        self._serial_protocol_combo = QComboBox()
        for proto_name in SERIAL_RIG_PROTOCOLS:
            self._serial_protocol_combo.addItem(proto_name)
        idx = self._serial_protocol_combo.findText(self._config.rig_serial_protocol)
        if idx >= 0:
            self._serial_protocol_combo.setCurrentIndex(idx)
        self._serial_protocol_combo.currentIndexChanged.connect(
            self._on_serial_protocol_changed
        )
        serial_form.addRow("Protocol:", self._serial_protocol_combo)

        # Serial port (shared between Direct Serial and rigctld launcher)
        self._serial_port_combo = QComboBox()
        self._serial_port_combo.setEditable(True)
        self._serial_port_combo.addItem("")
        for port_info in sorted(_list_serial_ports(), key=lambda p: p.device):
            self._serial_port_combo.addItem(port_info.device)
        if self._config.rig_serial_port:
            self._serial_port_combo.setCurrentText(self._config.rig_serial_port)
        serial_form.addRow("Serial port:", self._serial_port_combo)

        # Baud rate
        self._baud_rate_combo = QComboBox()
        for rate in _BAUD_RATES:
            self._baud_rate_combo.addItem(str(rate), rate)
        idx = self._baud_rate_combo.findData(self._config.rig_baud_rate)
        if idx >= 0:
            self._baud_rate_combo.setCurrentIndex(idx)
        serial_form.addRow("Baud rate:", self._baud_rate_combo)

        # CI-V address (Icom only)
        self._civ_address_row_label = QLabel("CI-V address:")
        civ_row = QHBoxLayout()
        self._civ_address_spin = QSpinBox()
        self._civ_address_spin.setRange(0, 255)
        self._civ_address_spin.setValue(self._config.rig_civ_address)
        self._civ_address_spin.setDisplayIntegerBase(16)
        self._civ_address_spin.setPrefix("0x")
        civ_row.addWidget(self._civ_address_spin)
        # Quick-pick for common Icom radios
        self._civ_preset_combo = QComboBox()
        self._civ_preset_combo.addItem("Select radio…")
        for radio_name, addr in sorted(ICOM_ADDRESSES.items()):
            self._civ_preset_combo.addItem(f"{radio_name} (0x{addr:02X})", addr)
        self._civ_preset_combo.currentIndexChanged.connect(self._on_civ_preset_changed)
        civ_row.addWidget(self._civ_preset_combo)
        serial_form.addRow(self._civ_address_row_label, civ_row)

        # PTT line selector (PTT-only mode)
        self._ptt_line_row_label = QLabel("PTT line:")
        self._ptt_line_combo = QComboBox()
        self._ptt_line_combo.addItem("DTR", "DTR")
        self._ptt_line_combo.addItem("RTS", "RTS")
        idx = self._ptt_line_combo.findData(self._config.rig_ptt_line)
        if idx >= 0:
            self._ptt_line_combo.setCurrentIndex(idx)
        serial_form.addRow(self._ptt_line_row_label, self._ptt_line_combo)

        # Test button for serial
        self._serial_test_btn = QPushButton("Test Serial Connection")
        self._serial_test_btn.clicked.connect(self._test_serial_connection)
        serial_form.addRow("", self._serial_test_btn)

        self._serial_status = QLabel("")
        serial_form.addRow("", self._serial_status)

        layout.addWidget(self._serial_group)

        # === rigctld group ===
        self._rigctld_group = QGroupBox("rigctld — Hamlib Daemon")
        rigctld_form = QFormLayout(self._rigctld_group)

        rigctld_help = QLabel(
            "Connect to a running <b>rigctld</b> daemon, or let "
            "Open-SSTV launch one for you. Requires Hamlib installed."
        )
        rigctld_help.setWordWrap(True)
        rigctld_help.setTextFormat(Qt.TextFormat.RichText)
        rigctld_form.addRow(rigctld_help)

        self._rigctld_host = QLineEdit(self._config.rigctld_host)
        rigctld_form.addRow("rigctld host:", self._rigctld_host)

        self._rigctld_port = QSpinBox()
        self._rigctld_port.setRange(1, 65535)
        self._rigctld_port.setValue(self._config.rigctld_port)
        rigctld_form.addRow("rigctld port:", self._rigctld_port)

        self._test_btn = QPushButton("Test rigctld Connection")
        self._test_btn.clicked.connect(self._test_connection)
        rigctld_form.addRow("", self._test_btn)

        # Radio model combo (for auto-launching rigctld)
        self._rig_model_combo = QComboBox()
        for model_id, name in _COMMON_RIG_MODELS:
            self._rig_model_combo.addItem(f"{name} ({model_id})", model_id)
        idx = self._rig_model_combo.findData(self._config.rig_model_id)
        if idx >= 0:
            self._rig_model_combo.setCurrentIndex(idx)
        rigctld_form.addRow("Radio model:", self._rig_model_combo)

        self._custom_model_id = QSpinBox()
        self._custom_model_id.setRange(0, 99999)
        self._custom_model_id.setValue(self._config.rig_model_id)
        self._custom_model_id.setToolTip(
            "Enter a Hamlib model number if your radio isn't in the list above."
        )
        rigctld_form.addRow("Custom model ID:", self._custom_model_id)
        self._rig_model_combo.currentIndexChanged.connect(
            lambda _: self._custom_model_id.setValue(
                self._rig_model_combo.currentData()
            )
        )

        # rigctld serial port & baud (for launching rigctld)
        self._rigctld_serial_combo = QComboBox()
        self._rigctld_serial_combo.setEditable(True)
        self._rigctld_serial_combo.addItem("")
        for port_info in sorted(_list_serial_ports(), key=lambda p: p.device):
            self._rigctld_serial_combo.addItem(port_info.device)
        if self._config.rig_serial_port:
            self._rigctld_serial_combo.setCurrentText(self._config.rig_serial_port)
        rigctld_form.addRow("Serial port:", self._rigctld_serial_combo)

        self._rigctld_baud_combo = QComboBox()
        for rate in _BAUD_RATES:
            self._rigctld_baud_combo.addItem(str(rate), rate)
        idx = self._rigctld_baud_combo.findData(self._config.rig_baud_rate)
        if idx >= 0:
            self._rigctld_baud_combo.setCurrentIndex(idx)
        rigctld_form.addRow("Baud rate:", self._rigctld_baud_combo)

        self._auto_launch = QCheckBox("Auto-launch rigctld on Connect")
        self._auto_launch.setChecked(self._config.auto_launch_rigctld)
        rigctld_form.addRow(self._auto_launch)

        btn_row = QHBoxLayout()
        self._launch_btn = QPushButton("Launch rigctld Now")
        self._launch_btn.clicked.connect(self._launch_rigctld)
        btn_row.addWidget(self._launch_btn)
        self._stop_rigctld_btn = QPushButton("Stop rigctld")
        self._stop_rigctld_btn.setEnabled(False)
        self._stop_rigctld_btn.clicked.connect(self._stop_rigctld)
        btn_row.addWidget(self._stop_rigctld_btn)
        rigctld_form.addRow("", btn_row)

        self._rigctld_status = QLabel("")
        rigctld_form.addRow("", self._rigctld_status)

        layout.addWidget(self._rigctld_group)

        # --- PTT and identity (always visible) ---
        misc_group = QGroupBox("PTT / Identity")
        misc_form = QFormLayout(misc_group)

        self._ptt_delay = QDoubleSpinBox()
        self._ptt_delay.setRange(0.0, 2.0)
        self._ptt_delay.setSingleStep(0.05)
        self._ptt_delay.setDecimals(2)
        self._ptt_delay.setSuffix(" s")
        self._ptt_delay.setValue(self._config.ptt_delay_s)
        misc_form.addRow("PTT delay:", self._ptt_delay)

        self._callsign = QLineEdit(self._config.callsign)
        self._callsign.setPlaceholderText("e.g. W0AEZ")
        # Live-update the banner preview when the callsign changes.
        # The preview lives on the Images tab; this connection ensures it
        # stays in sync without the user having to switch tabs first.
        self._callsign.textChanged.connect(self._refresh_banner_preview_if_built)
        misc_form.addRow("Callsign:", self._callsign)

        layout.addWidget(misc_group)

        # --- CW station ID ---
        cw_group = QGroupBox("CW Station ID")
        cw_form = QFormLayout(cw_group)

        cw_help = QLabel(
            "Appends a Morse code station ID after every SSTV transmission "
            "to satisfy regulatory identification requirements."
        )
        cw_help.setWordWrap(True)
        cw_form.addRow(cw_help)

        self._cw_enabled = QCheckBox("Append CW ID after transmissions")
        self._cw_enabled.setChecked(self._config.cw_id_enabled)
        cw_form.addRow(self._cw_enabled)

        self._cw_wpm = QSpinBox()
        self._cw_wpm.setRange(15, 30)
        self._cw_wpm.setValue(self._config.cw_id_wpm)
        self._cw_wpm.setSuffix(" WPM")
        cw_form.addRow("Speed:", self._cw_wpm)

        self._cw_tone_hz = QSpinBox()
        self._cw_tone_hz.setRange(400, 1200)
        self._cw_tone_hz.setValue(self._config.cw_id_tone_hz)
        self._cw_tone_hz.setSingleStep(50)
        self._cw_tone_hz.setSuffix(" Hz")
        cw_form.addRow("Tone:", self._cw_tone_hz)

        # Live-updating callsign label — reflects the Callsign field above
        # so the user knows which callsign will be sent without scrolling.
        _cs_display = self._callsign.text().strip().upper() or "(not set — see Callsign above)"
        self._cw_callsign_label = QLabel(_cs_display)
        self._cw_callsign_label.setStyleSheet("color: gray;")
        self._callsign.textChanged.connect(
            lambda cs: self._cw_callsign_label.setText(
                cs.strip().upper() or "(not set — see Callsign above)"
            )
        )
        cw_form.addRow("Callsign used:", self._cw_callsign_label)

        layout.addWidget(cw_group)
        layout.addStretch()

        # rigctld process handle (managed by this dialog instance)
        self._rigctld_proc: subprocess.Popen | None = None

        # Set initial visibility based on current connection mode.
        # _protocol_init_done gates baud auto-suggest so it only fires on
        # user-initiated protocol changes, not during dialog construction.
        self._protocol_init_done = False
        self._on_conn_mode_changed()
        self._protocol_init_done = True

        return tab

    def _on_conn_mode_changed(self) -> None:
        """Show/hide the serial and rigctld groups based on the selected mode."""
        mode = self._conn_mode_combo.currentData()
        self._serial_group.setVisible(mode == RigConnectionMode.SERIAL)
        self._rigctld_group.setVisible(mode == RigConnectionMode.RIGCTLD)
        if mode == RigConnectionMode.SERIAL:
            self._on_serial_protocol_changed()

    def _on_serial_protocol_changed(self) -> None:
        """Show/hide CI-V address and PTT line based on selected protocol."""
        proto = self._serial_protocol_combo.currentText()
        is_icom = proto == "Icom CI-V"
        is_ptt_only = proto.startswith("PTT Only")
        self._civ_address_row_label.setVisible(is_icom)
        self._civ_address_spin.setVisible(is_icom)
        self._civ_preset_combo.setVisible(is_icom)
        self._ptt_line_row_label.setVisible(is_ptt_only)
        self._ptt_line_combo.setVisible(is_ptt_only)
        # Auto-suggest the typical baud rate for this protocol, but only on
        # user-initiated changes (not during initial dialog construction).
        if getattr(self, "_protocol_init_done", False):
            self._suggest_baud_for_protocol(proto)

    # Typical baud rates for each serial protocol. Exposed as a class-level
    # constant so tests and the "Test Connection" path can reference them.
    _PROTOCOL_DEFAULT_BAUD: dict[str, int] = {
        "PTT Only (DTR/RTS)": 9600,
        "Icom CI-V": 19200,
        "Kenwood / Elecraft": 9600,
        "Yaesu CAT": 38400,
    }

    def _suggest_baud_for_protocol(self, proto: str) -> None:
        """Update the baud rate combo to the protocol's typical default.

        Called only when the user actively changes the protocol selector.
        Silently ignores unknown protocols so future additions don't break.
        """
        suggested = self._PROTOCOL_DEFAULT_BAUD.get(proto)
        if suggested is not None:
            idx = self._baud_rate_combo.findData(suggested)
            if idx >= 0:
                self._baud_rate_combo.setCurrentIndex(idx)

    def _on_civ_preset_changed(self, index: int) -> None:
        """Set the CI-V address spinbox when a preset radio is selected."""
        if index > 0:
            addr = self._civ_preset_combo.currentData()
            if addr is not None:
                self._civ_address_spin.setValue(addr)

    def _test_serial_connection(self) -> None:
        """Try to open and ping via the direct serial backend."""
        proto = self._serial_protocol_combo.currentText()
        port = self._serial_port_combo.currentText().strip()
        baud = self._baud_rate_combo.currentData()

        if not port:
            QMessageBox.warning(
                self, "No serial port",
                "Please select or enter a serial port.",
            )
            return

        try:
            rig = create_serial_rig(
                protocol=proto,
                port=port,
                baud_rate=baud if baud else 9600,
                ci_v_address=self._civ_address_spin.value(),
                ptt_line=self._ptt_line_combo.currentData() or "DTR",
            )
            rig.open()
            rig.ping()
            freq = rig.get_freq()
            mode, _ = rig.get_mode()
            rig.close()

            info_parts = [f"Connected via {proto} on {port}."]
            if freq > 0:
                info_parts.append(f"Frequency: {freq / 1_000_000:.6f} MHz")
            if mode:
                info_parts.append(f"Mode: {mode}")
            QMessageBox.information(
                self, "Connection successful", "\n".join(info_parts),
            )
            self._serial_status.setText("Connection OK")
            self._serial_status.setStyleSheet("color: green;")
        except RigError as exc:
            QMessageBox.warning(
                self, "Connection failed",
                f"Could not connect via {proto} on {port}.\n\n"
                f"Error: {exc}",
            )
            self._serial_status.setText(f"Failed: {exc}")
            self._serial_status.setStyleSheet("color: red;")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "Connection failed",
                f"Unexpected error:\n\n{exc}",
            )
            self._serial_status.setText(f"Failed: {exc}")
            self._serial_status.setStyleSheet("color: red;")

    def _build_images_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        # Default TX mode
        self._tx_mode = QComboBox()
        for mode in Mode:
            self._tx_mode.addItem(mode.value, mode.value)
        idx = self._tx_mode.findData(self._config.default_tx_mode)
        if idx >= 0:
            self._tx_mode.setCurrentIndex(idx)
        # v0.2.8: the autosave preview uses the default TX mode as a
        # realistic placeholder so the user sees how ``%m`` will render.
        self._tx_mode.currentIndexChanged.connect(
            lambda _=None: self._refresh_autosave_preview()
        )
        form.addRow("Default TX mode:", self._tx_mode)

        # Auto-save (RX)
        self._auto_save = QCheckBox("Auto-save received images")
        self._auto_save.setChecked(self._config.auto_save)
        form.addRow(self._auto_save)

        # v0.2.8: TX auto-save — independent of RX so operators can
        # archive a log of everything they transmit without also
        # archiving every RX decode (or vice versa).
        self._autosave_tx = QCheckBox("Auto-save transmitted images")
        self._autosave_tx.setToolTip(
            "When enabled, every successful SSTV transmission (not test tone)\n"
            "is written to the Save directory using the filename template\n"
            "below. The saved image includes any TX banner that was stamped."
        )
        self._autosave_tx.setChecked(self._config.autosave_tx)
        form.addRow(self._autosave_tx)

        # Save directory
        dir_row = QHBoxLayout()
        self._save_dir = QLineEdit(self._config.images_save_dir)
        self._save_dir.setReadOnly(True)
        dir_row.addWidget(self._save_dir)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_save_dir)
        dir_row.addWidget(browse_btn)
        form.addRow("Save directory:", dir_row)

        # v0.2.8: filename template.  Shared by RX + TX auto-save so the
        # operator gets a consistent naming convention across the
        # directory.  See ``open_sstv.templates.tokens`` for the vocabulary.
        self._autosave_pattern = QLineEdit(self._config.autosave_filename_pattern)
        self._autosave_pattern.setToolTip(
            "Tokens:\n"
            "  %d  date (YYYY-MM-DD, UTC)\n"
            "  %t  time (HHMMSS, UTC)\n"
            "  %ts Unix epoch timestamp\n"
            "  %c  your callsign\n"
            "  %m  SSTV mode (e.g. Scottie-S1)\n"
            "  %rx_tx  literal 'RX' or 'TX'\n"
            "  %%  literal percent sign\n"
            "Named aliases ({date}, {time}, {callsign}, …) also work."
        )
        self._autosave_pattern.textChanged.connect(
            self._refresh_autosave_preview
        )
        form.addRow("Filename template:", self._autosave_pattern)

        # File format — drop-down so hand-edited values (or unknown
        # formats) can't silently land users on a non-standard extension.
        self._autosave_format = QComboBox()
        for label, key in (("PNG (lossless)", "png"), ("JPG (smaller)", "jpg")):
            self._autosave_format.addItem(label, key)
        _fmt_idx = self._autosave_format.findData(
            self._config.autosave_file_format
        )
        if _fmt_idx >= 0:
            self._autosave_format.setCurrentIndex(_fmt_idx)
        self._autosave_format.currentIndexChanged.connect(
            self._refresh_autosave_preview
        )
        form.addRow("File format:", self._autosave_format)

        # Live preview — shows the concrete filename the current template
        # + format would produce right now, so the user can verify before
        # accepting the dialog.  Pure-Python, no filesystem touch.
        self._autosave_preview = QLabel()
        self._autosave_preview.setStyleSheet("color: #666; font-family: monospace;")
        self._autosave_preview.setToolTip(
            "Example filename using the current template, format, and\n"
            "your callsign at the current time. Updates live as you type."
        )
        form.addRow("Preview:", self._autosave_preview)
        self._refresh_autosave_preview()

        # --- TX Banner ---
        banner_group = QGroupBox("TX Banner")
        banner_layout = QFormLayout(banner_group)

        self._banner_enabled = QCheckBox(
            "Stamp Open-SSTV banner on transmitted images"
        )
        self._banner_enabled.setToolTip(
            "Adds a thin identification strip across the top of every\n"
            "transmitted image (not the test tone). Shows the app version\n"
            "centred and your callsign flush-right."
        )
        self._banner_enabled.setChecked(self._config.tx_banner_enabled)
        banner_layout.addRow(self._banner_enabled)

        # Background colour swatch button
        self._banner_bg_color: str = self._config.tx_banner_bg_color
        self._banner_bg_btn = QPushButton()
        self._banner_bg_btn.setFixedSize(60, 22)
        self._banner_bg_btn.setToolTip("Click to choose banner background colour")
        self._banner_bg_btn.setStyleSheet(
            f"background-color: {self._banner_bg_color}; border: 1px solid #888;"
        )
        self._banner_bg_btn.clicked.connect(self._pick_banner_bg_color)
        banner_layout.addRow("Background:", self._banner_bg_btn)

        # Text colour swatch button
        self._banner_text_color: str = self._config.tx_banner_text_color
        self._banner_text_btn = QPushButton()
        self._banner_text_btn.setFixedSize(60, 22)
        self._banner_text_btn.setToolTip("Click to choose banner text colour")
        self._banner_text_btn.setStyleSheet(
            f"background-color: {self._banner_text_color}; border: 1px solid #888;"
        )
        self._banner_text_btn.clicked.connect(self._pick_banner_text_color)
        banner_layout.addRow("Text:", self._banner_text_btn)

        # Size selector
        self._banner_size = QComboBox()
        for label, key in [("Small", "small"), ("Medium", "medium"), ("Large", "large")]:
            self._banner_size.addItem(label, key)
        _size_idx = self._banner_size.findData(self._config.tx_banner_size)
        if _size_idx >= 0:
            self._banner_size.setCurrentIndex(_size_idx)
        self._banner_size.currentIndexChanged.connect(self._refresh_banner_preview)
        banner_layout.addRow("Size:", self._banner_size)

        # Live preview — real render via apply_tx_banner() at the chosen size.
        self._banner_preview = QLabel()
        self._banner_preview.setFixedWidth(320)
        self._banner_preview.setToolTip(
            "Live preview of the banner as it will appear on transmitted images."
        )
        banner_layout.addRow("Preview:", self._banner_preview)
        self._refresh_banner_preview()

        # Preview on a real image — uses the TX panel image when one is loaded,
        # otherwise falls back to a file-picker.
        self._banner_preview_on_image_btn = QPushButton("Preview on image…")
        self._banner_preview_on_image_btn.setToolTip(
            "Show the banner applied to the current TX image.\n"
            "If no image is loaded in the TX panel, a file picker opens instead."
        )
        self._banner_preview_on_image_btn.clicked.connect(
            self._preview_banner_on_image
        )
        banner_layout.addRow("", self._banner_preview_on_image_btn)

        form.addRow(banner_group)

        # --- Updates ---
        updates_group = QGroupBox("Updates")
        updates_layout = QFormLayout(updates_group)
        self._check_updates_setting = QCheckBox("Check for updates on startup")
        self._check_updates_setting.setToolTip(
            "On startup, Open-SSTV makes a read-only HTTPS request to\n"
            "github.com/bucknova/Open-SSTV to check for newer releases.\n"
            "No data is sent."
        )
        self._check_updates_setting.setChecked(self._config.check_for_updates)
        updates_layout.addRow(self._check_updates_setting)
        form.addRow(updates_group)

        return tab

    # === QDialog overrides ===

    def reject(self) -> None:
        """Kill any rigctld we launched before closing, then super().reject()."""
        self._stop_rigctld()
        super().reject()

    # === Private slots ===

    def _refresh_banner_preview_if_built(self) -> None:
        """Refresh the banner + autosave previews only after the Images tab has been built.

        ``_callsign.textChanged`` fires during ``_build_radio_tab`` before
        ``_build_images_tab`` has run, so ``_banner_preview`` doesn't exist
        yet.  The guard prevents an AttributeError on dialog construction.
        v0.2.8 also refreshes the filename preview from here so the
        auto-save preview picks up callsign edits made on the Radio tab
        without the user having to switch tabs.
        """
        if hasattr(self, "_banner_preview"):
            self._refresh_banner_preview()
        if hasattr(self, "_autosave_preview"):
            self._refresh_autosave_preview()

    def _test_connection(self) -> None:
        """Try to connect and ping the rigctld daemon at the current settings."""
        host = self._rigctld_host.text().strip()
        port = self._rigctld_port.value()
        try:
            client = RigctldClient(host=host, port=port)
            client.open()
            client.ping()
            freq = client.get_freq()
            mode, _ = client.get_mode()
            client.close()
            QMessageBox.information(
                self,
                "Connection successful",
                f"Connected to rigctld at {host}:{port}.\n\n"
                f"Frequency: {freq / 1_000_000:.6f} MHz\n"
                f"Mode: {mode}",
            )
        except RigError as exc:
            QMessageBox.warning(
                self,
                "Connection failed",
                f"Could not connect to rigctld at {host}:{port}.\n\n"
                f"Error: {exc}\n\n"
                "Make sure rigctld is running, or use the launcher above.",
            )

    def _launch_rigctld(self) -> None:
        """Spawn a rigctld process with the current radio settings."""
        model_id = self._custom_model_id.value()
        serial_port = self._rigctld_serial_combo.currentText().strip()
        baud_rate = self._rigctld_baud_combo.currentData()
        tcp_port = self._rigctld_port.value()

        if model_id == 0:
            QMessageBox.warning(
                self, "No radio selected",
                "Please select a radio model before launching rigctld.",
            )
            return

        # OP-13: reject a serial-port string that could be mis-parsed as
        # a rigctld flag.  The editable combo allows free-form entry,
        # so a paste of "--help" or similar would otherwise end up as a
        # positional arg to Popen.  List-form argv already blocks shell
        # injection; this closes the arg-smuggling gap.
        if not is_safe_rigctld_arg(serial_port):
            QMessageBox.warning(
                self, "Unsafe serial port",
                f"Refusing to launch rigctld with serial port "
                f"{serial_port!r} — values starting with '-' are not "
                "accepted because they would be interpreted as rigctld "
                "flags.  Select a real device path.",
            )
            return

        cmd = ["rigctld", "-m", str(model_id), "-t", str(tcp_port)]
        if serial_port:
            cmd += ["-r", serial_port]
        if baud_rate:
            cmd += ["-s", str(baud_rate)]

        try:
            # OP2-14: start_new_session=True isolates the child process so a
            # GUI crash doesn't orphan rigctld holding the serial port.
            self._rigctld_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._rigctld_status.setText(f"rigctld launched (PID {self._rigctld_proc.pid})")
            self._rigctld_status.setStyleSheet("color: green;")
            self._launch_btn.setEnabled(False)
            self._stop_rigctld_btn.setEnabled(True)
        except FileNotFoundError:
            QMessageBox.warning(
                self, "rigctld not found",
                "Could not find <b>rigctld</b> on this system.\n\n"
                "Install Hamlib (e.g. <code>brew install hamlib</code> on macOS, "
                "or <code>sudo apt install libhamlib-utils</code> on Linux).",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "Launch failed",
                f"Could not launch rigctld:\n\n{exc}",
            )

    def _stop_rigctld(self) -> None:
        """Terminate the rigctld process we launched.

        Defensive against a process that already exited on its own
        (rigctld rejected its CLI args, port collision, etc.) — the
        ``terminate``/``wait``/``kill`` chain can raise
        ``ProcessLookupError`` or ``OSError`` and we treat that as
        "already stopped" so the dialog state stays consistent (OP-19).
        """
        if self._rigctld_proc is not None:
            try:
                self._rigctld_proc.terminate()
                try:
                    self._rigctld_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        self._rigctld_proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
            except (ProcessLookupError, OSError):
                pass
            self._rigctld_proc = None
            self._rigctld_status.setText("rigctld stopped.")
            self._rigctld_status.setStyleSheet("color: gray;")
            self._launch_btn.setEnabled(True)
            self._stop_rigctld_btn.setEnabled(False)

    def _browse_save_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select save directory", self._save_dir.text()
        )
        if directory:
            self._save_dir.setText(directory)

    def _refresh_autosave_preview(self) -> None:
        """Re-render the auto-save filename preview from the current inputs.

        Builds a throwaway :class:`TokenContext` against the current
        clock, callsign, and a canonical mode (the configured default
        TX mode, or ``Scottie-S1`` if that widget hasn't been built
        yet), then resolves the template the same way the RX/TX save
        paths do. Uses ``save_dir=None`` equivalent logic by pointing
        the builder at the configured save directory only if it
        already exists — otherwise we resolve into a fixed temp path
        so the user sees a realistic filename without us creating
        directories from a preview render.

        Safe to call before the Images tab is fully built: the guard
        on ``hasattr`` is the same pattern as ``_refresh_banner_preview_if_built``.
        """
        if not hasattr(self, "_autosave_preview"):
            return
        pattern = self._autosave_pattern.text()
        fmt = self._autosave_format.currentData() or "png"
        callsign = (
            self._callsign.text().strip().upper()
            if hasattr(self, "_callsign")
            else self._config.callsign
        )
        mode_name = (
            self._tx_mode.currentData()
            if hasattr(self, "_tx_mode") and self._tx_mode.currentData()
            else "Scottie-S1"
        )
        ctx = TokenContext(
            callsign=callsign,
            mode=mode_name,
            direction="RX",
            now_utc=datetime.datetime.now(datetime.UTC),
        )
        # Resolve in-memory without touching the filesystem — the
        # preview must not ``mkdir`` the save directory, and it must
        # not pick collision suffixes from whatever files happen to be
        # in there.  Call the lower-level resolver directly.
        from open_sstv.templates.filename import sanitize_filename_component
        from open_sstv.templates.tokens import resolve_tokens

        resolved = resolve_tokens(pattern, ctx)
        stem = sanitize_filename_component(resolved)
        self._autosave_preview.setText(f"{stem}.{fmt}")

    def _refresh_banner_preview(self) -> None:
        """Re-render the banner preview label from the current selections.

        Uses the same ``apply_tx_banner`` call that ``TxWorker.transmit``
        uses — what you see here is exactly what will be stamped on air.
        The source image is a 320×240 neutral-gray fill; we crop the top
        *banner_height* rows as the preview pixmap and resize the label
        to match so the strip always fills the allocated space exactly.
        """
        from PIL import Image as _PILImage

        size_key = self._banner_size.currentData() or "small"
        source = _PILImage.new("RGB", (320, 240), (0x80, 0x80, 0x80))
        bh, fs = scaled_banner_params(size_key, source.width)
        banner = apply_tx_banner(
            source,
            _APP_VERSION,
            self._callsign.text().strip().upper(),
            self._banner_bg_color,
            self._banner_text_color,
            banner_height=bh,
            font_size=fs,
        )
        strip = banner.crop((0, 0, 320, bh))
        raw = strip.tobytes("raw", "RGB")
        qimg = QImage(raw, strip.width, strip.height, strip.width * 3,
                      QImage.Format.Format_RGB888)
        self._banner_preview.setFixedHeight(bh)
        self._banner_preview.setPixmap(QPixmap.fromImage(qimg))

    def _pick_banner_bg_color(self) -> None:
        """Open a colour picker for the TX banner background."""
        color = QColorDialog.getColor(
            QColor(self._banner_bg_color), self, "Banner background colour"
        )
        if color.isValid():
            self._banner_bg_color = color.name()
            self._banner_bg_btn.setStyleSheet(
                f"background-color: {self._banner_bg_color}; border: 1px solid #888;"
            )
            self._refresh_banner_preview()

    def _pick_banner_text_color(self) -> None:
        """Open a colour picker for the TX banner text."""
        color = QColorDialog.getColor(
            QColor(self._banner_text_color), self, "Banner text colour"
        )
        if color.isValid():
            self._banner_text_color = color.name()
            self._banner_text_btn.setStyleSheet(
                f"background-color: {self._banner_text_color}; border: 1px solid #888;"
            )
            self._refresh_banner_preview()

    def _preview_banner_on_image(self) -> None:
        """Show the banner composited onto the current TX image (or a file pick).

        When the TX panel has an image loaded (passed in via ``tx_image``),
        it is used directly so the operator sees exactly what will go on air.
        If no TX image is available, falls back to a file picker so the button
        is still useful when the dialog is opened before loading an image.

        Does not require saving settings first — current colour and size
        selections are applied live.  Large images are scaled to 80 % of the
        screen to prevent spill on laptop displays.
        """
        from pathlib import Path as _Path

        from PIL import Image as _PILImage
        from PIL import UnidentifiedImageError

        if self._tx_image is not None:
            source: _PILImage.Image = self._tx_image.convert("RGB")  # type: ignore[union-attr]
            title = "Banner preview — TX image"
        else:
            initial_dir = self._save_dir.text() or str(_Path.home())
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Preview banner on image",
                initial_dir,
                "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tiff *.gif);;All files (*)",
            )
            if not path:
                return
            try:
                source = _PILImage.open(path).convert("RGB")
            except (OSError, UnidentifiedImageError, _PILImage.DecompressionBombError) as exc:
                QMessageBox.warning(
                    self,
                    "Could not open image",
                    f"Failed to load {path!r}:\n\n{exc}",
                )
                return
            title = f"Banner preview — {_Path(path).name}"

        size_key = self._banner_size.currentData() or "small"
        bh, fs = scaled_banner_params(size_key, source.width)
        stamped = apply_tx_banner(
            source,
            _APP_VERSION,
            self._callsign.text().strip().upper(),
            self._banner_bg_color,
            self._banner_text_color,
            banner_height=bh,
            font_size=fs,
        )

        raw = stamped.tobytes("raw", "RGB")
        qimg = QImage(
            raw, stamped.width, stamped.height, stamped.width * 3,
            QImage.Format.Format_RGB888,
        )
        pix = QPixmap.fromImage(qimg)

        screen_geom = self.screen().availableGeometry()
        max_w = int(screen_geom.width() * 0.80)
        max_h = int(screen_geom.height() * 0.80)
        if pix.width() > max_w or pix.height() > max_h:
            pix = pix.scaled(
                max_w, max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QVBoxLayout(dlg)
        caption = QLabel(
            f"{stamped.width}×{stamped.height} · {size_key} size "
            f"({bh} px / {fs} pt)"
        )
        caption.setStyleSheet("color: #888;")
        lay.addWidget(caption)
        img_label = QLabel()
        img_label.setPixmap(pix)
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(img_label)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    @Slot(bool)
    def _on_overdrive_toggled(self, enabled: bool) -> None:
        """Expand or contract the TX output gain slider range."""
        if enabled:
            self._output_gain_slider.setMaximum(200)
            self._output_gain_row_label.setText("TX output gain (0–200%, overdrive):")
        else:
            if self._output_gain_slider.value() > 100:
                self._output_gain_slider.setValue(100)
            self._output_gain_slider.setMaximum(100)
            self._output_gain_row_label.setText("TX output gain (0–100%):")

    @Slot()
    def _on_test_tone_clicked(self) -> None:
        """Disable the button immediately and emit the signal to main window."""
        self._tx_active = True
        self._update_test_tone_btn()
        self.test_tone_requested.emit()

    def _update_test_tone_btn(self) -> None:
        """Enable Test Tone only when a rig is connected and no TX is active."""
        enabled = self._rig_connected and not self._tx_active
        self._test_tone_btn.setEnabled(enabled)
        self._test_tone_btn.setText("Testing…" if self._tx_active else "Test Tone")

    # === TX lifecycle slots (connected by MainWindow before exec()) ===

    @Slot()
    def on_tx_started(self) -> None:
        """Called by MainWindow when any transmission begins."""
        self._tx_active = True
        self._update_test_tone_btn()

    @Slot()
    def on_tx_ended(self) -> None:
        """Called by MainWindow when a transmission completes, aborts, or errors."""
        self._tx_active = False
        self._update_test_tone_btn()

    @Slot(str)
    def on_tx_error(self, _message: str) -> None:
        """Called by MainWindow on TX error; restores button state."""
        self._tx_active = False
        self._update_test_tone_btn()

    # === Public API ===

    def result_config(self) -> AppConfig:
        """Build a new ``AppConfig`` from the current dialog state.

        Call after ``exec()`` returns ``QDialog.Accepted``.
        """
        conn_mode = self._conn_mode_combo.currentData() or RigConnectionMode.MANUAL.value

        # Serial port and baud come from the mode-specific widgets.  String
        # equality against RigConnectionMode values (StrEnum → str) keeps
        # this branch dense and immune to typos vs. bare literals (OP-28).
        if conn_mode == RigConnectionMode.SERIAL:
            serial_port = self._serial_port_combo.currentText().strip()
            baud_rate = self._baud_rate_combo.currentData() or 9600
        elif conn_mode == RigConnectionMode.RIGCTLD:
            serial_port = self._rigctld_serial_combo.currentText().strip()
            baud_rate = self._rigctld_baud_combo.currentData() or 9600
        else:
            serial_port = self._config.rig_serial_port
            baud_rate = self._config.rig_baud_rate

        return AppConfig(
            audio_input_device=self._input_combo.currentData(),
            audio_output_device=self._output_combo.currentData(),
            sample_rate=self._sample_rate.currentData(),
            default_tx_mode=self._tx_mode.currentData(),
            rig_connection_mode=conn_mode,
            rigctld_host=self._rigctld_host.text().strip(),
            rigctld_port=self._rigctld_port.value(),
            ptt_delay_s=self._ptt_delay.value(),
            rig_model_id=self._custom_model_id.value(),
            rig_serial_port=serial_port,
            rig_baud_rate=baud_rate,
            auto_launch_rigctld=self._auto_launch.isChecked(),
            rig_serial_protocol=self._serial_protocol_combo.currentText(),
            rig_civ_address=self._civ_address_spin.value(),
            rig_ptt_line=self._ptt_line_combo.currentData() or "DTR",
            audio_input_gain=self._input_gain_slider.value() / 100.0,
            audio_output_gain=self._output_gain_slider.value() / 100.0,
            tx_output_overdrive=self._overdrive_check.isChecked(),
            rx_weak_signal_mode=self._weak_signal_check.isChecked(),
            apply_final_slant_correction=self._final_slant_check.isChecked(),
            incremental_decode=self._incremental_check.isChecked(),
            cw_id_enabled=self._cw_enabled.isChecked(),
            cw_id_wpm=self._cw_wpm.value(),
            cw_id_tone_hz=self._cw_tone_hz.value(),
            callsign=self._callsign.text().strip().upper(),
            images_save_dir=self._save_dir.text(),
            auto_save=self._auto_save.isChecked(),
            autosave_tx=self._autosave_tx.isChecked(),
            autosave_filename_pattern=self._autosave_pattern.text(),
            autosave_file_format=self._autosave_format.currentData() or "png",
            tx_banner_enabled=self._banner_enabled.isChecked(),
            tx_banner_bg_color=self._banner_bg_color,
            tx_banner_text_color=self._banner_text_color,
            tx_banner_size=self._banner_size.currentData() or "small",
            check_for_updates=self._check_updates_setting.isChecked(),
            first_launch_seen=self._config.first_launch_seen,
        )

    @property
    def rigctld_process(self) -> subprocess.Popen | None:
        """Return the rigctld subprocess if we launched one."""
        return self._rigctld_proc


_ports_cache: list = []
_ports_cache_time: float = 0.0
_PORTS_CACHE_TTL_S: float = 5.0


def _list_serial_ports() -> list:
    """Return available serial ports, cached for ``_PORTS_CACHE_TTL_S`` seconds.

    Calling ``comports()`` on every Settings open can take 100–200 ms on
    Linux with many USB devices and runs on the GUI thread. The 5-second
    TTL is short enough to pick up a device plugged in while the dialog is
    open (user closes, plugs in cable, reopens), while avoiding repeated
    enumeration when the dialog is quickly dismissed and re-opened.

    Falls back to an empty list (and logs a warning) if enumeration fails.
    """
    import time as _time

    global _ports_cache, _ports_cache_time
    now = _time.monotonic()
    if now - _ports_cache_time < _PORTS_CACHE_TTL_S:
        return _ports_cache
    try:
        _ports_cache = list(serial.tools.list_ports.comports())
    except Exception:  # noqa: BLE001
        _log.warning("Could not enumerate serial ports", exc_info=True)
        _ports_cache = []
    _ports_cache_time = now
    return _ports_cache


__all__ = ["SettingsDialog"]
