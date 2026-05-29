# Future Work

Running list of design ideas and deferred enhancements that are worth capturing
but not scheduled for the current milestone.

---

## Weak-signal RX profile expansion

Captured 2026-04-16 during post-v0.2.2 discussion.

Today `rx_weak_signal_mode` only relaxes two VIS-detection thresholds (leader
fraction 0.40 → 0.25, min start-bit 20 ms → 15 ms). The toggle could grow into
a coordinated "Weak-signal RX profile" that bundles several complementary
relaxations. Ship as one toggle; if users ask for finer control later, split
into a preset with advanced overrides.

### Tier A — recommended additions (bundle under the existing toggle)

1. **Narrower RX prefilter bandwidth**
   - Current bandpass is wider than SSTV actually needs.
   - In weak-signal mode, tighten to roughly 1000–2400 Hz with a steeper
     skirt to reject out-of-band noise that degrades the Hilbert IF
     estimate.
   - Trade-off: slight edge ringing; that's why it's gated to weak mode.
   - Highest-impact single addition.

2. **Relaxed sync-tracking thresholds (`core/sync.py`)**
   - Widen the per-line sync-pulse search window.
   - Lower the sync-pulse presence threshold.
   - Natural companion to relaxed VIS: without it, a forgiving VIS hands
     off to a strict sync stage and produces slanted/black-barred images
     on exactly the signals weak-mode was meant to save.

3. **Noise blanker / impulse clipper at input**
   - Simple median-filter or peak-clip stage before Hilbert demod.
   - Stops single-sample spikes (QRN, ignition noise, lightning crashes)
     from smearing across multiple scanlines.
   - Weak HF conditions strongly correlate with QRN, so this pairs well
     with the mode even though it would also help on strong-signal days.

### Tier B — useful but probably standalone toggles later

- Heavier IF smoothing in the image-decode path (wider than current 2 ms
  boxcar); trades vertical resolution for noise immunity.
- Free-run sync fallback: after N lost sync pulses, hold the last known
  offset and drift at nominal rate instead of producing corrupt lines.
- Longer VIS search window — accumulate more buffered audio before
  declaring "no VIS," helps fading leaders.

### Explicitly do NOT bundle

- **Software AGC / auto gain.** SSTV demod is amplitude-invariant — the
  Hilbert transform extracts instantaneous frequency, not level. Software
  gain after the ADC recovers nothing and risks amplifying noise bursts.
  Any gain control that ships should stay as a standalone monitoring
  slider with a tooltip making this explicit. If gain matters, it matters
  at the radio / sound-card stage, not in software.
- **VIS parity relaxation.** One bit of parity; relaxing it roughly
  doubles false positives for zero real-signal gain.

### Measurement step before shipping

Run the existing test corpus (plus any newly captured weak-signal WAVs)
through both modes and compare successful-decode counts. Weak-signal
improvements are easy to *believe* and hard to *verify* without a
reference set. Lock in a before/after metric before promoting the change.

### Rough effort estimate

- Tier A, bundled under existing toggle: **3–5 days including tests.**
- Filter change needs unit tests verifying no distortion on strong
  signals plus regression on the corpus.
- Sync-threshold and noise-blanker stages: ~1 day each.

---

## Radio-filter setup info box in Settings

Captured 2026-04-16.

Add an informational block in Settings → Receive (near the weak-signal
toggle) that tells the user how to configure the **radio's own filters**
for best SSTV reception. Purely educational; no code logic depends on it.

Rationale: a lot of weak-signal trouble is actually upstream of the
decoder — IF/DSP filter too narrow in the radio, notch filter clipping
the 1100–2300 Hz SSTV band, noise-reduction artifacts mangling the
leader, or USB/LSB mode chosen wrong. Telling the user once, in context,
saves a lot of "the decoder is broken" reports that are really rig-setup
issues.

Content outline for the info box (draft — refine when implementing):

- Use **USB** for SSTV on HF (not LSB, not FM; FM only on 2 m/70 cm
  repeater-based SSTV).
