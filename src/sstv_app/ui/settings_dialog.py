# SPDX-License-Identifier: GPL-3.0-or-later
"""Modal settings dialog.

Edits an ``AppConfig`` instance from ``sstv_app.config.schema``. On accept,
the caller reads the updated config via ``result_config()`` and persists it.
Lays out fields by section: Audio, Radio, Images.

Uses ``QDialogButtonBox`` with OK/Cancel so the user can back out without
saving. The caller (``MainWindow``) is responsible for calling
``save_config`` and applying any live changes (e.g. toggling rig polling)
after the dialog is accepted.
"""
from __future__ import annotations

import logging
import subprocess

import serial.tools.list_ports

_log = logging.getLogger(__name__)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
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

from sstv_app.radio.exceptions import RigError
from sstv_app.radio.rigctld import RigctldClient
from sstv_app.radio.serial_rig import (
    ICOM_ADDRESSES,
    SERIAL_RIG_PROTOCOLS,
    create_serial_rig,
)

from sstv_app.audio.devices import (
    AudioDevice,
    list_input_devices,
    list_output_devices,
)
from sstv_app.config.schema import AppConfig
from sstv_app.core.modes import Mode


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

    def __init__(
        self, config: AppConfig, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._config = config

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

        # Output gain slider (0–200% in 1% steps)
        out_row = QHBoxLayout()
        self._output_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self._output_gain_slider.setRange(0, 200)
        self._output_gain_slider.setValue(int(self._config.audio_output_gain * 100))
        self._output_gain_label = QLabel(f"{self._config.audio_output_gain * 100:.0f}%")
        self._output_gain_label.setFixedWidth(45)
        self._output_gain_slider.valueChanged.connect(
            lambda v: self._output_gain_label.setText(f"{v}%")
        )
        out_row.addWidget(self._output_gain_slider)
        out_row.addWidget(self._output_gain_label)
        gain_layout.addRow("TX output gain:", out_row)

        form.addRow(gain_group)

        return tab

    def _build_radio_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # --- Connection mode selector ---
        mode_group = QGroupBox("Connection Mode")
        mode_form = QFormLayout(mode_group)

        self._conn_mode_combo = QComboBox()
        self._conn_mode_combo.addItem("Manual (no rig control)", "manual")
        self._conn_mode_combo.addItem("Direct Serial (built-in)", "serial")
        self._conn_mode_combo.addItem("rigctld (Hamlib daemon)", "rigctld")
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
        misc_form.addRow("Callsign:", self._callsign)

        layout.addWidget(misc_group)
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
        self._serial_group.setVisible(mode == "serial")
        self._rigctld_group.setVisible(mode == "rigctld")
        if mode == "serial":
            self._on_serial_protocol_changed()

    def _on_serial_protocol_changed(self) -> None:
        """Show/hide CI-V address and PTT line based on selected protocol."""
        proto = self._serial_protocol_combo.currentText()
        is_icom = proto == "Icom CI-V"
        is_ptt_only = proto.startswith("PTT Only")
        self._civ_address_row_label.setVisible(is_icom)
        self._civ_address_spin.parentWidget()  # trigger layout
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
        form.addRow("Default TX mode:", self._tx_mode)

        # Auto-save
        self._auto_save = QCheckBox("Auto-save decoded images")
        self._auto_save.setChecked(self._config.auto_save)
        form.addRow(self._auto_save)

        # Save directory
        dir_row = QHBoxLayout()
        self._save_dir = QLineEdit(self._config.images_save_dir)
        self._save_dir.setReadOnly(True)
        dir_row.addWidget(self._save_dir)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_save_dir)
        dir_row.addWidget(browse_btn)
        form.addRow("Save directory:", dir_row)

        return tab

    # === Private slots ===

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

        cmd = ["rigctld", "-m", str(model_id), "-t", str(tcp_port)]
        if serial_port:
            cmd += ["-r", serial_port]
        if baud_rate:
            cmd += ["-s", str(baud_rate)]

        try:
            self._rigctld_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
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
        """Terminate the rigctld process we launched."""
        if self._rigctld_proc is not None:
            self._rigctld_proc.terminate()
            try:
                self._rigctld_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._rigctld_proc.kill()
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

    # === Public API ===

    def result_config(self) -> AppConfig:
        """Build a new ``AppConfig`` from the current dialog state.

        Call after ``exec()`` returns ``QDialog.Accepted``.
        """
        conn_mode = self._conn_mode_combo.currentData() or "manual"

        # Serial port and baud come from the mode-specific widgets
        if conn_mode == "serial":
            serial_port = self._serial_port_combo.currentText().strip()
            baud_rate = self._baud_rate_combo.currentData() or 9600
        elif conn_mode == "rigctld":
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
            rig_enabled=conn_mode != "manual",
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
            callsign=self._callsign.text().strip().upper(),
            last_image_dir=self._config.last_image_dir,
            images_save_dir=self._save_dir.text(),
            auto_save=self._auto_save.isChecked(),
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
