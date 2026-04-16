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
