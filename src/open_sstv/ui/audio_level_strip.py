# SPDX-License-Identifier: GPL-3.0-or-later
"""Slim always-on audio strip shown to the right of the RX panel.

Three vertical controls side by side:

1. **TX gain** slider — mirrors ``AppConfig.audio_output_gain`` (the same
   value as Settings → Audio → Software Gain → "TX output gain").
2. **RX gain** slider — mirrors ``AppConfig.audio_input_gain``.
3. A :class:`~open_sstv.ui.level_meter.LevelMeter` showing the live RX
   input level.

Both sliders push their value to the audio workers live on every tick
(``tx_gain_changed`` / ``rx_gain_changed``, gain as a 0.0–2.0 float) and
emit a ``*_gain_committed`` signal on release so MainWindow can persist
to disk once, not on every pixel of a drag.

The widget owns no audio, no threads and no config — MainWindow wires the
signals and calls ``set_tx_gain`` / ``set_rx_gain`` / ``set_tx_overdrive``
to keep it in sync with the Settings dialog.  Those setters block signals
so a programmatic sync never loops back into a worker push or a disk write.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from open_sstv.ui.level_meter import LevelMeter

#: Slider ceilings, in percent.  RX has always allowed up to 200 %;
#: TX is capped at 100 % unless overdrive is enabled (matches the
#: Settings dialog, see ``settings_dialog._on_overdrive_toggled``).
_RX_MAX_PCT: int = 200
_TX_MAX_PCT: int = 100
_TX_OVERDRIVE_MAX_PCT: int = 200


class AudioLevelStrip(QWidget):
    """TX gain + RX gain sliders and a live RX level meter, vertical."""

    #: Emitted on every slider tick — gain as a 0.0–2.0 float (live push).
    tx_gain_changed = Signal(float)
    rx_gain_changed = Signal(float)
    #: Emitted once when the user releases the slider — persist to config.
    tx_gain_committed = Signal(float)
    rx_gain_committed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        self._tx_slider, self._tx_value, tx_col = self._build_slider_column(
            "TX", _TX_MAX_PCT,
            "Software TX output gain — same value as Settings → Audio.\n"
            "Release the slider to save it.",
        )
        self._rx_slider, self._rx_value, rx_col = self._build_slider_column(
            "RX", _RX_MAX_PCT,
            "Software RX input gain — same value as Settings → Audio.\n"
            "Release the slider to save it.",
        )

        self._tx_slider.valueChanged.connect(self._on_tx_changed)
        self._tx_slider.sliderReleased.connect(
            lambda: self.tx_gain_committed.emit(self._tx_slider.value() / 100.0)
        )
        self._rx_slider.valueChanged.connect(self._on_rx_changed)
        self._rx_slider.sliderReleased.connect(
            lambda: self.rx_gain_committed.emit(self._rx_slider.value() / 100.0)
        )

        # Meter column.
        self._meter = LevelMeter(self)
        meter_col = QVBoxLayout()
        meter_col.setSpacing(2)
        meter_caption = QLabel("in")
        meter_caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        meter_col.addWidget(meter_caption)
        meter_col.addWidget(self._meter, stretch=1)
        meter_unit = QLabel("dBFS")
        meter_unit.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        meter_unit.setStyleSheet("color: gray; font-size: 9px;")
        meter_col.addWidget(meter_unit)

        root.addLayout(tx_col)
        root.addLayout(rx_col)
        root.addLayout(meter_col)

    def _build_slider_column(
        self, label: str, max_pct: int, tooltip: str
    ) -> tuple[QSlider, QLabel, QVBoxLayout]:
        col = QVBoxLayout()
        col.setSpacing(2)

        cap = QLabel(label)
        cap.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        slider = QSlider(Qt.Orientation.Vertical)
        slider.setRange(0, max_pct)
        slider.setValue(100)
        slider.setToolTip(tooltip)
        slider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        value = QLabel("100%")
        value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        value.setStyleSheet("font-size: 9px;")

        col.addWidget(cap)
        col.addWidget(slider, stretch=1, alignment=Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(value)
        return slider, value, col

    # === signal handlers ===

    def _on_tx_changed(self, v: int) -> None:
        self._tx_value.setText(f"{v}%")
        self.tx_gain_changed.emit(v / 100.0)
        # Keyboard / wheel steps never fire sliderReleased — persist those
        # here.  During a mouse drag isSliderDown() is True, so the commit
        # is deferred to sliderReleased (one disk write per drag, not per px).
        if not self._tx_slider.isSliderDown():
            self.tx_gain_committed.emit(v / 100.0)

    def _on_rx_changed(self, v: int) -> None:
        self._rx_value.setText(f"{v}%")
        self.rx_gain_changed.emit(v / 100.0)
        if not self._rx_slider.isSliderDown():
            self.rx_gain_committed.emit(v / 100.0)

    # === MainWindow-facing setters (no signals emitted) ===

    def set_tx_gain(self, gain: float) -> None:
        pct = int(round(gain * 100))
        blocked = self._tx_slider.blockSignals(True)
        self._tx_slider.setValue(min(pct, self._tx_slider.maximum()))
        self._tx_slider.blockSignals(blocked)
        self._tx_value.setText(f"{self._tx_slider.value()}%")

    def set_rx_gain(self, gain: float) -> None:
        pct = int(round(gain * 100))
        blocked = self._rx_slider.blockSignals(True)
        self._rx_slider.setValue(min(pct, self._rx_slider.maximum()))
        self._rx_slider.blockSignals(blocked)
        self._rx_value.setText(f"{self._rx_slider.value()}%")

    def set_tx_overdrive(self, enabled: bool) -> None:
        """Expand / contract the TX slider ceiling (100 % ↔ 200 %)."""
        new_max = _TX_OVERDRIVE_MAX_PCT if enabled else _TX_MAX_PCT
        if new_max == self._tx_slider.maximum():
            return
        blocked = self._tx_slider.blockSignals(True)
        self._tx_slider.setMaximum(new_max)
        if self._tx_slider.value() > new_max:
            self._tx_slider.setValue(new_max)
        self._tx_slider.blockSignals(blocked)
        self._tx_value.setText(f"{self._tx_slider.value()}%")

    @Slot(float)
    def set_input_level(self, peak_linear: float) -> None:
        self._meter.set_level(peak_linear)

    @Slot()
    def reset_level(self) -> None:
        self._meter.reset()