- Set the radio's DSP / IF filter **wide enough to pass 1100–2300 Hz**
  — typically the "SSB wide" or 2.7–3.0 kHz filter. A narrow CW filter
  will clip the image.
- **Turn off or widen any auto-notch / notch filter.** Auto-notch
  chases the 1900 Hz leader tone and destroys VIS detection.
- **Disable noise-reduction (NR / DNR) for decode.** NR smears
  frequency transitions that the decoder relies on. It can mask audibility
  to your ear while making decode *worse*.
- Keep **RX audio level** in the upper part of the sound-card input range
  without clipping — a clean signal near full scale is better than a hot
  signal that clips.
- **AGC on the radio**: fast AGC can pump on CQ leaders; medium/slow is
  usually best for SSTV.

Implementation notes:

- `QGroupBox` with a `QLabel` using rich-text / small font; not a
  dialog — inline in Settings so users see it in context.
- Consider a collapsible "Tips for radio setup" disclosure so it's
  discoverable without cluttering the default view.
- Link out to the User Guide section if it grows beyond ~8 bullets.

Effort: **half a day** including wording review and a screenshot
refresh in the User Guide.

---

## RX-side callsign auto-detection (FSKID / CW ID)

Captured 2026-05-28 during v0.4 (logbook) planning.

Considered for v0.4.0 and **deferred**.  Worth revisiting if user
feedback shows operators frequently logging stations whose callsign
would have been recoverable from FSKID or CW ID embedded in the
audio.

### Why deferred from v0.4

- **Sender opt-in problem.**  FSKID is supported by MMSSTV, RX-SSTV,
  QSSTV, MultiPSK, ChromaPIX, MultiScan — but a meaningful fraction
  of operators disable it (adds 5–10 s to TX, redundant with voice
  ID or with the callsign already baked into the image by a
  template overlay).  Net effect: auto-population would succeed only
  for an unpredictable fraction of contacts.
- **CW ID needs its own DSP project.**  More universally *heard*
  than FSKID, but machine-decoding it means writing a CW decoder —
  a multi-day feature on its own.
- **Manual entry is honest.**  v0.4 ships with a 3-line callsign
  input.  No worse than every other ham logger.
- **Image-embedded callsign already exists.**  Template system lets
  the sender bake the callsign into the picture — the most
  universally readable form.

### What would change the calculus

- User feedback citing specific FSKID-active QSOs they'd want
  auto-logged.
- A well-tested FSKID decoder library appearing in the Python
  ecosystem (avoids writing from scratch).
- Decision to ship a CW decoder for other reasons (CW practice
  mode, beacon decoding) — then FSKID-style audio decode becomes
  the cheaper add-on.

### Related but distinct: TX-side CW ID injection

Separate question: should Open-SSTV inject a CW ID at the end of TX
for FCC §97.119 compliance?  Today operators handle this with voice
between transmissions, or rely on the template overlay to satisfy
the "machine-readable" interpretation.  Candidate for v0.5 or v0.6;
not blocking logbook work in v0.4.

### Rough effort estimate

- RX-side FSKID decoder + auto-fill wiring: **3–5 days** including
  corpus testing on real FSKID samples.
- RX-side CW ID decoder: **5–7 days** (Morse decoding is its own
  rabbit hole — variable WPM, noisy conditions).
- TX-side CW ID injection: **1–2 days** (much simpler than decode;
  just emit a CW tone train at end of PySSTV-generated audio).

---

## Forced-mode decode (skip VIS)

