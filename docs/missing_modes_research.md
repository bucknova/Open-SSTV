# Missing SSTV modes — research notes

Research compiled 2026-04-14. This document is the single reference for implementing the nine SSTV modes not yet in `src/open_sstv/core/modes.py`. Read [Implementation notes](#implementation-notes) before writing any code.

---

## Summary table

| Mode | VIS (7-bit dec) | VIS (hex, 7-bit) | 8-bit w/ parity | Dimensions | Total time | Color system | Base architecture |
|---|---|---|---|---|---|---|---|
| Robot 8 BW | 2 | 0x02 | 0x82 | 160×120 | ~8.0 s | Grayscale (luma only) | GrayscaleSSTV subclass; no PySSTV class |
| Robot 12 BW | 6 | 0x06 | 0x06 | 160×120 | 12.0 s | Grayscale (luma only) | GrayscaleSSTV subclass; no PySSTV class |
| Robot 24 | 4 | 0x04 | 0x84 | 160×120 | 24.0 s | YCbCr 4:2:2 (full chroma each line) | Same as Robot 72, different constants |
| Robot 72 | 12 | 0x0C | 0x0C | 320×240 | 72.0 s | YCbCr 4:2:2 (full chroma each line) | Different from Robot 36; no PySSTV class |
| Martin M3 | 36 | 0x24 | 0x24 | 320×128 | ~57.1 s | RGB (G→B→R) | Same code path as M1; different height only |
| Martin M4 | 32 | 0x20 | 0xA0 | 160×128 | ~29.0 s | RGB (G→B→R) | Same code path as M2; different height only |
| Scottie S3 | 52 | 0x34 | 0xB4 | 320×128 | ~54.8 s | RGB (G→B→R) | Same code path as S1; different height only |
| Scottie S4 | 48 | 0x30 | 0x30 | 160×128 | ~35.5 s | RGB (G→B→R) | Same code path as S2; different height only |
| PD-50 | 93 | 0x5D | 0xDD | 320×256 | ~49.7 s | YCbCr line-pair (Y0+Cr+Cb+Y1) | Same code path as PD-90; different pixel time |

**Note on 8-bit VIS format:** The 7-bit VIS data code is transmitted with an even parity bit in the MSB position. The 8-bit form shown above is what appears in QSSTV source tables and what the VIS detector observes on the wire. The `vis_code` field in `ModeSpec` stores only the 7-bit value (the existing `detect_vis()` implementation strips the parity bit before lookup). Verify that the existing parity-strip logic handles all parity-bit patterns before adding these modes.

---

## Per-mode detail

### Robot 8 BW

**VIS code:** 7-bit = 2 (0x02); 8-bit with even parity = 0x82.
Confirmed by: PySSTV `grayscale.py` (`VIS_CODE = 0x02`), Oros42/SSTV_Robot_encoder.c source.

**Image dimensions:** 160×120 pixels (4:3 aspect ratio).

**Color system:** Grayscale (single luma channel only). Frequency maps linearly: 1500 Hz = black, 2300 Hz = white. No chroma channels.

**Line timing parameters:**

Two slightly different values appear in the literature:

| Parameter | BruXy SSTV Handbook table | PySSTV / Oros42 encoder |
|---|---|---|
| Sync pulse (1200 Hz) | 10.0 ms | 7.0 ms |
| Scan line (pixel data) | 56.0 ms | 60.0 ms |
| Total per line | 66.0 ms | 67.0 ms |
| Image total (120 lines) | 7.920 s | 8.040 s |

The BruXy handbook table gives `sync=10, scan=56` (line = 66 ms, total = 7.92 s). PySSTV `grayscale.py` and the Oros42 encoder define `SYNC=7, SCAN=60` (line = 67 ms, total = 8.04 s). Both round to "8 seconds." The Oros42 source (`SSTV_Robot_encoder.c`) is closer to actual Robot Research Corporation hardware timings; PySSTV uses the same constants. **Recommended values for implementation: sync = 7.0 ms, scan = 60.0 ms** (consistent with the encoder most implementations build on).

Pixel time = 60.0 ms / 160 px = 0.375 ms/px.

**Total image transmission time:** ≈ 8.04 s (67 ms × 120 lines).

**Frequency mapping:** 1200 Hz sync; pixel data 1500–2300 Hz (same as all SSTV modes).

**Architectural similarity:** Pure grayscale — no chroma channels at all. Simpler than Robot 36. Can be implemented as a `GrayscaleSSTV` subclass (if we model PySSTV's class hierarchy) or a separate decode path that skips all YCbCr conversion. The sync position is LINE_START.

**Gotchas:** No porch between sync and pixel data in the grayscale Robot BW modes — the sync pulse is immediately followed by the scan line. This differs from all color Robot modes (which have a 3 ms sync porch). Do not assume a universal porch constant across Robot family modes.

**PySSTV status:** PySSTV `grayscale.py` has `Robot8BW` with `VIS_CODE=0x02`, `WIDTH=160`, `HEIGHT=120`, `SYNC=7`, `SCAN=60`. **PySSTV already implements this mode for TX.** The class can be used directly for encoding. No decoder dispatch exists in the current codebase.

---

### Robot 12 BW

**VIS code:** 7-bit = 6 (0x06); 8-bit with even parity = 0x06 (two 1-bits → even → parity bit = 0).
This value is inferred from the Robot BW series pattern (R8BW=2, R24BW=10 from PySSTV) and cross-checked against the SSTV handbook mode list (sstv_05.pdf). No PySSTV class exists for this mode. Treat as moderately uncertain — flag if a decoder encounter reveals a different VIS.

**Image dimensions:** 160×120 pixels.

**Color system:** Grayscale (single luma channel). Same frequency mapping as Robot 8 BW.

**Line timing parameters:**

| Parameter | Value |
|---|---|
| Sync pulse (1200 Hz) | 7.0 ms |
| Scan line (pixel data) | 93.0 ms |
| Total per line | 100.0 ms |
| Image total (120 lines) | 12.000 s |

Pixel time = 93.0 ms / 160 px = 0.58125 ms/px. No porch between sync and scan (same as Robot 8 BW).

**Total image transmission time:** 12.000 s exactly.

**Frequency mapping:** 1200 Hz sync; pixel data 1500–2300 Hz.

**Architectural similarity:** Same structure as Robot 8 BW but with a longer scan time (slower pixel rate). Can be implemented with identical code to Robot 8 BW by changing only `SCAN` and `VIS_CODE` constants. Sync position: LINE_START.

**Gotchas:** Same "no porch" quirk as Robot 8 BW. The BruXy handbook table shows sync=7 ms, scan=93 ms for Robot BW 12, giving exactly 100 ms/line.

**PySSTV status:** No class exists in PySSTV for Robot 12 BW. PySSTV only has `Robot8BW` and `Robot24BW` in `grayscale.py`. A subclass of `Robot8BW` changing `VIS_CODE`, `SCAN`, and potentially `WIDTH`/`HEIGHT` would be needed for TX.

---

### Robot 24

**VIS code:** 7-bit = 4 (0x04); 8-bit with even parity = 0x84.
Sources: QSSTV `sstvparam.cpp` shows `R24` with `VIS 0x84` (8-bit); stripping the MSB parity bit gives 7-bit = 4. This is confirmed by the digigrup.org VIS table which lists "Robot Color 24" at code 4. Cross-check: PySSTV `grayscale.py` has `Robot24BW` at VIS 0x0A (decimal 10), not 4 — so 4 is reserved for the color variant.

**Disambiguation warning:** "Robot 24" is ambiguous. There are two modes:
- Robot 24 BW (grayscale, VIS=10/0x0A, 320×240, 24 s) — implemented in PySSTV as `Robot24BW`
- Robot 24 Color (YCbCr, VIS=4/0x04, 160×120, 24 s) — not in PySSTV

This research note covers **Robot 24 Color** (the one missing from the codebase).

**Image dimensions:** 160×120 pixels.

**Color system:** YCbCr 4:2:2. Unlike Robot 36 (which alternates Cb/Cr between lines in a 4:2:0 scheme), Robot 24 Color transmits **all three channels on every scan line** — Y, then Cr (R-Y), then Cb (B-Y) — same as Robot 72. This is the key architectural difference from Robot 36.

**Channel transmission order:** Y → Cr (R-Y) → Cb (B-Y) on every line.

**Line timing parameters:**

| Component | Duration | Frequency |
|---|---|---|
| Sync pulse | 9.0 ms | 1200 Hz |
| Sync porch | 3.0 ms | 1500 Hz |
| Y (luminance) scan | 88.0 ms | 1500–2300 Hz |
| Separator 1 | 4.5 ms | 1500 Hz |
| Porch 1 | 1.5 ms | 1900 Hz |
| Cr (R-Y) scan | 44.0 ms | 1500–2300 Hz |
| Separator 2 | 4.5 ms | 2300 Hz |
| Porch 2 | 1.5 ms | 1900 Hz |
| Cb (B-Y) scan | 44.0 ms | 1500–2300 Hz |
| **Total per line** | **200.0 ms** | |

Verification: 9+3+88+4.5+1.5+44+4.5+1.5+44 = 200.0 ms. At 120 lines: 200×120 = 24,000 ms = 24.000 s.

Y pixel time = 88.0 ms / 160 px = 0.55 ms/px.
Chroma pixel time = 44.0 ms / 160 px = 0.275 ms/px (same spatial resolution as Y, half the time per pixel — matches Robot 36 Y scan rate for the same 160 px width).

**Total image transmission time:** 24.000 s exactly.

**Frequency mapping:** 1200 Hz sync; 1500 Hz black, 2300 Hz white for pixel data.

**Architectural similarity:** Robot 24 Color reuses the same line structure as Robot 72, just with half the pixel count in each channel (160 pixels instead of 320) and the same absolute scan times as Robot 36 Y and C channels. The decoder logic needed is identical to Robot 72; only width, height, and VIS code differ.

**Gotchas:**
- Full 4:2:2 chroma on every line — do NOT apply the line-pair chroma averaging logic from Robot 36. Each line is complete and self-contained.
- The separator frequencies alternate: Separator 1 is 1500 Hz (same as between-channel gaps in Robot 36 "even" lines), Separator 2 is 2300 Hz. This matches the Robot 72 structure exactly.
- The "Robot 24" label in software tools sometimes refers to Robot 24 BW (320×240, grayscale), not this color mode. Always check VIS code.

**PySSTV status:** No class exists for Robot 24 Color. PySSTV `grayscale.py` has `Robot24BW` but it is at VIS 0x0A (10), not 0x04. A custom class inheriting from `ColorSSTV` (or analogous to `Robot72` with adjusted constants) would be needed. Given the existing Robot 36 custom encoder in the codebase, Robot 24 Color would also need a custom encoder.

---

### Robot 72

**VIS code:** 7-bit = 12 (0x0C); 8-bit with even parity = 0x0C (two 1-bits → even → parity bit = 0).
Confirmed by: QSSTV `sstvparam.cpp` (`R72, VIS 0x0C`), smolgroot/sstv-decoder `constants.ts` (VIS=12), `brainwagon/sstv-encoders/robot72.c` (VIS=12), smolgroot/sstv-decoder `ROBOT72.md`. Three independent sources agree.

**Image dimensions:** 320×240 pixels.

**Color system:** YCbCr 4:2:2. Full chroma (Cr and Cb) transmitted on **every scan line**. This is the critical difference from Robot 36 which uses 4:2:0 (alternating Cb/Cr between even/odd lines). Robot 72 sends Y + Cr + Cb sequentially on every line.

**Channel transmission order per line:** Y → Cr (R-Y/V) → Cb (B-Y/U).

**Line timing parameters:**

| Component | Duration | Frequency |
|---|---|---|
| Sync pulse | 9.0 ms | 1200 Hz |
| Sync porch | 3.0 ms | 1500 Hz |
| Y (luminance) scan | 138.0 ms | 1500–2300 Hz |
| Separator 1 | 4.5 ms | 1500 Hz |
| Porch 1 | 1.5 ms | 1900 Hz |
| Cr (R-Y / V) scan | 69.0 ms | 1500–2300 Hz |
| Separator 2 | 4.5 ms | 2300 Hz |
| Porch 2 | 1.5 ms | 1900 Hz |
| Cb (B-Y / U) scan | 69.0 ms | 1500–2300 Hz |
| **Total per line** | **300.0 ms** | |

Verification: 9+3+138+4.5+1.5+69+4.5+1.5+69 = 300.0 ms. At 240 lines: 300×240 = 72,000 ms = 72.000 s.

Y pixel time = 138.0 ms / 320 px = 0.43125 ms/px.
Chroma pixel time = 69.0 ms / 320 px = 0.215625 ms/px.

Multiple independent sources (smolgroot decoder constants.ts, brainwagon robot72.c, olgamiller SSTVEncoder2 Robot72.java) all agree on these values.

**Total image transmission time:** 72.000 s exactly.

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data range (same as all SSTV modes).

**Architectural similarity:** Robot 72 and Robot 24 Color use an identical line structure with different pixel counts and VIS codes. The Robot 36 decoder cannot be reused for Robot 72 — Robot 36 uses a 4:2:0 interlaced chroma scheme (requiring line-pair chroma averaging), while Robot 72 is fully self-contained per line. Robot 72 requires its own decode path (or can share a decoder with Robot 24 Color if that is parameterized).

**Gotchas:**
- **Do not reuse the Robot 36 decoder.** The 4:2:0 line-pairing logic in `_decode_robot36` and `_decode_robot36_line_pair` does not apply here.
- Separator 1 is at 1500 Hz; Separator 2 is at 2300 Hz. Both act as frequency markers between channels, not as sync pulses.
- No chroma averaging between adjacent lines — each line provides independent full-color data.
- Some sources confusingly refer to Robot 72 as a "4:2:2" mode even though the chroma is not sub-sampled spatially — Cr and Cb each have 320 samples at the same spatial rate as Y. The "4:2:2" label comes from the temporal efficiency compared to RGB modes.
- The brainwagon encoder confirms Y=138ms, V(R-Y)=69ms, U(B-Y)=69ms with sync=9ms and both separators at 4.5ms.

**PySSTV status:** No class exists in PySSTV for Robot 72. The existing codebase needed a custom Robot 36 encoder because PySSTV emits a non-standard single-line format; Robot 72 would similarly need a custom encoder (or a careful evaluation of whether any available PySSTV extension handles it).

---

### Martin M3

**VIS code:** 7-bit = 36 (0x24); 8-bit with even parity = 0x24 (two 1-bits → even → parity = 0).
Confirmed by multiple sources including the digigrup.org VIS code table, wa9tt.com SSTV methodology, and SSTV handbook sstv_05.pdf. Multiple web sources consistently cite "Martin M3 VIS = 36."

**Image dimensions:** 320×128 pixels. (The top 8 rows may be gray calibration bars in some implementations — the WAVECOM reference notes "top 8 lines are grayscale." Usable image = 320×120, but the protocol transmits 128 lines.)

**Color system:** RGB sequential, G→B→R transmission order (same as all Martin modes).

**Line timing parameters:** Martin M3 uses **identical sync, porch, and scan timing to Martin M1**. Only the line count changes (128 vs. 256).

| Parameter | Value | Notes |
|---|---|---|
| Sync pulse (1200 Hz) | 4.862 ms | Same as M1/M2 |
| Sync porch (1500 Hz) | 0.572 ms | Same as M1/M2; appears 4× per line |
| Channel scan (G, B, R) | 146.432 ms each | Same as M1 (320 px) |
| Line structure | sync + 4×porch + 3×scan | Same formula as M1 |
| **Total per line** | **4.862 + 4×0.572 + 3×146.432 = 446.446 ms** | |
| Lines | 128 | |
| **Image total** | **446.446 × 128 / 1000 = 57.145 s** | Sources cite ~57 s |

Pixel time per channel = 146.432 ms / 320 px = 0.4576 ms/px (same as M1).

**Total image transmission time:** ≈ 57.1 s (matches all cited sources).

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data (same as all SSTV modes).

**Architectural similarity:** Martin M3 is **Martin M1 with 128 lines instead of 256**. No timing constant changes — the only difference in `ModeSpec` is `height=128` and `vis_code=0x24`. A single `ModeSpec` entry and a new `Mode` enum value are the complete implementation for RX decode dispatch (since the decoder iterates line count from the spec). For TX, the PySSTV `MartinM1` class could be subclassed with `VIS_CODE=0x24` and `HEIGHT=128` if needed.

**Gotchas:** None beyond Martin family norms (G→B→R channel order, 4 inter-channel porches, sync at LINE_START).

**PySSTV status:** No `MartinM3` or `MartinM4` class in PySSTV (`color.py` has only `MartinM1` and `MartinM2`). A TX encoder would require a trivial `MartinM1` subclass overriding `VIS_CODE` and `HEIGHT`.

---

### Martin M4

**VIS code:** 7-bit = 32 (0x20); 8-bit with even parity = 0xA0 (one 1-bit → odd → parity bit = 1; so 1 + 0100000 = 10100000 = 0xA0).
Sources: digigrup.org VIS table, multiple SSTV mode lists consistently cite M4 VIS = 32.

**Image dimensions:** 160×128 pixels.

**Color system:** RGB sequential, G→B→R (same as all Martin modes).

**Line timing parameters:** Martin M4 uses **identical timing to Martin M2** (same sync, porch, scan per pixel) with 128 lines instead of 256.

| Parameter | Value |
|---|---|
| Sync pulse (1200 Hz) | 4.862 ms |
| Sync porch (1500 Hz) | 0.572 ms (×4) |
| Channel scan (G, B, R) | 73.216 ms each (same as M2, 160 px) |
| **Total per line** | **4.862 + 4×0.572 + 3×73.216 = 226.798 ms** |
| Lines | 128 |
| **Image total** | **226.798 × 128 / 1000 = 29.030 s** |

Pixel time per channel = 73.216 ms / 160 px = 0.4576 ms/px (same pixel rate as M1/M2/M3).

**Total image transmission time:** ≈ 29.0 s (matches cited ~29 s).

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data.

**Architectural similarity:** Martin M4 is **Martin M2 with 128 lines instead of 256**. All timing constants are identical to M2. Only `height=128` and `vis_code=0x20` differ.

**Gotchas:** Same as Martin M3. Note that the 8-bit parity form (0xA0) is different from what a naive "add 0x80" calculation would give — always compute parity from the 1-count of the 7-bit code.

**PySSTV status:** No `MartinM4` class in PySSTV. Subclass `MartinM2` with `VIS_CODE=0x20`, `HEIGHT=128`.

---

### Scottie S3

**VIS code:** 7-bit = 52 (0x34); 8-bit with even parity = 0xB4 (three 1-bits → odd → parity = 1; 1+0110100 = 10110100 = 0xB4).
Sources: digigrup.org VIS table cited by multiple aggregators. One search result cited Scottie S3 VIS = 52 explicitly.

**Image dimensions:** 320×128 pixels.

**Color system:** RGB sequential, G→B→R (same as all Scottie modes). Sync falls **BEFORE_RED** (Scottie's defining quirk — same as S1/S2/DX).

**Line timing parameters:** Scottie S3 uses **identical timing to Scottie S1** with 128 lines instead of 256.

| Parameter | Value | Notes |
|---|---|---|
| Sync pulse (1200 Hz) | 9.0 ms | Same as S1/S2/DX; occurs mid-line before Red channel |
| Inter-channel gap (1500 Hz) | 1.5 ms | Appears 6× per line (before G, after G, before sync, after sync=porch, before B? — follows Scottie structure) |
| Green scan | 136.74 ms | = 138.24 − 1.5 ms (PySSTV formula: SCAN = TOTAL − INTER_CH_GAP) |
| Blue scan | 136.74 ms | Same |
| Red scan | 136.74 ms | Same |
| **Total per line** | **9.0 + 6×1.5 + 3×136.74 = 428.220 ms** | |
| Lines | 128 | |
| **Image total** | **428.220 × 128 / 1000 = 54.812 s** | Sources cite ~55 s |

Note: In the existing codebase `_SCOTTIE_S1_SCAN_MS = 138.24 - 1.5 = 136.74`. Scottie S3 uses the same scan constant (same 320 px width). Pixel time = 136.74 ms / 320 px = 0.42731 ms/px.

**Total image transmission time:** ≈ 54.8 s (matches cited ~55 s).

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data.

**Architectural similarity:** Scottie S3 is **Scottie S1 with 128 lines instead of 256**. The `sync_position=SyncPosition.BEFORE_RED` is identical. Only `height=128` and `vis_code=0x34` differ. The existing decoder path for Scottie S1 handles S3 with no logic changes, only different iteration count from the `ModeSpec`.

**Gotchas:**
- Scottie's sync-before-red arrangement (not at line start) applies here exactly as in S1. See existing `SyncPosition.BEFORE_RED` handling.
- The top 8 lines may be grayscale calibration (same note as Martin M3/M4 — same manufacturer convention).

**PySSTV status:** No `ScottieS3` class in PySSTV (`color.py` has S1, S2, DX only). Subclass `ScottieS1` with `VIS_CODE=0x34`, `HEIGHT=128`.

---

### Scottie S4

**VIS code:** 7-bit = 48 (0x30); 8-bit with even parity = 0x30 (two 1-bits → even → parity = 0; 0+0110000 = 00110000 = 0x30).
Sources: digigrup.org VIS table.

**Image dimensions:** 160×128 pixels.

**Color system:** RGB sequential, G→B→R; sync BEFORE_RED (same Scottie family structure).

**Line timing parameters:** Scottie S4 uses **identical timing to Scottie S2** with 128 lines instead of 256.

| Parameter | Value |
|---|---|
| Sync pulse (1200 Hz) | 9.0 ms |
| Inter-channel gap (1500 Hz) | 1.5 ms (×6) |
| Green / Blue / Red scan | 86.564 ms each (= 88.064 − 1.5, same as S2 for 160 px) |
| **Total per line** | **9.0 + 6×1.5 + 3×86.564 = 277.692 ms** |
| Lines | 128 |
| **Image total** | **277.692 × 128 / 1000 = 35.545 s** |

Pixel time = 86.564 ms / 160 px = 0.54103 ms/px.

**Total image transmission time:** ≈ 35.5 s (sources cite ~36 s).

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data.

**Architectural similarity:** Scottie S4 is **Scottie S2 with 128 lines instead of 256**. Same decoder dispatch code; only height and VIS differ.

**Gotchas:** Same BEFORE_RED sync quirk as all Scottie modes.

**PySSTV status:** No `ScottieS4` class in PySSTV. Subclass `ScottieS2` with `VIS_CODE=0x30`, `HEIGHT=128`.

---

### PD-50

**VIS code:** 7-bit = 93 (0x5D); 8-bit with even parity = 0xDD (five 1-bits → odd → parity = 1; 1+1011101 = 11011101 = 0xDD).
Confirmed by: QSSTV `sstvparam.cpp` (PD50 with `VIS 0xDD`), olgamiller/SSTVEncoder2 `PD50.java` (`mVISCode = 93`), smolgroot/sstv-decoder (VIS=93 from the mode list). Three independent sources agree.

**Image dimensions:** 320×256 pixels (full image). Transmitted as 128 super-lines (line pairs), each covering 2 image rows, so the decoder produces a 320×256 output from 128 sync pulses.

**Color system:** YCbCr line-pair format. Each super-line carries: Y0 (odd image row) + Cr + Cb + Y1 (even image row). The format is identical to PD-90/120/160/180/240/290 — only the pixel time changes. Channel order: **Y0 → Cr → Cb → Y1** (same as all PD modes).

**Line timing parameters:**

| Component | Duration | Frequency |
|---|---|---|
| Sync pulse | 20.0 ms | 1200 Hz |
| Porch | 2.08 ms | 1500 Hz |
| Y0 (first image row) | 91.52 ms | 1500–2300 Hz |
| Cr (R-Y, shared between 2 rows) | 91.52 ms | 1500–2300 Hz |
| Cb (B-Y, shared between 2 rows) | 91.52 ms | 1500–2300 Hz |
| Y1 (second image row) | 91.52 ms | 1500–2300 Hz |
| **Total per super-line** | **20.0 + 2.08 + 4 × 91.52 = 388.160 ms** | |
| Super-lines | 128 (= 256 image rows / 2) | |
| **Image total** | **388.160 × 128 / 1000 = 49.685 s** | |

Pixel time = 91.52 ms / 320 px = 0.2860 ms/px.
Verification from olgamiller PD50.java: `mColorScanDurationMs = 91.52` — matches exactly.

**Total image transmission time:** ≈ 49.7 s (sources cite ~50 s).

**Frequency mapping:** 1200 Hz sync; 1500–2300 Hz pixel data. Identical range to all other SSTV modes.

**Architectural similarity:** PD-50 is **architecturally identical to PD-90** (and all other PD modes). The line-pair structure (one sync pulse → four channel scans covering two image rows) is the same. PD-90 uses `PIXEL=0.532 ms` for 320 px → channel = 320×0.532 = 170.24 ms. PD-50 uses `PIXEL=0.286 ms` for 320 px → channel = 320×0.286 = 91.52 ms. Only the pixel time (hence channel scan duration) and VIS code differ.

In `modes.py`, PD-50 would follow the identical pattern as PD-90:
```python
_PD_50_CHANNEL_SCAN_MS: float = 320 * 0.286  # 91.52 ms
```

And the `ModeSpec` would have `height=128` (256 image rows / 2, same convention as PD-90).

**Gotchas:**
- PD-50's actual image is 320×256, not 320×240. Some web sources quote 320×240; the olgamiller/SSTVEncoder2 `PD50.java` source explicitly has `mHeight = 256` — trust the implementation. QSSTV `sstvparam.cpp` shows 320 pixel width, 256 display lines, 128 data lines (super-lines).
- The PD modes' `height` in `MODE_TABLE` is stored as the number of super-lines (128 for PD-50), not the image height (256). The decoder reconstructs 256 image rows from 128 sync pulses. The `total_duration_s` property in `ModeSpec` computes correctly from `line_time_ms * height` since line_time_ms covers one super-line (two image rows).
- The channel order in the olgamiller PD base class is `addYScan(mLine) → addVScan(mLine) → addUScan(mLine) → addYScan(++mLine)`. The labels V (Cb) and U (Cr) in that code follow a different convention; the standard description is Y0→Cr→Cb→Y1. Verify against the existing PD-90 implementation in this codebase to ensure consistent labeling.

**PySSTV status:** No `PD50` class exists in PySSTV's `color.py` (which has PD90 through PD290 only). The olgamiller SSTVEncoder2 Android app does implement PD50, confirming the pixel time of 91.52 ms/channel. A PySSTV `PD50` TX encoder can be created by subclassing `PD90` and overriding `VIS_CODE=93` and `PIXEL=0.286`.

---

## Implementation notes

### Modes that extend existing code by parameter change only

All five of these modes need only a new `Mode` enum value and a `ModeSpec` entry. Zero new decoder logic required.

| Mode | Derived from | Changed constants |
|---|---|---|
| Martin M3 | Martin M1 | `vis_code=0x24`, `height=128` |
| Martin M4 | Martin M2 | `vis_code=0x20`, `height=128` |
| Scottie S3 | Scottie S1 | `vis_code=0x34`, `height=128` |
| Scottie S4 | Scottie S2 | `vis_code=0x30`, `height=128` |
| PD-50 | PD-90 | `vis_code=0x5D`, `_PD_50_CHANNEL_SCAN_MS = 91.52` |

For PD-50, also add the corresponding `ModeSpec` entry using `height=128` (128 super-lines = 256 image rows) and `line_time_ms = _PD_SYNC_MS + _PD_PORCH_MS + 4 * _PD_50_CHANNEL_SCAN_MS = 388.160 ms`.

### Modes needing new decoder logic

These modes require new decode functions because their per-line structure does not match anything already in `decoder.py`:

| Mode | Required new logic |
|---|---|
| Robot 8 BW | Grayscale decode path: no chroma, no porch, no YCbCr conversion |
| Robot 12 BW | Same as Robot 8 BW; different scan constant |
| Robot 24 (color) | 4:2:2 YCbCr: Y+Cr+Cb on every line, no line pairing; new decoder |
| Robot 72 | Same structure as Robot 24 Color but 320×240; new decoder |

Robot 8 and 12 BW are the simplest: single-channel (luma only), no porch, direct frequency-to-gray mapping. They can share a single `_decode_robot_bw(samples, fs, spec)` function parameterized on `spec`.

Robot 24 Color and Robot 72 are the same line structure (Y+sep+porch+Cr+sep+porch+Cb) and can share a single `_decode_robot72_style(samples, fs, spec)` function with 9 ms sync, 3 ms porch, then three channels separated by 4.5 ms + 1.5 ms gaps.

The existing Robot 36 decoder uses the line-pair format auto-detection (`median sync spacing` trick). The Robot 72 decoder should use the 300 ms sync spacing threshold to distinguish it from Robot 36 (150 ms). Robot 24 Color uses 200 ms spacing, so the three modes can be distinguished by their expected sync period alone, enabling straightforward dispatch before per-mode decode begins.

### PySSTV encoder coverage and gaps

| Mode | PySSTV class | TX encoder strategy |
|---|---|---|
| Robot 8 BW | `Robot8BW` in `grayscale.py` | Direct use (already a dependency) |
| Robot 12 BW | None | Subclass `Robot8BW`; change `VIS_CODE`, `HEIGHT` (same), `SCAN=93` |
| Robot 24 Color | None | Custom encoder; reuse Robot 72 encoder with different constants |
| Robot 72 | None | Custom encoder (same reason as Robot 36 — PySSTV lacks a class) |
| Martin M3 | None | Subclass `MartinM1`; override `VIS_CODE=0x24`, `HEIGHT=128` |
| Martin M4 | None | Subclass `MartinM2`; override `VIS_CODE=0x20`, `HEIGHT=128` |
| Scottie S3 | None | Subclass `ScottieS1`; override `VIS_CODE=0x34`, `HEIGHT=128` |
| Scottie S4 | None | Subclass `ScottieS2`; override `VIS_CODE=0x30`, `HEIGHT=128` |
| PD-50 | None | Subclass `PD90`; override `VIS_CODE=93`, `PIXEL=0.286` |

PySSTV `Robot8BW` outputs standard-format audio, so it can be used directly for TX without the workaround applied to Robot 36.

### Cross-source disagreements and chosen values

**Robot 8 BW sync/scan timing:** Two values exist:
- BruXy SSTV Handbook bw_mode table: sync=10 ms, scan=56 ms (line=66 ms, total=7.92 s).
- PySSTV `grayscale.py` and Oros42 SSTV_Robot_encoder.c: sync=7 ms, scan=60 ms (line=67 ms, total=8.04 s).
- **Chosen:** sync=7 ms, scan=60 ms. The PySSTV value is used by most active open-source implementations and is more widely tested.

**Robot 12 Color vs. BW:** The task description lists "Robot 12" as a missing mode. Research finds Robot 12 occurs in two forms: BW (grayscale, VIS=6) and Color (YCbCr). The color mode is obscure, poorly documented, and has a VIS code conflict (some sources assign it VIS=0, which is not a valid or reliable code). **Recommended approach:** implement Robot 12 as BW only (grayscale, VIS=6) which is what all major SSTV software supports. The color variant should be flagged as a low-priority research item requiring original Robot Research Corporation documentation. If Robot 12 Color is later confirmed as VIS=4, note that QSSTV assigns VIS=4 to Robot 24 Color — the two modes would collide, which is likely an error in one source.

**Robot 24 Color dimensions:** QSSTV `sstvparam.cpp` shows `R24` as 160 pixels wide, 120 lines. Some general web sources say 160×120 for "Robot 24." This is consistent with the MMSSTV user guide which lists "Robot 24: 24 sec, 160x120." Used: **160×120**.

**Scottie S3/S4 dimensions:** Some sources say 128 lines, others say 120 lines (with 8 being grayscale calibration). The WAVECOM table notes "top 8 lines are grayscale" for Scottie S3. The protocol transmits 128 lines; the display of the bottom 120 is a rendering choice. `ModeSpec.height` should be 128 (total transmitted lines), and the decoder should accept all 128 lines.

**PD-50 image height:** Multiple casual web sources cite 320×240 for PD-50. The authoritative implementation source (olgamiller SSTVEncoder2 `PD50.java`) sets `mHeight=256`, and QSSTV `sstvparam.cpp` shows `displayLines=256`. Use **256** (consistent with all other PD modes being 256 or 496 lines).

**Martin M3/M4 VIS parity:** VIS=32 (M4) yields odd parity → 8-bit = 0xA0 (not 0x20). Several web resources show "M4 VIS = 0x20" which is the 7-bit code. The 8-bit on-wire form is 0xA0. Ensure the VIS detector strips parity correctly before matching against `ModeSpec.vis_code` (which stores 7-bit codes in this codebase). This is how it already works for existing modes.

**Robot 24 Color vs. Robot 24 BW VIS collision:** QSSTV uses VIS=4 (0x84 8-bit) for Robot 24 Color. PySSTV's `Robot24BW` uses VIS=10 (0x0A). These are distinct and do not collide. But watch for sources that call any 24-second 160×120 Robot transmission "Robot 24" regardless of color encoding — always verify by VIS code.

---

## Sources cited

1. [PySSTV `grayscale.py` — dnet/pySSTV GitHub](https://github.com/dnet/pySSTV/blob/master/pysstv/grayscale.py) — Robot8BW and Robot24BW class definitions with VIS codes and timing constants.

2. [PySSTV `color.py` — dnet/pySSTV GitHub](https://github.com/dnet/pySSTV/blob/master/pysstv/color.py) — All color mode class definitions (MartinM1/M2, ScottieS1/S2/DX, Robot36, PD90-PD290, Pasokon, Wraase).

3. [QSSTV `sstvparam.cpp` — ON4QZ/QSSTV GitHub](https://github.com/ON4QZ/QSSTV/blob/main/src/sstv/sstvparam.cpp) — Complete SSTVTable with all mode parameters including R24 (VIS 0x84), R72 (VIS 0x0C), PD50 (VIS 0xDD). Most authoritative single source for VIS code verification.

4. [smolgroot/sstv-decoder `constants.ts`](https://github.com/smolgroot/sstv-decoder/blob/main/src/lib/sstv/constants.ts) — Robot 72 timing: sync=9ms, porch=3ms, Y=138ms, sep=4.5ms, porch=1.5ms, V=69ms, U=69ms. Confirms total = 300ms/line.

5. [smolgroot/sstv-decoder `ROBOT72.md`](https://github.com/smolgroot/sstv-decoder/blob/main/doc/ROBOT72.md) — Detailed Robot 72 line structure table including VIS=12, 320×240, 300ms/line.

6. [brainwagon/sstv-encoders `robot72.c`](https://github.com/brainwagon/sstv-encoders/blob/master/robot72.c) — Robot 72 encoder confirming VIS=12, Y=138ms, R-Y=69ms, B-Y=69ms.

7. [olgamiller/SSTVEncoder2 `PD50.java`](https://github.com/olgamiller/SSTVEncoder2/blob/master/app/src/main/java/om/sstvencoder/Modes/PD50.java) — PD-50: VIS=93, width=320, height=256, `mColorScanDurationMs = 91.52`.

8. [olgamiller/SSTVEncoder2 `PD.java` (base class)](https://github.com/olgamiller/SSTVEncoder2/blob/master/app/src/main/java/om/sstvencoder/Modes/PD.java) — PD family: sync=20ms, porch=2.08ms. Channel order: Y0→Cr→Cb→Y1.

9. [olgamiller/SSTVEncoder2 `Robot72.java`](https://github.com/olgamiller/SSTVEncoder2/blob/master/app/src/main/java/om/sstvencoder/Modes/Robot72.java) — Robot 72 VIS=12, 320×240, sync=9ms, Y=138ms, sep1=4.5ms, sep2=4.5ms.

10. [Oros42/SSTV_Robot_encoder `SSTV_Robot_encoder.c`](https://github.com/Oros42/SSTV_Robot_encoder/blob/master/SSTV_Robot_encoder.c) — Robot8BW: VIS=0x02, 160×120, sync=7ms, scan=60ms.

11. [BruXy/sstv-handbook `bw_mode.ctex`](https://github.com/BruXy/sstv-handbook/blob/master/sstv/bw_mode.ctex) — BW mode table with Robot B&W 8/12/24/36 sync and scan times (alternate values).

12. [Wikipedia — Slow-scan television](https://en.wikipedia.org/wiki/Slow-scan_television) — General mode overview; confirms Robot 72 is 240 lines, 72 s; Martin M3/M4 and Scottie S3/S4 not listed in detail.

13. [SSTV Transmission Modes — digigrup.org](https://www.digigrup.org/ccdd/sstv.htm) — VIS code table including Martin M3 (VIS=36), M4 (VIS=32), Scottie S3 (VIS=52), S4 (VIS=48), and all Robot modes.

14. [BruXy SSTV article — regnet.cz](https://bruxy.regnet.cz/web/hamradio/EN/a-look-into-sstv-mode/) — Martin M1, Robot 36, Robot 72 timing details (confirming Robot 72 Y=138ms, C=69ms).

15. [The development of the PD modes — classicsstv.com](https://www.classicsstv.com/pdmodes.php) — PD mode history and confirmation of Y0+Cr+Cb+Y1 line-pair structure; notes PD-50 uses Y, R-Y, B-Y encoding identical to other PD modes.

16. [deepwiki.com xdsopl/robot36 supported modes](https://deepwiki.com/xdsopl/robot36/6-supported-sstv-modes) — Confirms PD-50 VIS=93, total~50s; Robot 72 VIS=12 total~72s.
