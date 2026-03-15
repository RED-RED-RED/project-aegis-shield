"""
scanner/sdr.py
==============
Optional RTL-SDR scanner for passive RF fingerprinting at 2.4 GHz.

This does NOT decode Remote ID directly — that's handled by the Wi-Fi and BT
scanners. Instead, the SDR detects RF energy bursts that correlate in time and
frequency with known drone control links (e.g. DJI OcuSync on 2.4 GHz), which
can help identify unlicensed or non-RID drones.

Fingerprinting approach:
  1. IQ sample the 2.4 GHz band
  2. Run energy detection across 2MHz sub-bands
  3. Publish anomalous energy bursts to MQTT for central correlation
  4. Central server cross-references with RID detections by timestamp/position

Requires:
  pip install pyrtlsdr numpy scipy
  RTL-SDR drivers: sudo apt install rtl-sdr librtlsdr-dev
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("sdr")

try:
    import numpy as np
    from rtlsdr import RtlSdr
    _SDR_AVAILABLE = True
except ImportError:
    _SDR_AVAILABLE = False
    log.warning("pyrtlsdr or numpy not installed — SDR scanner disabled")


# 2.4 GHz sub-bands to sweep (DJI OcuSync, video downlinks, etc.)
SWEEP_FREQS = [
    2_412_000_000,   # Wi-Fi ch1
    2_437_000_000,   # Wi-Fi ch6
    2_462_000_000,   # Wi-Fi ch11
    2_440_000_000,   # DJI OcuSync center
]

SAMPLE_RATE    = 2_400_000   # 2.4 MSPS — covers 2.4 MHz per tune
NUM_SAMPLES    = 256_000     # ~107 ms per capture
ENERGY_THRESH  = 15.0        # dB above noise floor to flag


class SDRScanner:
    """
    RTL-SDR energy scanner. Sweeps 2.4 GHz sub-bands and publishes
    anomalous RF bursts to the AEGIS platform for correlation.
    """

    def __init__(
        self,
        device_index: int,
        publisher,
        gps,
        node_id: str,
        stop_event: threading.Event,
    ):
        self.device_index = device_index
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self._noise_floor: dict[int, float] = {}

    def run(self):
        if not _SDR_AVAILABLE:
            log.error("SDR libraries not available — scanner exiting")
            return

        log.info(f"SDR scanner starting (device {self.device_index})")

        try:
            sdr = RtlSdr(self.device_index)
            sdr.sample_rate   = SAMPLE_RATE
            sdr.gain          = "auto"
        except Exception as e:
            log.error(f"Failed to open RTL-SDR device {self.device_index}: {e}")
            return

        try:
            while not self.stop_event.is_set():
                for freq in SWEEP_FREQS:
                    if self.stop_event.is_set():
                        break
                    self._scan_freq(sdr, freq)
                    time.sleep(0.05)   # Brief dwell between tunes
        finally:
            sdr.close()
            log.info("SDR scanner stopped.")

    def _scan_freq(self, sdr, freq: int):
        try:
            sdr.center_freq = freq
            samples = sdr.read_samples(NUM_SAMPLES)
        except Exception as e:
            log.debug(f"SDR read error at {freq/1e6:.1f} MHz: {e}")
            return

        # Power spectral density (simple FFT-based)
        power_db = self._compute_power_db(samples)

        # Calibrate noise floor (rolling average over first 10 samples)
        if freq not in self._noise_floor:
            self._noise_floor[freq] = power_db
            return

        alpha = 0.05   # Slow tracking of noise floor
        self._noise_floor[freq] = (1 - alpha) * self._noise_floor[freq] + alpha * power_db

        snr = power_db - self._noise_floor[freq]

        if snr > ENERGY_THRESH:
            log.debug(f"RF burst @ {freq/1e6:.1f} MHz: {power_db:.1f} dBm (SNR={snr:.1f} dB)")
            self._publish_rf_event(freq, power_db, snr)

    def _compute_power_db(self, samples) -> float:
        """Estimate total band power in dBm (relative, not calibrated)."""
        import numpy as np
        power_linear = np.mean(np.abs(samples) ** 2)
        if power_linear <= 0:
            return -100.0
        return float(10 * np.log10(power_linear))

    def _publish_rf_event(self, freq: int, power_db: float, snr: float):
        """Publish an RF anomaly event to MQTT for central correlation."""
        payload = {
            "node_id": self.node_id,
            "type": "rf_burst",
            "freq_hz": freq,
            "power_db": round(power_db, 2),
            "snr_db": round(snr, 2),
            "node_position": {
                "lat": self.gps.lat,
                "lon": self.gps.lon,
                "alt": self.gps.alt,
            },
            "ts": time.time(),
        }
        # Publish to a separate topic so the server can correlate independently
        import json
        topic = f"argus/{self.node_id}/rf_event"
        try:
            self.publisher._enqueue(topic, payload, qos=0)
        except Exception as e:
            log.debug(f"RF event publish error: {e}")
