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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sstv_app.radio.exceptions import RigError
from sstv_app.radio.rigctld import RigctldClient

from sstv_app.audio.devices import (
    AudioDevice,
    list_input_devices,
    list_output_devices,
)
from sstv_app.config.schema import AppConfig
from sstv_app.core.modes import Mode


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

        return tab

    def _build_radio_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        # Explanatory note
        help_label = QLabel(
            "Open SSTV controls your radio through Hamlib's "
            "<b>rigctld</b> daemon.\n"
            "Start rigctld in a terminal first, e.g.:\n"
            "<code>rigctld -m 1035 -r /dev/ttyUSB0 -s 38400</code>\n\n"
            "Then enter the host and port below and use the "
            "<b>Connect Rig</b> button in the main window."
        )
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(help_label)

        self._rig_enabled = QCheckBox("Enable rig control (rigctld)")
        self._rig_enabled.setChecked(self._config.rig_enabled)
        form.addRow(self._rig_enabled)

        self._rigctld_host = QLineEdit(self._config.rigctld_host)
        form.addRow("rigctld host:", self._rigctld_host)

        self._rigctld_port = QSpinBox()
        self._rigctld_port.setRange(1, 65535)
        self._rigctld_port.setValue(self._config.rigctld_port)
        form.addRow("rigctld port:", self._rigctld_port)

        # Test connection button
        self._test_btn = QPushButton("Test Connection")
        self._test_btn.clicked.connect(self._test_connection)
        form.addRow("", self._test_btn)

        self._ptt_delay = QDoubleSpinBox()
        self._ptt_delay.setRange(0.0, 2.0)
        self._ptt_delay.setSingleStep(0.05)
        self._ptt_delay.setDecimals(2)
        self._ptt_delay.setSuffix(" s")
        self._ptt_delay.setValue(self._config.ptt_delay_s)
        form.addRow("PTT delay:", self._ptt_delay)

        self._callsign = QLineEdit(self._config.callsign)
        self._callsign.setPlaceholderText("e.g. W0AEZ")
        form.addRow("Callsign:", self._callsign)

        return tab

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
                "Make sure rigctld is running. Example:\n"
                "  rigctld -m 1035 -r /dev/ttyUSB0 -s 38400",
            )

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
        return AppConfig(
            audio_input_device=self._input_combo.currentData(),
            audio_output_device=self._output_combo.currentData(),
            sample_rate=self._sample_rate.currentData(),
            default_tx_mode=self._tx_mode.currentData(),
            rigctld_host=self._rigctld_host.text().strip(),
            rigctld_port=self._rigctld_port.value(),
            rig_enabled=self._rig_enabled.isChecked(),
            ptt_delay_s=self._ptt_delay.value(),
            callsign=self._callsign.text().strip().upper(),
            last_image_dir=self._config.last_image_dir,
            images_save_dir=self._save_dir.text(),
            auto_save=self._auto_save.isChecked(),
        )


__all__ = ["SettingsDialog"]
