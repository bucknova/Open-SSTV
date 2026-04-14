# Waterfall Display — v1.1 Scope

## Window type

Floating `QMainWindow` (not a dialog). Shown/hidden via **View → Waterfall**
toggle in the main menu; state persisted in `AppConfig`. Keeping it separate
from the main window avoids reflowing the TX/RX splitter layout and lets
users move it to a second monitor.

## FFT tap point

`RxWorker.feed_chunk` already receives every raw audio chunk from
`InputStreamWorker`. Add the FFT there, after the gain adjustment and
before the existing scratch-buffer accumulation. This keeps DSP off the
GUI thread at no extra cost — `RxWorker` already runs on its own thread.

## New signal on RxWorker

```
waterfall_chunk = Signal(object)   # 1-D numpy float32 array, linear magnitude
```

Emitted once per chunk with the FFT magnitude column. `MainWindow` connects
it to `WaterfallWidget.add_column` via a queued connection only when the
waterfall window is visible (disconnect on hide, reconnect on show) to
avoid computing FFTs for an invisible widget.

## FFT parameters

| Parameter | Value | Rationale |
|---|---|---|
| Window size | 1024 samples | ~21 ms at 48 kHz; fits in one PortAudio chunk |
| Overlap | 50 % (512 samples) | Smooth scroll, no temporal smearing |
| Window fn | Hann | Good sidelobe rejection for SSTV tones |
| Output bins | 512 (DC to Nyquist) | Display 0–4000 Hz; discard upper half |
| Frequency res | ~47 Hz/bin | Enough to distinguish 1200 / 1500 / 2300 Hz markers |

Emit only the lower 170 bins (0–8000 Hz) to keep signal size small; the
widget clips to 0–4000 Hz display range internally.

## Colormap

Classic amateur-radio palette: black (noise floor) → blue → green → yellow →
white (peak). Map linearly over a configurable dB range, default −80 to 0 dBFS.
Store as a 256-entry `uint8` RGB LUT computed once at widget init.

## Scrolling renderer

`QImage` in `Format_RGB888`, one pixel wide per column, full display height
tall (256 px). On each `add_column` call:

1. `np.roll` the backing numpy array one column left (or keep a write-cursor
   index and wrap — avoids the copy).
2. Write the new column at the right edge.
3. `QImage.fromData(array)` → `QLabel.setPixmap(pixmap.scaled(...))`.

`QGraphicsScene` is overkill here and adds allocation overhead. A plain
`QLabel` + `QPixmap` repaint on a 50 ms `QTimer` is sufficient and simpler.

## Performance budget

- FFT + LUT lookup: < 1 ms per chunk on any modern machine (numpy FFT,
  vectorised colormap).
- Repaint: target ≤ 20 repaints/second (50 ms timer), decoupled from the
  audio chunk rate so a slow repaint never stalls DSP.
- No waterfall work when window is hidden (signal disconnected).

## Main window integration

Add **View → Waterfall** `QAction` (checkable, default unchecked) to the
menu bar. On check: create `WaterfallWidget` if not yet constructed, show
it, connect `rx_worker.waterfall_chunk`. On uncheck: hide it, disconnect
signal. Persist checked state to `AppConfig.show_waterfall: bool = False`.

## Out of scope for v1.1

- Frequency markers / grid lines (v1.2)
- Click-to-tune (requires rig write-back)
- Adjustable dB range slider (hardcoded default is fine for v1.1)
- Persistence mode / peak hold
- Zoom or pan
- Waterfall recording to file