Captured 2026-05-29.  Originates from GitHub issue
[#20](https://github.com/bucknova/Open-SSTV/issues/20) opened by
`invalidop`.

User asks for the ability to manually pick an SSTV mode and start
decoding immediately, bypassing VIS leader detection.  Use case:
operator tunes into a transmission already in flight, missed the
leader, wants at least a partial decode of the bottom of the picture
rather than waiting for the next transmission.  Reference: the
[xdsopl/robot36 Android app](https://github.com/xdsopl/robot36) does
this.

### Why this is harder for us than for Robot36

Robot36 is a **single-mode app** — it only decodes Robot 36, so its
"force decode" UX is just one button.  Open-SSTV supports **22
modes** (Martin M1/M2/M3/M4, Scottie 1/2/DX/3/4, PD 50/90/120/160/
180/240/290, Robot 36/72, Wraase SC2-120/180, Pasokon P3/P5/P7).
Asking the user to pick the right one when they don't have VIS as
a reference is the real product question, not the DSP.

Most operators can't reliably distinguish modes by ear without
significant experience.  Real-world distribution helps:
**~90% of HF SSTV traffic is one of Martin M1, Scottie 1, Scottie 2,
PD 120, PD 180, or Robot 36**, which suggests a tiered UI (curated
common-modes section + "all modes" expander) is the right answer
over a flat 22-item dropdown — but even a curated list still
requires the operator to know enough to pick.

Picking wrong = visibly garbled output (no crash, just useless).
That's recoverable but it's a worse user experience than the
auto-VIS path, which is why we're being cautious about scoping
this.

### Design sketch (not committed to a release)

- **UI**: collapsible "Force Decode (advanced)" disclosure in the
  RX panel.  Sectioned `QComboBox`: common modes on top, separator,
  "All modes…" below.  Last-selected mode persists via config.
- **Decoder**: new `force_mode` constructor parameter on
  `Decoder` (mirroring the existing weak-signal flag pattern in
  `config/schema.py:87` → `ui/workers.py:1372` → `core/decoder.py:86`).
  When set, skip `detect_vis()`, jump straight into the mode-specific
  decoder via the existing `_PIXEL_DECODERS` dispatch.
- **DSP**: new `acquire_sync_without_vis(inst, fs, spec)` in
  `core/sync.py` that locks onto the first valid horizontal-sync
  pattern matching the chosen mode's `spec.sync_pulse_ms` /
  `spec.line_time_ms` parameters.  First few decoded lines may be
  visually skewed until sync stabilizes — acceptable for the "at
  least a partial decode" use case.

### Why we're not scheduling it yet

The DSP work is achievable and the UI sketch above would work.
What we don't have yet is data on how often the use case actually
arises for operators using Open-SSTV in the field.  Auto-VIS
already handles the common case (CQ heard from the start) cleanly.
Forced-mode is for the catch-mid-transmission edge case.  Before
committing implementation time, we want to see how the v0.4
logbook + v0.5 gallery rollouts shake out and whether user feedback
explicitly asks for this.

### What would change the calculus

- Multiple users (not just one) requesting forced-mode in issues
  or testing feedback.
- Decision to ship the weak-signal RX profile expansion (also in
  this doc) as a themed "Reception improvements" release — forced-
  mode would slot in naturally alongside it.
- A user-contributed PR with the DSP work largely done would
  short-circuit the wait.

### Out of scope even if/when this lands

- **Sync-pulse heuristic auto-suggest**: a "Suggest mode" button
  that analyzes 3–5 s of audio and pre-selects the most likely
  mode.  Different modes have distinguishable sync timings (Martin
  4.862 ms vs Scottie 9 ms vs PD 20 ms), so it's genuinely doable.
  Adds ~2–3 days on top of the base feature and requires exposing
  sync data from per-mode decoders (currently computed and
  discarded).  Capture as a separate enhancement if forced-mode
  ships and users want it.
- **Parallel-decode top-5 candidates**: "just press the button, app
  picks the cleanest output."  CPU-heavy on Pi-class hardware and
  risks locking up older Windows laptops.  Not shipping.
- **Robot36-style scrolling-up display**: pure presentation, can be
  added later without DSP changes.

### Rough effort estimate (when we do tackle it)

- DSP (`acquire_sync_without_vis` + per-mode template-match tuning):
  ~2 days.
- Decoder `force_mode` path + RxWorker plumbing: ~1.5 days.
- RX panel UI + config wiring: ~1 day.
- Test corpus (mid-transmission WAVs — need to record or generate):
  ~0.5 day.
- Tests + docs + cross-OS smoke: ~2 days.
- **Total: ~7 days** when prioritized.
