"""
scanner/sdr_classify.py
=======================
Classify an RF emission from a block of IQ samples into a drone-relevant
signal type. Pure numpy; no SDR hardware and no I/O, so it is fully testable
with synthetic signals.

The pipeline is two stages so each is testable on its own:

  iq_to_spectrogram(iq, fs)       -> Spectrogram (power in dB, freq x time)
  classify(spectrogram, band)     -> Classification (signal_type, confidence, features)

What the features mean
----------------------
duty_cycle      fraction of time the band is occupied. Continuous links
                (analog/digital video) sit near 1.0; frequency-hopping control
                links (ELRS, Crossfire, OcuSync) are bursty and low.
burst_rate_hz   bursts per second. ELRS runs 50-1000 Hz packet rates.
regularity      1 - CoV of inter-burst gaps. Control links are highly periodic
                (near 1.0); Wi-Fi / interference is irregular (low).
occupied_bw_hz  width of the occupied part of the captured span.
freq_spread     fraction of sub-bands that saw energy. Hopping spreaders light
                up many sub-bands over the capture; a fixed carrier lights one.

These thresholds are first-order and meant to be calibrated in the field with
a known transmitter (e.g. a RadioMaster ELRS radio). They are named constants
below for exactly that reason. The classifier is deliberately conservative:
confidence reflects how cleanly the features match a template, and anything
ambiguous returns "unknown" at low confidence rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ── Tunable thresholds (calibrate in the field) ──────────────────────────────
NOISE_MARGIN_DB    = 6.0      # sub-band power above its noise floor to count "active"
CONTINUOUS_DUTY    = 0.6      # duty >= this => continuous carrier (video)
BURSTY_DUTY_MAX    = 0.5      # duty <= this => bursty (control link)
MIN_OCCUPANCY      = 0.01     # below this total occupancy => just noise
CONTROL_REG_MIN    = 0.55     # regularity >= this => periodic control link
VIDEO_MIN_BW_HZ    = 4e6      # analog/digital video occupies several MHz (persistent)
WIDE_BURST_BW_HZ   = 6e6      # OcuSync-style wide bursts vs narrow ELRS bursts
STFT_WIN           = 256      # FFT size for the spectrogram
N_SUBBANDS         = 32       # sub-bands for robust, leakage-tolerant detection


@dataclass
class Spectrogram:
    power_db: np.ndarray        # shape (n_freq, n_time), dB
    freqs_hz: np.ndarray        # baseband bin centres, length n_freq
    slice_dt: float             # seconds per time column
    fs: float                   # sample rate used


@dataclass
class Classification:
    signal_type: str            # see SIGNAL_TYPES
    confidence: float           # 0..1
    band: str
    occupied_bw_hz: float = 0.0
    burst_bw_hz: float = 0.0
    duty_cycle: float = 0.0
    burst_rate_hz: float = 0.0
    regularity: float = 0.0
    freq_spread: float = 0.0
    center_offset_hz: float = 0.0   # occupied-band centre relative to capture centre
    features: dict = field(default_factory=dict)


SIGNAL_TYPES = {
    "dji_ocusync", "elrs_2g", "elrs_900", "crossfire_900",
    "fpv_video_5g", "fpv_video_analog", "video_1g2", "lora_900", "unknown",
}


def iq_to_spectrogram(iq: np.ndarray, fs: float, win: int = STFT_WIN) -> Spectrogram:
    """Short-time FFT magnitude (dB), frequency-shifted so DC is centred."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < win:
        iq = np.pad(iq, (0, win - iq.size))
    n_slices = iq.size // win
    iq = iq[: n_slices * win].reshape(n_slices, win)
    window = np.hanning(win).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(iq * window, axis=1), axes=1)
    power = (np.abs(spec) ** 2).T                      # (freq, time)
    power_db = 10.0 * np.log10(power + 1e-12)
    freqs = np.fft.fftshift(np.fft.fftfreq(win, d=1.0 / fs))
    return Spectrogram(power_db=power_db, freqs_hz=freqs,
                       slice_dt=win / fs, fs=fs)


def _subband_power(spec: Spectrogram, k: int = N_SUBBANDS):
    """
    Collapse the fine FFT bins into k sub-bands of linear power, (k, n_time).
    Averaging over several bins dilutes a strong tone's spectral leakage, so a
    sub-band only reads "hot" when real energy sits in it, not a sidelobe.
    """
    lin = np.power(10.0, spec.power_db / 10.0)        # back to linear power
    n_freq = lin.shape[0]
    k = min(k, n_freq)
    chunks = np.array_split(lin, k, axis=0)
    sb = np.stack([c.mean(axis=0) for c in chunks], axis=0)   # (k, n_time)
    sb_centres = np.array([
        spec.freqs_hz[np.array_split(np.arange(n_freq), k)[i]].mean()
        for i in range(k)
    ])
    return sb, sb_centres


