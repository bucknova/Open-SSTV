# Open-SSTV User Guide

**Version 0.1.2** | GPL-3.0-or-later

---

## Table of Contents

1. [What Is SSTV?](#1-what-is-sstv)
2. [About Open-SSTV](#2-about-open-sstv)
3. [Installation and Setup](#3-installation-and-setup)
   - [System Requirements](#31-system-requirements)
   - [Installing from Source](#32-installing-from-source)
   - [Command-Line Tools](#33-command-line-tools)
4. [Quick Start](#4-quick-start)
5. [How a Typical SSTV QSO Works](#5-how-a-typical-sstv-qso-works)
6. [The Main Window](#6-the-main-window)
   - [Radio Panel (Toolbar)](#61-radio-panel-toolbar)
   - [Transmit Panel (Left)](#62-transmit-panel-left)
   - [Receive Panel (Right)](#63-receive-panel-right)
   - [Menu Bar](#64-menu-bar)
7. [Receiving Images](#7-receiving-images)
   - [Starting a Capture](#71-starting-a-capture)
   - [Progressive Decode](#72-progressive-decode)
   - [The Image Gallery](#73-the-image-gallery)
   - [Saving Received Images](#74-saving-received-images)
8. [Transmitting Images](#8-transmitting-images)
   - [Loading an Image](#81-loading-an-image)
   - [Editing an Image](#82-editing-an-image)
   - [Choosing an SSTV Mode](#83-choosing-an-sstv-mode)
   - [Using QSO Templates](#84-using-qso-templates)
   - [Sending the Transmission](#85-sending-the-transmission)
9. [SSTV Modes Reference](#9-sstv-modes-reference)
10. [Radio and Rig Control](#10-radio-and-rig-control)
    - [Manual Mode (No Rig Control)](#101-manual-mode-no-rig-control)
    - [rigctld (Hamlib Daemon)](#102-rigctld-hamlib-daemon)
    - [Direct Serial (Built-In CAT)](#103-direct-serial-built-in-cat)
    - [Supported Radios and Protocols](#104-supported-radios-and-protocols)
    - [PTT Delay](#105-ptt-delay)
11. [Audio Setup](#11-audio-setup)
    - [Choosing Audio Devices](#111-choosing-audio-devices)
    - [Sample Rate](#112-sample-rate)
    - [Input and Output Gain](#113-input-and-output-gain)
    - [Audio Routing Tips](#114-audio-routing-tips)
12. [Settings Reference](#12-settings-reference)
    - [Audio Tab](#121-audio-tab)
    - [Radio Tab](#122-radio-tab)
    - [Images Tab](#123-images-tab)
13. [QSO Templates In Depth](#13-qso-templates-in-depth)
    - [How Templates Work](#131-how-templates-work)
    - [Built-In Templates](#132-built-in-templates)
    - [Creating Custom Templates](#133-creating-custom-templates)
    - [Placeholder Reference](#134-placeholder-reference)
14. [Image Editor Reference](#14-image-editor-reference)
15. [Command-Line Interface](#15-command-line-interface)
    - [sstv-app-encode](#151-sstv-app-encode)
    - [sstv-app-decode](#152-sstv-app-decode)
16. [Configuration Files](#16-configuration-files)
17. [Troubleshooting](#17-troubleshooting)
18. [Glossary](#18-glossary)

---

## 1. What Is SSTV?

Slow-Scan Television (SSTV) is a method of transmitting still images over radio. Unlike broadcast television, which sends 25 or 30 full frames per second, SSTV sends a single image over a period of roughly 36 seconds to two minutes, depending on the mode. The image is encoded as audio tones: each horizontal line of the picture is converted into a sweep of frequencies between 1500 Hz (black) and 2300 Hz (white), and a short 1200 Hz sync pulse marks the boundary between lines.

SSTV is popular among amateur (ham) radio operators, particularly on the HF bands (most commonly 14.230 MHz USB for 20 meters and 3.845 MHz LSB for 75 meters). Operators exchange images of their stations, antennas, scenery, QSL cards, and—most often—images with their callsign, a signal report, and a greeting. SSTV works on any voice-capable radio: HF, VHF, UHF, and even through amateur satellites and the International Space Station.

No special hardware is required beyond a radio and a computer with a sound card. The computer generates the audio tones for transmission and decodes received tones back into an image.

---

## 2. About Open-SSTV

Open-SSTV is a free, open-source, cross-platform SSTV transceiver for amateur radio operators. It provides a graphical application for transmitting and receiving SSTV images as well as headless command-line tools for encoding and decoding. The project is built with Python and Qt 6 (PySide6) and runs on Linux, macOS, and Windows.

Key features at a glance:

- **Transmit and receive** SSTV images with a modern Qt 6 interface
- **Three popular SSTV modes**: Robot 36, Martin M1, Scottie S1
- **Real-time progressive decode** — watch the image appear line by line as it is received
- **Automatic VIS detection** — the app identifies the incoming SSTV mode automatically
- **Built-in image editor** with crop, rotate, flip, and text overlays
- **QSO templates** — one-click callsign and exchange overlays for fast on-air operation
- **Rig control** via Hamlib's rigctld daemon or direct serial CAT (Icom CI-V, Kenwood, Yaesu) with PTT keying
- **PTT-only serial mode** for rigs without full CAT support (DTR or RTS line keying)
- **Audio device selection** with adjustable input/output gain
- **Image gallery** showing the 20 most-recent decoded images
- **Auto-save** option for received images
- **Slant correction** — compensates for TX/RX sound card clock drift
- **Headless CLI tools** for encoding and decoding WAV files (suitable for Raspberry Pi or CI pipelines)

---

## 3. Installation and Setup

### 3.1 System Requirements

- **Python 3.11 or later**
- **A sound card** (built-in works, but a USB audio interface is recommended for cleaner audio when connected to a radio)
- **A radio** (any voice-capable amateur radio — HF, VHF, or UHF) if you want to go on the air. You can test the app without a radio by playing and recording audio through your sound card.
- **Operating system**: Linux, macOS, or Windows. The GUI requires a display server (X11 or Wayland on Linux, or the native desktop on macOS/Windows).

### 3.2 Installing from Source

Clone the repository and install in a virtual environment:

```bash
git clone https://github.com/bucknova/Open-SSTV.git
cd Open-SSTV
python -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -e .
```

This installs the `sstv-app` command (the GUI), plus `sstv-app-encode` and `sstv-app-decode` (the command-line tools).

**Dependencies installed automatically**: PySide6, NumPy, SciPy, sounddevice (PortAudio), PySSTV, Pillow, pyserial, platformdirs, tomli-w.

On Linux you may also need PortAudio development headers. On Debian/Ubuntu:

```bash
sudo apt install libportaudio2
```

To launch the GUI:

```bash
sstv-app
```

Or run as a Python module:

```bash
python -m sstv_app
```

### 3.3 Command-Line Tools

Two headless CLI tools are available for use without the GUI, useful on headless machines, Raspberry Pi devices, or in automated scripts:

- `sstv-app-encode` — Render an image to an SSTV WAV file
- `sstv-app-decode` — Decode an SSTV WAV file back into an image

These tools do not require PySide6 or a display and can run in any terminal.

---

## 4. Quick Start

1. **Launch the app**: Run `sstv-app` from a terminal.
2. **Set your callsign**: Go to **File > Settings**, open the **Radio** tab, and enter your callsign in the Callsign field. Click OK.
3. **Choose your audio devices**: In **File > Settings > Audio**, select the input device your radio feeds audio into and the output device that feeds audio to your radio. If you are just testing, leave both on "System default."
4. **To receive**: Click **Start Capture** on the right-side Receive panel. The app will listen for SSTV signals. When it detects a VIS header, it will automatically identify the mode and begin decoding the image line by line.
5. **To transmit**: Click **Load Image** on the left-side Transmit panel, pick an image, select an SSTV mode from the dropdown, and click **Transmit**. If rig control is connected, the app will key your PTT automatically.

---

## 5. How a Typical SSTV QSO Works

A QSO (amateur radio contact) on SSTV follows a pattern similar to a voice contact, but images replace spoken exchanges. Here is the typical flow:

**1. Tune to an SSTV frequency.** The most popular calling frequencies are 14.230 MHz USB (20 m) and 3.845 MHz LSB (75 m). On VHF, 145.500 MHz FM is sometimes used.

**2. Listen first.** Before transmitting, listen for a minute or two to make sure the frequency is clear. Start a capture in Open-SSTV and watch for incoming signals. The waterfall or status bar will show activity.

**3. Call CQ.** When the frequency is clear, prepare an image with your callsign and "CQ" (the general call for any station). Open-SSTV's built-in CQ template places text like "CQ CQ CQ DE [YOURCALL] K" onto your image automatically. Select your SSTV mode (Robot 36 is the most common for quick exchanges), then click Transmit. The transmission takes 36 seconds for Robot 36, about 110 seconds for Scottie S1 or Martin M1.

**4. Wait for a reply.** After your CQ image finishes transmitting, switch back to receive and listen. An answering station will send an image addressed to your callsign with their callsign and a signal report.

**5. Exchange images.** When you receive a reply, use the Exchange template to send an image back with their callsign, your callsign, and a signal report (RST — typically "59" for a good signal). The exchange template fills in "{theircall} DE {mycall}" and "UR {rst}" automatically; you just type in their callsign and the report when prompted.

**6. Send 73.** To close the contact, send a final image with "73" (best regards) using the 73 template. The other station may send a 73 image back as well.

**7. Log the contact.** After the QSO, log the contact in your station logbook with the date, time (UTC), frequency, mode (SSTV), callsigns, and signal reports.

Throughout the QSO, each station takes turns transmitting and receiving. Robot 36 is preferred for quick exchanges because each image takes only 36 seconds. For higher-quality pictures (scenery, detailed QSL cards), operators may switch to Martin M1 or Scottie S1, which take longer but produce larger, sharper images.

---

## 6. The Main Window

When Open-SSTV launches, the main window is divided into three areas: a radio status toolbar across the top, a Transmit panel on the left, and a Receive panel on the right.

![Open-SSTV main window](docs/screenshots/main-window.png)

### 6.1 Radio Panel (Toolbar)

The toolbar at the top of the window shows your rig connection status and live radio information.

From left to right, the toolbar displays: a **Connect Rig / Disconnect** button, a **connection status indicator** ("Connected" in green, "Disconnected" in gray, or "Connection lost" in red), the **current frequency** displayed in MHz, kHz, or Hz (or "—" if no rig is connected), the **radio mode** (USB, LSB, FM, etc.), an **S-meter** bar (scaled from S0 to S9), and your **callsign** displayed prominently on the right side.

When a rig is connected, the app polls the radio once per second and updates the frequency, mode, and signal strength displays automatically.

### 6.2 Transmit Panel (Left)

The left side of the window is dedicated to preparing and sending SSTV images.

At the top is the **image preview area** (minimum 320x240 pixels), which shows either a placeholder message ("No image loaded") or the image you have loaded and edited.

Below the preview is the **QSO template bar** — a row of buttons for quickly overlaying callsign and exchange text onto your image. The default templates are CQ, Exchange, and 73. There is also a Clear Text button (to remove template overlays) and a gear icon to open the template editor.

Below the template bar are the controls: a **Mode dropdown** (showing the mode name, resolution, and approximate duration for each mode), a **Load Image** button (opens a file picker supporting PNG, JPEG, BMP, GIF, TIFF, and WebP), an **Edit Image** button (opens the image editor for cropping, rotating, and adding text), and the **Transmit** button which starts the transmission.

During transmission, the Transmit button is replaced by a **Stop** button, and a **progress bar** appears showing the percentage complete along with elapsed and total time in seconds.

A **status label** at the bottom displays messages about image loading, template application, and any errors.

### 6.3 Receive Panel (Right)

The right side of the window handles SSTV reception.

The layout mirrors the transmit side: an **image preview area** at the top that shows the image currently being decoded (or the most recently completed image), followed by control buttons and status information.

The controls include a **Start Capture / Stop Capture** toggle button, a **Clear** button (resets the decoder to hunt for a new VIS header), and a **Save Image** button (saves the most recently decoded image).

The **status label** shows the detected mode name, VIS code, and decode progress (for example, "Robot 36 — VIS 8 — 180/240 lines").

At the bottom is the **image gallery** — a horizontal strip of thumbnails showing up to 20 recently decoded images, with the newest on the left. Double-click a thumbnail to save it, or right-click for a context menu with "Save As..." and "Copy to Clipboard" options.

### 6.4 Menu Bar

The **File** menu contains **Settings** (opens the configuration dialog) and **Quit**.

The **Help** menu contains **About**, which shows the application name, version, and license.

The keyboard shortcut **Ctrl+S** saves the most recently decoded image (same as clicking Save Image on the Receive panel).

---

## 7. Receiving Images

### 7.1 Starting a Capture

Click **Start Capture** on the Receive panel. The app begins listening on your selected audio input device. The status label will show "Listening..." while the app watches for a VIS header.

Make sure your audio input device is set correctly in Settings (or that the system default device is receiving audio from your radio). The app expects audio at the configured sample rate (48 kHz by default, or 44.1 kHz).

### 7.2 Progressive Decode

When the app detects a valid VIS header in the incoming audio, it automatically identifies the SSTV mode and begins decoding. The status label updates to show the detected mode and VIS code — for example, "Robot 36 — VIS 8 — Decoding..."

The image appears in the preview area line by line as it is decoded. Undecoded lines appear as black rows. The status updates periodically with the count of decoded lines versus total lines (for example, "120/240 lines").

When decoding completes, the status shows "Decode complete" and the finished image is added to the gallery strip. The decoder then automatically resets to hunt for the next VIS header, so you can receive multiple images in succession without any manual intervention.

The app includes automatic **slant correction**: if the transmitting and receiving sound cards have slightly different clock rates (which is common and causes the decoded image to lean to one side), the decoder fits a line through the detected sync pulse positions and compensates for the drift. This produces a straight image even with imperfect clocks.

### 7.3 The Image Gallery

The gallery at the bottom of the Receive panel holds up to 20 thumbnails (160x120 pixels each) of recently decoded images, displayed newest-first (left to right). When a 21st image arrives, the oldest one is dropped.

Double-click any thumbnail to save it. Right-click a thumbnail to access a context menu with two options: "Save As..." (opens a file dialog) and "Copy to Clipboard" (copies the image to the system clipboard for pasting into other applications).

### 7.4 Saving Received Images

There are several ways to save a decoded image:

- Click the **Save Image** button on the Receive panel, which saves the most recently decoded image.
- Press **Ctrl+S** anywhere in the app.
- Double-click a gallery thumbnail.
- Right-click a gallery thumbnail and choose "Save As..."
- Enable **auto-save** in Settings (Images tab). When auto-save is on, every completed decode is saved automatically to your configured save directory. Files are named with the mode and a timestamp, for example `sstv_robot_36_20260413_143022.png`.

The default save directory is `~/Pictures/sstv_app`. You can change this in **File > Settings > Images**.

---

## 8. Transmitting Images

### 8.1 Loading an Image

Click **Load Image** on the Transmit panel. A file dialog opens supporting these formats: PNG, JPEG, BMP, GIF, TIFF, and WebP. After selecting an image, it appears in the preview area and the Edit Image and Transmit buttons become enabled.

The app will automatically resize your image to match the selected SSTV mode's native resolution when transmitting. You can also crop and resize it manually using the image editor.

### 8.2 Editing an Image

Click **Edit Image** to open the image editor dialog. The editor provides the following tools.

**Crop**: A yellow dashed rectangle appears over the image. Drag it to select a crop region, or type exact X, Y, width, and height values in the spin boxes on the right. The "Lock Aspect Ratio" toggle constrains the crop to the target mode's aspect ratio (typically 4:3). Click "Auto-fit Crop" to calculate the largest centered crop at the correct aspect ratio, then "Apply Crop" to commit the crop. "Reset" reverts to the original image.

**Transform**: Four buttons let you rotate left (90 degrees counterclockwise), rotate right (90 degrees clockwise), flip horizontally, and flip vertically.

**Text Overlays**: Enter text (such as your callsign) in the text field, set the font size (8 to 120 pixels), pick a color via the color picker, choose a position (Top Left, Top Center, Top Right, Center, Bottom Left, Bottom Center, or Bottom Right), and click "Add Text." You can stack multiple overlays — each one appears in the overlay list. Select an overlay and click "Remove" to delete it. Text is rendered with a dark shadow for readability on any background.

An info label at the bottom of the editor shows the current image dimensions, the target dimensions for the selected mode, and the number of text layers. When you click OK, all overlays are burned into the image and it is resized to the mode's native dimensions with high-quality LANCZOS resampling.

### 8.3 Choosing an SSTV Mode

The Mode dropdown on the Transmit panel lists all supported modes. Each entry shows the mode name, resolution, and approximate transmission duration:

- **Robot 36** — 320x240, about 36 seconds
- **Martin M1** — 320x256, about 114 seconds
- **Scottie S1** — 320x256, about 110 seconds

Robot 36 is the fastest and most commonly used mode for casual QSOs. Martin M1 and Scottie S1 produce larger images with more detail but take roughly three times as long.

You can set a default TX mode in **File > Settings > Images** so that it is pre-selected each time you launch the app.

### 8.4 Using QSO Templates

QSO templates let you overlay standardized text (callsigns, signal reports, greetings) onto your image with a single click, speeding up on-air operation. The template bar sits between the image preview and the mode dropdown.

Click a template button (for example, "CQ") and a text overlay is applied to your image instantly. If the template contains placeholders that need your input — such as `{theircall}` for the other station's callsign — a small dialog pops up asking you to fill in the values.

Click **Clear Text** to remove template overlays and restore the original image. Click the **gear icon** to open the template editor for creating and modifying templates (see the Templates In Depth section below).

![QSO templates](docs/screenshots/qso-templates.png)

### 8.5 Sending the Transmission

Once your image is loaded (and optionally edited and templated), click **Transmit**. Here is what happens:

1. The image is encoded into SSTV audio samples for the selected mode.
2. If a rig is connected, the app keys PTT (press-to-talk) on your radio.
3. The app waits for the configured PTT delay (default 0.2 seconds) to allow the radio's relay to settle.
4. The audio is played through your selected output device.
5. The progress bar shows percentage complete and elapsed/total seconds.
6. When playback finishes, PTT is released.

To abort a transmission in progress, click **Stop**. The audio stops immediately, PTT is released, and the status shows "Transmission aborted."

If no rig control is connected (Manual mode), the app simply plays the audio and you are responsible for keying your radio manually — either by pressing your radio's PTT button or using VOX (voice-activated transmit).

---

## 9. SSTV Modes Reference

Open-SSTV supports three SSTV transmission modes. Every SSTV transmission begins with a VIS (Vertical Interval Signaling) header — a short sequence of tones that identifies the mode so the receiving station's software can decode it automatically.

| Mode | VIS Code | Resolution | Duration | Color System | Sync Position |
|------|----------|------------|----------|--------------|---------------|
| Robot 36 | 8 | 320 x 240 | ~36 s | Y/C (luminance + chroma) | Line start |
| Martin M1 | 44 | 320 x 256 | ~114 s | RGB (G, B, R order) | Line start |
| Scottie S1 | 60 | 320 x 256 | ~110 s | RGB (G, B, R order) | Before red channel (mid-line) |

**Robot 36** uses a luminance/chrominance color system similar to analog television. Each "super-line" encodes two image rows: the even row's luminance, a shared Cr (red chroma) component, the odd row's luminance, and a shared Cb (blue chroma) component. This chroma subsampling is what makes Robot 36 fast — it sends color information at half resolution. Robot 36 is the most popular mode worldwide for quick exchanges.

**Martin M1** is the most popular mode in Europe. It sends full RGB color (green, then blue, then red for each line) with a sync pulse at the start of each line. Each line takes about 146 ms for each color channel, resulting in higher color fidelity than Robot 36 but a longer total transmission time.

**Scottie S1** is the most popular mode in the United States. Like Martin M1, it sends full RGB, but the sync pulse sits in the middle of the line (between the blue and red channels) rather than at the start. Each line's layout is: green scan, blue scan, sync pulse, red scan.

All three modes use the same frequency-to-brightness mapping: 1500 Hz is black and 2300 Hz is white, with a linear scale between them. Sync pulses are at 1200 Hz.

---

## 10. Radio and Rig Control

Open-SSTV supports three rig control modes, configured in **File > Settings > Radio**.

### 10.1 Manual Mode (No Rig Control)

Select **"Manual (no rig control)"** in the Connection Mode dropdown. In this mode, the app does not communicate with your radio at all. You must key PTT yourself — either by pressing your radio's PTT switch, using a foot switch, or enabling VOX on your radio so it transmits whenever it hears audio.

The Radio Panel toolbar will show "Disconnected" and the frequency, mode, and S-meter displays will show placeholder values. This mode is useful for simple setups, testing, or if your radio doesn't support CAT control.

### 10.2 rigctld (Hamlib Daemon)

Select **"rigctld (Hamlib daemon)"** for the most flexible rig control option. This uses Hamlib's `rigctld` daemon, which supports hundreds of radio models and communicates over a TCP socket.

**How it works**: The `rigctld` daemon runs as a separate process (either started manually or launched by Open-SSTV). It connects to your radio over a serial port and provides a network API. Open-SSTV connects to the daemon over TCP (default: localhost port 4532) and sends commands to read frequency, mode, and signal strength, as well as key and unkey PTT.

**Setup steps**:

1. Install Hamlib on your system. On Debian/Ubuntu: `sudo apt install hamlib-utils`. On macOS with Homebrew: `brew install hamlib`.
2. In the Settings dialog's Radio tab, select "rigctld (Hamlib daemon)."
3. Set the **Host** (default: `localhost`) and **Port** (default: `4532`).
4. Select your **Radio Model** from the dropdown. The list includes popular models from Icom, Kenwood, Yaesu, and Elecraft. If your model isn't listed, enter its Hamlib model number in the "Custom Model ID" field (consult `rigctld --list` for all models).
5. Set the **Serial Port** to the port your radio is connected to (for example, `/dev/cu.usbserial-1420` on macOS, `/dev/ttyUSB0` on Linux, or `COM3` on Windows).
6. Set the **Baud Rate** to match your radio's configuration (common values: 9600, 19200, 38400, 115200).
7. Optionally check **Auto-launch rigctld** to have the app start the daemon for you when you click Connect Rig.
8. Alternatively, click **Launch rigctld Now** to start the daemon immediately from the settings dialog. The status label will confirm if it started successfully. Use **Stop rigctld** to terminate it.
9. Click **Test rigctld Connection** to verify the connection. If successful, the dialog will display your radio's current frequency and mode.
10. Click OK to save settings.

Back in the main window, click **Connect Rig** on the toolbar. The status indicator should turn green and the frequency, mode, and S-meter will begin updating every second.

### 10.3 Direct Serial (Built-In CAT)

Select **"Direct Serial (built-in)"** to have Open-SSTV communicate directly with your radio over serial, without needing rigctld or Hamlib installed. This is a simpler setup but supports a smaller set of radio families.

**Supported protocols**:

- **Icom CI-V** — For Icom radios (IC-7300, IC-705, IC-7100, IC-9700, IC-7200, IC-7600, IC-7000, IC-7851, IC-R8600, and others). Provides full CAT control: frequency, mode, PTT, and S-meter.
- **Kenwood** — For Kenwood radios (TS-590SG, TS-890S, TS-480, TS-2000) and Elecraft radios (K3, KX3, KX2, K4). Full CAT control.
- **Yaesu** — For modern Yaesu radios (FT-991A, FT-891, FT-710, FTDX10, FTDX101, FT-950). Full CAT control.
- **PTT Only (DTR/RTS)** — For any radio. Toggles the DTR or RTS serial control line to key PTT. No frequency, mode, or S-meter readback. This is the simplest option if you only need automated PTT.

**Setup steps**:

1. In Settings > Radio, select "Direct Serial (built-in)."
2. Choose the **Protocol** from the dropdown.
3. Select the **Serial Port** (the dropdown lists detected ports; you can also type a custom path).
4. Set the **Baud Rate** to match your radio.
5. For **Icom CI-V**: Choose your radio from the CI-V Address Preset dropdown (this auto-fills the hex address), or enter the address manually. Common addresses: IC-7300 = 0x94, IC-705 = 0xA4, IC-9700 = 0xA2.
6. For **PTT Only**: Choose the **PTT Line** — either DTR or RTS, depending on your cable wiring.
7. Click **Test Serial Connection** to verify. A successful test displays the radio's frequency and mode (or just confirms PTT line toggling for PTT-only mode).
8. Click OK, then click **Connect Rig** in the main window.

### 10.4 Supported Radios and Protocols

The following radios have been tested or have built-in presets in the settings dialog.

**Via Direct Serial (Icom CI-V)**: IC-7300, IC-7610, IC-9700, IC-705, IC-7100, IC-7200, IC-7600, IC-7000, IC-7851, IC-R8600.

**Via Direct Serial (Kenwood)**: TS-590SG, TS-890S, TS-480, TS-2000, and Elecraft K3, KX3, KX2, K4.

**Via Direct Serial (Yaesu)**: FT-991A, FT-891, FT-710, FTDX10, FTDX101, FT-950.

**Via rigctld (Hamlib)**: Hundreds of additional models — any radio supported by Hamlib. Run `rigctld --list` in a terminal for the full list. Common models included in the dropdown: IC-7300 (model 3073), TS-590SG (model 2028), FT-991A (model 1036), FT-817/818 (model 1020), plus a Hamlib Dummy model (1) for testing.

**PTT-only serial**: Works with any radio that has a serial PTT interface — no CAT protocol required.

### 10.5 PTT Delay

When transmitting with rig control, the app keys PTT and then waits for a configurable delay before playing audio. This gives your radio's transmit relay time to engage so the beginning of the SSTV signal is not clipped.

The default PTT delay is **0.2 seconds** (200 ms). You can adjust it from 0 to 2 seconds in 0.05-second steps in **File > Settings > Radio**. If you find that the first fraction of a second of your transmission is missing at the receiving end, try increasing this value.

---

## 11. Audio Setup

### 11.1 Choosing Audio Devices

Open **File > Settings > Audio** to configure audio input and output devices.

**Input Device**: Select the audio device that receives audio from your radio. This is used for SSTV reception. If your radio is connected via a USB audio interface (such as a SignaLink, DigiRig, or a built-in USB codec on modern radios like the IC-7300), select that device. If you leave it on "System default," the app uses your operating system's default input device.

**Output Device**: Select the audio device that feeds audio to your radio. This is used for SSTV transmission. Select the same interface you use for digital modes. Leave it on "System default" to use the system's default output.

The device list is refreshed each time you open the Settings dialog, so hot-plugged USB devices will appear.

### 11.2 Sample Rate

Choose between **48,000 Hz** (48 kHz) and **44,100 Hz** (44.1 kHz). The default is 48 kHz, which is the standard for SSTV applications and matches the encoder's native sample rate. Most USB audio interfaces support both rates. Use 44.1 kHz only if your audio device does not support 48 kHz.

### 11.3 Input and Output Gain

Two sliders let you adjust software gain for input (receive) and output (transmit), ranging from 0% to 200%. The default is 100% (unity gain — no change). These are digital gain controls applied before decoding (input) and after encoding (output). They do not change your sound card's hardware volume.

Increase input gain if the received signal is too quiet for reliable decoding. Decrease it if the audio is clipping. For output, adjust gain to set an appropriate drive level to your radio — too much gain causes distortion and splatter; too little causes a weak signal.

### 11.4 Audio Routing Tips

**Simplest setup (VOX)**: Connect your computer's audio output to your radio's microphone input and your radio's speaker/headphone output to your computer's audio input. Enable VOX on your radio. The app plays SSTV audio, VOX keys the radio, and you receive by recording the radio's audio output.

**USB audio interface**: A dedicated USB interface (SignaLink USB, DigiRig Mobile, or a radio with built-in USB audio like the IC-7300) provides cleaner audio and avoids RF feedback. Select the interface as both input and output device in the app's settings.

**Virtual audio cables (software loopback)**: On Linux you can use PulseAudio or PipeWire loopback modules. On macOS, tools like BlackHole or Soundflower can route audio between applications. On Windows, Virtual Audio Cable (VAC) serves the same purpose. This is useful if you are piping audio between Open-SSTV and another application such as SDR software.

---

## 12. Settings Reference

Open the settings dialog via **File > Settings** (or the menu shortcut). It has three tabs.

### 12.1 Audio Tab

| Setting | Description | Default |
|---------|-------------|---------|
| Input Device | Audio input for RX (list of detected devices, or "System default") | System default |
| Output Device | Audio output for TX (list of detected devices, or "System default") | System default |
| Sample Rate | 44100 Hz or 48000 Hz | 48000 Hz |
| Input Gain | Digital gain for received audio, 0–200% | 100% |
| Output Gain | Digital gain for transmitted audio, 0–500% | 100% |

> **IC-7300 note:** The radio has its own audio input level control at **Menu → SET → Connectors → USB MOD Level**. The factory default (around 50%) is fine for most setups — you generally don't need to change it. Adjust the app's Output Gain slider and your computer's system output volume first; only touch USB MOD Level if you are recalibrating from scratch.

![Audio settings tab](docs/screenshots/settings-audio.png)

### 12.2 Radio Tab

| Setting | Description | Default |
|---------|-------------|---------|
| Connection Mode | "Manual (no rig control)", "Direct Serial (built-in)", or "rigctld (Hamlib daemon)" | Manual |
| Serial Protocol | Icom CI-V, Kenwood, Yaesu, or PTT Only (DTR/RTS) | PTT Only (DTR/RTS) |
| Serial Port | Serial port path (editable dropdown listing detected ports) | (empty) |
| Baud Rate | 4800, 9600, 19200, 38400, 57600, or 115200 | 9600 |
| CI-V Address | Icom CI-V hex address (shown only for Icom protocol) | 0x94 |
| CI-V Preset | Dropdown of common Icom radios to auto-fill the CI-V address | (none) |
| PTT Line | DTR or RTS (shown only for PTT-only mode) | DTR |
| rigctld Host | Hostname or IP address of the rigctld daemon | localhost |
| rigctld Port | TCP port for rigctld | 4532 |
| Radio Model | Hamlib model for rigctld (dropdown of common models) | (none) |
| Custom Model ID | Numeric Hamlib model ID for unlisted radios | 0 |
| Auto-launch rigctld | Start the daemon automatically when connecting | Off |
| PTT Delay | Seconds to wait after keying PTT before playing audio (0–2 s) | 0.2 s |
| Callsign | Your amateur radio callsign (displayed in the toolbar and used in templates) | (empty) |

![Radio settings tab](docs/screenshots/settings-radio.png)

### 12.3 Images Tab

| Setting | Description | Default |
|---------|-------------|---------|
| Default TX Mode | SSTV mode pre-selected in the Transmit panel mode dropdown | Martin M1 |
| Auto-save | Automatically save every decoded image to the save directory | Off |
| Save Directory | Folder for saved and auto-saved images (browse button to pick) | ~/Pictures/sstv_app |

![Images settings tab](docs/screenshots/settings-images.png)

---

## 13. QSO Templates In Depth

### 13.1 How Templates Work

QSO templates are named presets that overlay text onto your transmit image with one click. Each template has a name (shown as a button label) and one or more text overlays. Each overlay specifies the text content (which may include placeholders like `{mycall}`), a position on the image, a font size, and a color.

When you click a template button, the app resolves any placeholders, prompts you for any values it needs (like the other station's callsign), and then burns the text onto the image. The original un-overlaid image is preserved internally, so clicking "Clear Text" removes the overlays and restores the clean image.

### 13.2 Built-In Templates

Open-SSTV ships with three templates designed for a standard SSTV QSO:

**CQ** — Places "CQ CQ CQ DE {mycall} {mycall} K" at the bottom center of the image in white, 24pt text. Your callsign is filled in automatically from your settings. This is used to call for any station.

**Exchange** — Two overlays: "{theircall} DE {mycall}" at the top center (24pt white) and "UR {rst} {date}" at the bottom center (20pt cream/off-white). When you click this button, a dialog asks you to enter the other station's callsign and a signal report (defaulting to "59"). The date is filled in automatically as the current UTC date.

**73** — Places "{theircall} 73 DE {mycall} SK" at the bottom center (24pt white). You are prompted for the other station's callsign. "73" means "best regards" and "SK" means "end of contact."

### 13.3 Creating Custom Templates

Click the **gear icon** on the template bar (or open the template editor from within the settings) to create and edit templates.

The template editor has a list of templates on the left and editing controls on the right. Click **Add** to create a new template, give it a name, then add one or more text overlays. For each overlay, set the text, position (7 anchor points from Top Left to Bottom Right), size (8 to 72 pixels), and color. A live preview at the bottom of the dialog shows how the overlays will look on a dark background.

You can use placeholders in overlay text. The app substitutes them at render time. For example, an overlay with `{mycall} on {date}` would render as `W1ABC on 2026-04-13`.

Click OK to save your templates. They are persisted to a TOML file and available in future sessions.

### 13.4 Placeholder Reference

| Placeholder | Replaced With | Notes |
|-------------|---------------|-------|
| `{mycall}` | Your callsign from settings | Automatic — no prompt |
| `{theircall}` | The other station's callsign | You are prompted to enter this |
| `{rst}` | Signal report | You are prompted; defaults to "59" |
| `{date}` | Current UTC date (YYYY-MM-DD) | Automatic |
| `{time}` | Current UTC time (HH:MZ) | Automatic |

The quick-fill dialog remembers the last callsign you entered during a session, so you don't have to retype it for subsequent exchanges in the same QSO.

---

## 14. Image Editor Reference

The image editor (opened by clicking **Edit Image** on the Transmit panel) provides these tools:

**Crop**: Drag the yellow crop rectangle or enter coordinates manually. "Auto-fit Crop" computes the largest centered rectangle matching the target mode's aspect ratio. "Lock Aspect Ratio" constrains manual adjustments. "Apply Crop" commits the crop; "Reset" reverts to the original image.

**Rotate**: Rotate the image 90 degrees counterclockwise or clockwise.

**Flip**: Mirror the image horizontally or vertically.

**Text Overlays**: Add text at any of 7 positions (Top Left/Center/Right, Center, Bottom Left/Center/Right) with configurable size (8–120px) and color. Multiple overlays can be stacked. Text is rendered with a dark shadow outline for readability. The overlay list shows each entry as "text" followed by its size and position.

The info label shows: current image dimensions, target dimensions for the selected mode, and the number of text layers.

When you click OK, all overlays are composited onto the image, and the result is resized to the SSTV mode's native resolution using high-quality LANCZOS resampling.

---

## 15. Command-Line Interface

Open-SSTV includes two CLI tools that work without a graphical display. They are useful for scripting, Raspberry Pi deployments, and testing.

### 15.1 sstv-app-encode

Encode an image into an SSTV WAV file.

```
sstv-app-encode IMAGE -o OUTPUT.wav --mode MODE [--sample-rate RATE]
```

**Arguments**:

- `IMAGE` — Path to the input image (any format Pillow can read: PNG, JPEG, BMP, TIFF, WebP, etc.)
- `-o, --output` — Path to the output WAV file (required)
- `--mode` — SSTV mode: `robot_36`, `martin_m1`, or `scottie_s1` (required)
- `--sample-rate` — Output sample rate in Hz (default: 48000)

**Example**:

```bash
sstv-app-encode my_photo.jpg --mode robot_36 -o output.wav
```

The output is a mono 16-bit PCM WAV file ready to be played through a sound card or piped to your radio.

### 15.2 sstv-app-decode

Decode an SSTV WAV file into an image.

```
sstv-app-decode INPUT.wav -o OUTPUT.png [--quiet]
```

**Arguments**:

- `INPUT.wav` — Path to the input WAV file (16-bit PCM recommended; stereo is supported and mixed to mono)
- `-o, --output` — Path to the output image file (format inferred from extension: PNG, JPEG, BMP, etc.) (required)
- `-q, --quiet` — Suppress the success message

**Example**:

```bash
sstv-app-decode recording.wav -o decoded_image.png
```

The decoder auto-detects the SSTV mode from the VIS header. If no valid VIS header is found, it exits with code 1 and prints an error to stderr.

---

## 16. Configuration Files

Open-SSTV stores its configuration in platform-appropriate directories using the XDG Base Directory Specification on Linux and equivalent paths on macOS and Windows.

**Settings file**: `~/.config/sstv_app/config.toml` (Linux). Contains all settings from the Settings dialog in TOML format. You can edit this file manually if desired, but it is easier to use the GUI.

**Templates file**: `~/.config/sstv_app/templates.toml`. Contains your QSO template definitions. Manually editing this file is supported but the template editor is recommended.

On macOS, these files are typically at `~/Library/Application Support/sstv_app/`. On Windows, they are at `%APPDATA%\sstv_app\`.

If either file is missing or empty, the app creates it with default values on first launch.

---

## 17. Troubleshooting

**No audio input / "Listening..." but nothing happens**

- Confirm the correct input device is selected in Settings > Audio.
- Check that your radio is feeding audio to the computer. Play a local SSTV WAV file through your speakers and point your microphone at them as a basic test.
- Make sure the sample rate in Settings matches your audio device's capability (try 48 kHz first, then 44.1 kHz).
- Increase the input gain slider if the audio level is very low.
- On Linux, verify that PulseAudio or PipeWire is routing the correct source. Use `pavucontrol` to check.

**Decode starts but the image is garbled, slanted, or has wrong colors**

- A moderate slant is normal and should be corrected automatically. Severe slant may indicate a very large sample rate mismatch between your sound card and the transmitting station.
- Garbled images can result from interference, weak signals, or multipath. Try again with a stronger signal.
- Wrong colors may indicate a mode mismatch. The app auto-detects mode via the VIS header, but if the VIS was corrupted by noise, the wrong decoder may be applied. Try decoding from a saved WAV file using `sstv-app-decode`.
- If you are receiving from a station using an unusual Robot 36 variant, the app auto-detects between the two common Robot 36 layouts (PySSTV single-line and canonical broadcast line-pair).

**Rig not connecting (rigctld)**

- Make sure `rigctld` is running. If you are not using auto-launch, start it manually: `rigctld -m MODEL -r /dev/ttyUSB0 -s 19200 &`
- Check that the host and port in Settings match where rigctld is listening (default: localhost:4532).
- Use the "Test rigctld Connection" button in Settings to diagnose. The error message will indicate whether it is a connection refused (daemon not running), timeout (wrong host/port), or command error (wrong model or baud rate).
- On Linux, make sure your user has permission to access the serial port: `sudo usermod -aG dialout $USER` (then log out and back in).

**Rig not connecting (direct serial)**

- Verify the serial port, baud rate, and protocol match your radio's settings.
- For Icom radios, ensure the CI-V address matches your radio's configuration (check your radio's menu settings for CI-V address). The most common address for the IC-7300 is 0x94.
- Some radios require you to enable the serial port or USB connection in their menu settings.
- On macOS, use `/dev/cu.*` ports, not `/dev/tty.*`.
- Use the "Test Serial Connection" button to diagnose.

**PTT not keying the radio**

- In Manual mode, the app does not control PTT. Use VOX on your radio or key it manually.
- With rig control, verify the connection is active (green "Connected" indicator on the toolbar).
- If using PTT-only serial mode, make sure you selected the correct PTT line (DTR or RTS) to match your cable wiring.
- Try increasing the PTT delay in Settings if the radio is keying but the beginning of the transmission is clipped (the relay may not have engaged yet when audio starts).
- Some radios require a specific setting to allow CAT-controlled PTT. Check your radio's menu.

**No audio output during transmit**

- Confirm the correct output device is selected in Settings > Audio.
- Increase the output gain if the level is very low.
- If using a USB audio interface, make sure it is selected as the output device — not your computer's built-in speakers.
- On macOS, check System Settings > Sound to ensure the correct output device is active.

**ALC doesn't move during Test Tone or transmission**

- Raise the Output Gain slider in **Settings → Audio**. Values above 100% (up to 500%) apply digital gain before the samples reach the sound card — push it up until ALC just starts to flicker on peaks, then back off slightly.
- Check the **macOS system output volume for the USB audio device**. macOS stores a per-device volume that can default below 100% and is separate from the master volume. Go to **System Settings → Sound → Output**, select your USB audio interface, and set the volume to 100%.
- Make sure the correct output device is selected in Settings → Audio. If "System default" is chosen but your radio interface is not the macOS default, the audio goes to the wrong device.
- For reference, the IC-7300's radio-side input level is at **Menu → SET → Connectors → USB MOD Level**. The factory default (~50%) works for most setups and you shouldn't need to change it if the steps above resolve the issue.

**Application crashes or freezes**

- File a bug report at [github.com/bucknova/Open-SSTV/issues](https://github.com/bucknova/Open-SSTV/issues) with your operating system, Python version, and the error traceback from the terminal.
- Try running from a terminal (`sstv-app`) to see error messages that may not appear in the GUI.
- Ensure you are running Python 3.11 or later and that all dependencies are up to date: `pip install -e . --upgrade`.

**Image saves as black or incomplete**

- This can happen if you save before decoding is complete. Wait until the status shows "Decode complete" before saving.
- If auto-save is on, the app saves immediately on decode completion, so partial images should not occur.

---

## 18. Glossary

**CAT (Computer Aided Transceiver)**: A protocol for controlling a radio from a computer, typically over a serial or USB connection. Allows reading and setting frequency, mode, and PTT state.

**CI-V**: Icom's proprietary CAT protocol, used on most Icom amateur radios. Communicates over a serial bus with addressable devices.

**CQ**: A general call inviting any station to respond. "CQ CQ CQ DE W1ABC" means "Calling any station, this is W1ABC."

**DE**: French for "from." Used in amateur radio to separate the called station's callsign from the calling station's. "W2XYZ DE W1ABC" means "W2XYZ, this is W1ABC."

**Hamlib**: An open-source library providing a standardized API for controlling amateur radios. The `rigctld` daemon provides a network interface to Hamlib.

**K**: An invitation for the other station to transmit. Used at the end of a transmission when you expect a reply.

**PTT (Press-to-Talk)**: The mechanism that switches a radio between receive and transmit. Can be a physical button, a foot switch, VOX, or CAT-controlled.

**QSO**: An amateur radio contact between two or more stations.

**rigctld**: The Hamlib rig control daemon. Runs as a background process and accepts commands over TCP, providing rig control to client applications.

**RST**: A signal report code. In SSTV, typically just the "RS" portion is used — Readability (1–5) and Strength (1–9). "59" means "perfectly readable, extremely strong signal."

**SK**: "End of contact." Signals that the QSO is finished.

**S-meter**: A signal strength meter on a radio receiver, calibrated in S-units (S0 to S9, then S9+10dB, S9+20dB, etc.). S9 corresponds to approximately -73 dBm on HF.

**SSTV (Slow-Scan Television)**: A method of sending still images over radio as audio tones. Each pixel is encoded as a frequency between 1500 Hz (black) and 2300 Hz (white).

**USB/LSB (Upper/Lower Sideband)**: Single-sideband modulation modes. SSTV on HF is typically transmitted on USB above 10 MHz and LSB below 10 MHz.

**VIS (Vertical Interval Signaling)**: An 8-bit header sent at the beginning of every SSTV transmission that identifies the SSTV mode. It consists of leader tones, a start bit, 7 data bits, a parity bit, and a stop bit.

**VOX (Voice-Operated Exchange)**: A circuit that automatically keys the transmitter when audio is present and unkeys when audio stops. Eliminates the need for a PTT switch.

**73**: Amateur radio shorthand for "best regards." Used as a closing salutation at the end of a QSO.
