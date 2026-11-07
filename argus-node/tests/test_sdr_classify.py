"""
SDR classifier tests using synthetic IQ with known structure. No hardware.

Each generator produces a block of complex samples with the time/frequency
shape of a real signal class, so the feature extractor and classifier can be
checked end to end. Thresholds are first-order (see sdr_classify.py); these
tests assert the robust behaviours, not fragile exact subtypes.
"""

import numpy as np
import pytest

from scanner.sdr_classify import (
    iq_to_spectrogram, extract_features, classify, SIGNAL_TYPES,
)

FS = 20e6           # 20 MHz analysis window
N = 60000           # samples (~234 STFT slices at win=256)


def _noise(n=N, amp=0.02):
    return (amp * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


def _bandlimit(iq, fs, bw_hz):
    """Zero spectrum outside +/- bw/2 so a 'wide' signal still has guard bands."""
    spec = np.fft.fft(iq)
    freqs = np.fft.fftfreq(iq.size, d=1.0 / fs)
    spec[np.abs(freqs) > bw_hz / 2] = 0
    return np.fft.ifft(spec).astype(np.complex64)


def continuous_wideband(n=N, bw_hz=8e6):
    """An always-on carrier ~8 MHz wide inside the 20 MHz capture: video."""
    sig = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)
    return _bandlimit(sig, FS, bw_hz) * 2.0 + _noise(n)


def fhss_bursts(n=N, period=6000, burst=800, narrow=True, hop=True, regular=True):
    """
    Frequency-hopping bursts: a control link (ELRS/Crossfire/OcuSync).
    Defaults give ~10 bursts, ~3 slices each, ~13% duty at win=256.
    narrow=True -> single-tone bursts (ELRS); False -> wide noise bursts.
    """
    out = _noise(n)
    i = 0
    while i < n:
        jitter = 0 if regular else np.random.randint(-period // 4, period // 4)
        start = max(0, i + jitter)
        end = min(n, start + burst)
        if end <= start:
            i += period
            continue
        f = np.random.uniform(-FS / 2 * 0.8, FS / 2 * 0.8) if hop else 2e6
        idx = np.arange(start, end)
        carrier = np.exp(2j * np.pi * f * idx / FS)
        if narrow:
            out[idx] += (2.0 * carrier).astype(np.complex64)
        else:
            w = (np.random.randn(idx.size) + 1j * np.random.randn(idx.size))
            wide = _bandlimit(w.astype(np.complex64), FS, 10e6)
            out[idx] += (2.0 * wide * carrier).astype(np.complex64)
        i += period
    return out


def single_wide_burst(n=N):
    """One isolated wide burst on a fixed channel: metering / LoRa-ish."""
    out = _noise(n)
    start = n // 3
    end = start + 4000
    idx = np.arange(start, end)
    w = (np.random.randn(idx.size) + 1j * np.random.randn(idx.size)).astype(np.complex64)
    wide = _bandlimit(w, FS, 3e6)
    out[idx] += (2.0 * wide * np.exp(2j * np.pi * 1e6 * idx / FS)).astype(np.complex64)
    return out


# ── Feature extraction ───────────────────────────────────────────────────────

class TestFeatures:
    def test_noise_has_low_occupancy(self):
        f = extract_features(iq_to_spectrogram(_noise(), FS))
        assert f["total_occ"] < 0.05

    def test_continuous_has_high_duty(self):
        f = extract_features(iq_to_spectrogram(continuous_wideband(), FS))
        assert f["duty_cycle"] > 0.6
        assert f["occupied_bw_hz"] > 4e6

    def test_bursts_are_low_duty_and_periodic(self):
        f = extract_features(iq_to_spectrogram(
            fhss_bursts(regular=True), FS))
        assert f["duty_cycle"] < 0.5
        assert f["n_bursts"] >= 3
        assert f["regularity"] > 0.5

    def test_irregular_bursts_have_low_regularity(self):
        np.random.seed(1)
        f = extract_features(iq_to_spectrogram(
            fhss_bursts(regular=False), FS))
        assert f["regularity"] < 0.9

    def test_hopping_spreads_across_subbands(self):
        f = extract_features(iq_to_spectrogram(fhss_bursts(hop=True), FS))
        assert f["freq_spread"] >= 0.5


# ── Classification ───────────────────────────────────────────────────────────

class TestClassify:
    def test_noise_is_unknown_zero_conf(self):
        c = classify(iq_to_spectrogram(_noise(), FS), "2g4")
        assert c.signal_type == "unknown" and c.confidence == 0.0

    def test_video_5g(self):
        c = classify(iq_to_spectrogram(continuous_wideband(), FS), "5g8")
        assert c.signal_type == "fpv_video_5g"
        assert c.confidence > 0.5

    def test_video_1g2(self):
        c = classify(iq_to_spectrogram(continuous_wideband(), FS), "1g2")
        assert c.signal_type == "video_1g2"

    def test_control_link_2g4(self):
        np.random.seed(0)
        c = classify(iq_to_spectrogram(
            fhss_bursts(narrow=True, regular=True), FS), "2g4")
        assert c.signal_type in ("elrs_2g", "dji_ocusync")
        assert c.confidence > 0.3
        assert c.duty_cycle < 0.5

    def test_narrow_hopping_is_elrs(self):
        np.random.seed(3)
        c = classify(iq_to_spectrogram(
            fhss_bursts(period=7000, burst=500, narrow=True, regular=True), FS), "2g4")
        assert c.signal_type == "elrs_2g"

    def test_control_link_900(self):
        np.random.seed(2)
        c = classify(iq_to_spectrogram(
            fhss_bursts(narrow=True, regular=True), FS), "900")
        assert c.signal_type in ("elrs_900", "unknown")

    def test_single_wide_burst_900_is_lora(self):
        np.random.seed(5)
        c = classify(iq_to_spectrogram(single_wide_burst(), FS), "900")
        assert c.signal_type in ("lora_900", "unknown")

    def test_every_signal_type_is_known(self):
        for band in ("2g4", "5g8", "900", "1g2", "433"):
            c = classify(iq_to_spectrogram(_noise(), FS), band)
            assert c.signal_type in SIGNAL_TYPES

    def test_confidence_bounded(self):
        for gen, band in [(continuous_wideband, "5g8"),
                          (fhss_bursts, "2g4"),
                          (_noise, "900")]:
            c = classify(iq_to_spectrogram(gen(), FS), band)
            assert 0.0 <= c.confidence <= 1.0

    def test_short_block_does_not_crash(self):
        c = classify(iq_to_spectrogram(_noise(100), FS), "2g4")
        assert c.signal_type in SIGNAL_TYPES
