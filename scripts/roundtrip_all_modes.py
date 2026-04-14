#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip encode→decode audit for all 17 supported SSTV modes.

For each mode:
  1. Creates a synthetic test image at the mode's native resolution.
  2. Encodes it to a WAV buffer in memory.
  3. Decodes the WAV buffer back to an image.
  4. Verifies the decoded image:
       - Correct size (width × actual_height)
       - Correct mode/VIS detected
       - Mean absolute pixel error ≤ MAX_MAE across all channels
         (SSTV is analog FM so some loss is expected and normal)

Runs without touching audio hardware or Qt. Output is a table of
PASS / FAIL results per mode with diagnostic details on any failure.
"""
from __future__ import annotations

import io
import sys
import traceback
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

sys.path.insert(0, str(__file__.replace("/scripts/roundtrip_all_modes.py", "/src")))

from sstv_app.core.encoder import encode
from sstv_app.core.decoder import decode_wav
from sstv_app.core.modes import MODE_TABLE, Mode

if TYPE_CHECKING:
    pass

# Maximum mean-absolute-error (0–255 scale) across all RGB channels that we
# still consider a PASS.  SSTV is lossy (FM quantization, filter ringing at
# line boundaries), so a small error budget is expected. 12 is generous —
# well-implemented modes in our decoder typically land below 6.
MAX_MAE: float = 12.0

# Sample rate for all tests.  48 kHz is the default and what the app uses.
FS: int = 48_000


def make_test_image(width: int, height: int) -> Image.Image:
    """Synthetic RGB image with a colour gradient and a grey diagonal stripe.

    Chosen to exercise all three colour channels non-trivially while keeping
    pixel values spread across the full 0-255 range, which stresses the
    FM-mapping at both ends of the SSTV frequency band.
    """
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    xs = np.linspace(0, 255, width, dtype=np.uint8)
    ys = np.linspace(0, 255, height, dtype=np.uint8)
    # R: horizontal ramp, G: vertical ramp, B: inverted horizontal ramp
    arr[:, :, 0] = xs[np.newaxis, :]
    arr[:, :, 1] = ys[:, np.newaxis]
    arr[:, :, 2] = (255 - xs)[np.newaxis, :]
    # Diagonal grey stripe so the image has high-frequency structure
    for i in range(min(width, height)):
        arr[i, i, :] = 200
    return Image.fromarray(arr, mode="RGB")


def encode_to_wav_bytes(image: Image.Image, mode: Mode) -> bytes:
    """Encode a PIL image for ``mode`` and return raw WAV bytes."""
    samples = encode(image, mode, sample_rate=FS)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(FS)
        wav.writeframes(samples.tobytes())
    return buf.getvalue()


def decode_from_wav_bytes(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Read WAV bytes and return (float64_mono_samples, sample_rate)."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wav:
        fs = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    return samples, fs


@dataclass
class Result:
    mode: Mode
    status: str       # "PASS" | "FAIL" | "ERROR"
    wav_size_kb: float = 0.0
    wav_duration_s: float = 0.0
    decoded_size: tuple[int, int] | None = None
    detected_mode: str | None = None
    mae: float | None = None
    note: str = ""


def audit_mode(mode: Mode) -> Result:
    spec = MODE_TABLE[mode]
    # Actual image dimensions (PD stores height=super-lines, so multiply back)
    actual_height = spec.height * 2 if spec.color_layout[0] in ("Y0",) else spec.height

    try:
        img = make_test_image(spec.width, actual_height)
    except Exception as exc:
        return Result(mode, "ERROR", note=f"make_test_image: {exc}")

    # --- Encode ---
    try:
        wav_bytes = encode_to_wav_bytes(img, mode)
    except Exception as exc:
        return Result(mode, "ERROR", note=f"encode: {exc}\n{traceback.format_exc()}")

    wav_size_kb = len(wav_bytes) / 1024
    wav_duration_s = (len(wav_bytes) - 44) / 2 / FS  # minus 44-byte WAV header

    # --- Decode ---
    try:
        samples, fs = decode_from_wav_bytes(wav_bytes)
        result = decode_wav(samples, fs)
    except Exception as exc:
        return Result(
            mode, "ERROR",
            wav_size_kb=wav_size_kb,
            wav_duration_s=wav_duration_s,
            note=f"decode: {exc}\n{traceback.format_exc()}",
        )

    if result is None:
        return Result(
            mode, "FAIL",
            wav_size_kb=wav_size_kb,
            wav_duration_s=wav_duration_s,
            note="decode_wav returned None (VIS not detected or mode not recognised)",
        )

    # --- Verify ---
    decoded = result.image
    expected_size = (spec.width, actual_height)

    if decoded.size != expected_size:
        return Result(
            mode, "FAIL",
            wav_size_kb=wav_size_kb,
            wav_duration_s=wav_duration_s,
            decoded_size=decoded.size,
            detected_mode=result.mode.value,
            note=(
                f"wrong output size: got {decoded.size}, "
                f"expected {expected_size}"
            ),
        )

    if result.mode != mode:
        return Result(
            mode, "FAIL",
            wav_size_kb=wav_size_kb,
            wav_duration_s=wav_duration_s,
            decoded_size=decoded.size,
            detected_mode=result.mode.value,
            note=f"wrong mode detected: got {result.mode.value}",
        )

    # Pixel fidelity check — resize original to decoded size for fair comparison
    orig_arr = np.array(img.convert("RGB").resize(decoded.size, Image.Resampling.LANCZOS), dtype=float)
    dec_arr = np.array(decoded.convert("RGB"), dtype=float)
    mae = float(np.mean(np.abs(orig_arr - dec_arr)))

    status = "PASS" if mae <= MAX_MAE else "FAIL"
    note = "" if status == "PASS" else f"MAE {mae:.1f} exceeds threshold {MAX_MAE}"

    return Result(
        mode, status,
        wav_size_kb=wav_size_kb,
        wav_duration_s=wav_duration_s,
        decoded_size=decoded.size,
        detected_mode=result.mode.value,
        mae=mae,
        note=note,
    )


def main() -> None:
    modes_in_order = [
        Mode.ROBOT_36,
        Mode.MARTIN_M1, Mode.MARTIN_M2,
        Mode.SCOTTIE_S1, Mode.SCOTTIE_S2, Mode.SCOTTIE_DX,
        Mode.PD_90, Mode.PD_120, Mode.PD_160,
        Mode.PD_180, Mode.PD_240, Mode.PD_290,
        Mode.WRAASE_SC2_120, Mode.WRAASE_SC2_180,
        Mode.PASOKON_P3, Mode.PASOKON_P5, Mode.PASOKON_P7,
    ]

    results: list[Result] = []
    for mode in modes_in_order:
        print(f"  testing {mode.value} ...", flush=True)
        results.append(audit_mode(mode))

    # --- Summary table ---
    w = 20
    print()
    print(f"{'Mode':<{w}}  {'Status':<6}  {'WAV (s)':<8}  {'WAV (KB)':<9}  {'Decoded size':<14}  {'MAE':<6}  Note")
    print("-" * 100)
    passes = 0
    fails = 0
    errors = 0
    for r in results:
        dur = f"{r.wav_duration_s:.1f}" if r.wav_duration_s else "—"
        kb = f"{r.wav_size_kb:.0f}" if r.wav_size_kb else "—"
        sz = f"{r.decoded_size[0]}×{r.decoded_size[1]}" if r.decoded_size else "—"
        mae = f"{r.mae:.2f}" if r.mae is not None else "—"
        print(f"{r.mode.value:<{w}}  {r.status:<6}  {dur:<8}  {kb:<9}  {sz:<14}  {mae:<6}  {r.note}")
        if r.status == "PASS":
            passes += 1
        elif r.status == "FAIL":
            fails += 1
        else:
            errors += 1

    print()
    print(f"Results: {passes} PASS  {fails} FAIL  {errors} ERROR  (of {len(results)} modes)")
    if fails or errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