def extract_features(spec: Spectrogram) -> dict:
    """Occupancy, duty cycle, burst timing, per-burst and persistent bandwidth."""
    sb, sb_centres = _subband_power(spec)
    k, n_time = sb.shape
    subband_bw = spec.fs / k

    # Global noise floor = median over all sub-band cells. The scanner always
    # tunes a span wider than the emission, so most cells are guard band / quiet
    # time and the median lands in the noise, even for an always-on carrier
    # (its sub-bands are the minority). Sub-band averaging kills tone leakage.
    floor = float(np.median(sb))
    thresh = floor * (10.0 ** (NOISE_MARGIN_DB / 10.0))
    active = sb > thresh                              # (k, n_time) bool

    total_occ = float(active.mean())
    time_active = active.any(axis=0)
    duty = float(time_active.mean())

    bursts = _runs(time_active)
    burst_rate = len(bursts) / (n_time * spec.slice_dt) if n_time else 0.0

    if len(bursts) >= 3:
        starts = np.array([b[0] for b in bursts], dtype=float)
        gaps = np.diff(starts)
        cov = gaps.std() / gaps.mean() if gaps.mean() > 0 else 1.0
        regularity = float(max(0.0, 1.0 - cov))
    else:
        regularity = 0.0

    # Persistent occupied bandwidth: sub-bands hot for a real fraction of time
    # (a fixed carrier / video). Hopping links light each sub-band only briefly,
    # so this stays small for them.
    persistent = active.mean(axis=1) > 0.3
    occupied_bw = float(persistent.sum() * subband_bw)

    # Per-burst bandwidth: during active slices, how many sub-bands are hot at
    # once (median). Narrow ELRS bursts ~1 sub-band; wide OcuSync bursts many.
    if time_active.any():
        per_slice = active[:, time_active].sum(axis=0)
        burst_bw = float(np.median(per_slice) * subband_bw)
    else:
        burst_bw = 0.0

    # Frequency spread: fraction of sub-bands that ever went active.
    freq_spread = float((active.any(axis=1)).mean())

    if persistent.any():
        center_offset = float(np.average(sb_centres, weights=persistent.astype(float)))
    else:
        center_offset = 0.0

    peak = float(sb.max()) if sb.size else 0.0
    peak_db = float(10.0 * np.log10(peak + 1e-12))
    noise_db = float(10.0 * np.log10(floor + 1e-12))

    return {
        "total_occ": total_occ,
        "duty_cycle": duty,
        "burst_rate_hz": burst_rate,
        "regularity": regularity,
        "occupied_bw_hz": occupied_bw,
        "burst_bw_hz": burst_bw,
        "freq_spread": freq_spread,
        "center_offset_hz": center_offset,
        "noise_floor_db": noise_db,
        "peak_dbfs": peak_db,
        "snr_db": peak_db - noise_db,
        "n_bursts": len(bursts),
    }


def _runs(mask: np.ndarray):
    """Return [(start, end_exclusive), ...] index runs where mask is True."""
    runs = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def classify(spec: Spectrogram, band: str) -> Classification:
    """
    Map spectrogram features + band to a signal type and confidence.

    band is one of: 2g4, 5g8, 900, 1g2, 433. It matters because the same
    feature set means different things per band (e.g. a continuous wide
    carrier at 5.8 GHz is FPV video; at 900 MHz it is more likely telemetry).
    """
    f = extract_features(spec)
    out = Classification(
        signal_type="unknown", confidence=0.0, band=band,
        occupied_bw_hz=f["occupied_bw_hz"], burst_bw_hz=f["burst_bw_hz"],
        duty_cycle=f["duty_cycle"],
        burst_rate_hz=f["burst_rate_hz"], regularity=f["regularity"],
        freq_spread=f["freq_spread"], center_offset_hz=f["center_offset_hz"],
        features=f,
    )

    # Nothing meaningfully above the noise floor.
    if f["total_occ"] < MIN_OCCUPANCY:
        return out

    continuous = f["duty_cycle"] >= CONTINUOUS_DUTY
    bursty     = f["duty_cycle"] <= BURSTY_DUTY_MAX
    periodic   = f["regularity"] >= CONTROL_REG_MIN
    wide       = f["occupied_bw_hz"] >= VIDEO_MIN_BW_HZ

    if band in ("5g8",):
        if continuous and wide:
            out.signal_type = "fpv_video_5g"
            out.confidence = _conf(f["duty_cycle"], CONTINUOUS_DUTY,
                                   f["occupied_bw_hz"] / VIDEO_MIN_BW_HZ)
        elif bursty and periodic:
            out.signal_type = "dji_ocusync"       # digital video/control, bursty
            out.confidence = 0.5 * periodic + 0.3
        return out

    if band == "1g2":
        if continuous and wide:
            out.signal_type = "video_1g2"
            out.confidence = _conf(f["duty_cycle"], CONTINUOUS_DUTY, 1.0)
        return out

    if band == "2g4":
        if bursty and periodic and f["freq_spread"] >= 0.5:
            # Hopping control link. Wide bursts -> OcuSync; narrow -> ELRS.
            if f["burst_bw_hz"] >= WIDE_BURST_BW_HZ:
                out.signal_type = "dji_ocusync"
            else:
                out.signal_type = "elrs_2g"
            out.confidence = min(1.0, 0.4 + 0.4 * f["regularity"] + 0.2 * f["freq_spread"])
        elif continuous and wide:
            out.signal_type = "dji_ocusync"       # occupied digital video downlink
            out.confidence = 0.5
        return out

    if band in ("900", "433"):
        if bursty and periodic and f["freq_spread"] >= 0.5:
            out.signal_type = "elrs_900" if band == "900" else "unknown"
            out.confidence = min(1.0, 0.35 + 0.4 * f["regularity"])
        elif f["n_bursts"] <= 3 and not periodic:
            # A few isolated wide bursts on one channel: metering / LoRa-ish.
            out.signal_type = "lora_900" if band == "900" else "unknown"
            out.confidence = 0.4
        return out

    return out


def _conf(value: float, threshold: float, scale: float) -> float:
    """Confidence that rises as value clears a threshold, capped at 1.0."""
    base = min(1.0, max(0.0, (value - threshold) / max(1e-6, 1 - threshold)))
    return float(min(1.0, 0.5 + 0.4 * base + 0.1 * min(scale, 1.0)))
