"""
scanner/sdr.py
==============
SDR scanner: sweeps a band plan, classifies each capture into a drone-relevant
signal type (scanner/sdr_classify.py), and publishes classified links as
argus/<node>/rf_detection. The server correlates these with Remote ID and
flags links that have no Remote ID broadcast (likely non-compliant drones).

This does NOT decode Remote ID — the Wi-Fi and BT scanners do that.

Radio support (scanner/sdr_source.py):
  HackRF One / Pro   1 MHz - 6 GHz: 2.4 GHz (ELRS/OcuSync), 5.8 GHz video,
                     900 MHz, 1.2 GHz, 433 MHz. The full picture.
  RTL-SDR            up to ~1.77 GHz: 433 / 900 / 1.2 GHz only. 2.4 and
                     5.8 GHz are above its tuner and are skipped.

The classifier thresholds are first-order and meant to be calibrated in the
field with a known transmitter (see sdr_classify.py). Nothing here transmits:
the HackRF's transmit path is never used. Jamming is illegal for non-federal
operators; AEGIS is receive-only by design.
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("sdr")

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False
    log.warning("numpy not installed — SDR scanner disabled")

from scanner.sdr_classify import iq_to_spectrogram, classify
from scanner.sdr_source import IQSource, make_source, SDRSourceError


# ── Band plan ────────────────────────────────────────────────────────────────
# Each entry: band code -> list of centre frequencies (Hz) to step through.
# Hopping links (ELRS/OcuSync) spread across a band, so a few 20 MHz windows
# sample enough hops to classify. Video sits on a fixed channel, so 5.8 GHz is
# stepped across the FPV band.
def _range(lo, hi, step):
    f = lo
    out = []
    while f <= hi:
        out.append(int(f))
        f += step
    return out

BAND_PLAN = {
    "433": [433_900_000],
    "900": [915_000_000],
    "1g2": [1_260_000_000],
    "2g4": [2_412_000_000, 2_437_000_000, 2_462_000_000],
    "5g8": _range(5_660_000_000, 5_940_000_000, 40_000_000),
}

BAND_ORDER = ["2g4", "5g8", "900", "1g2", "433"]


class SDRScanner:
    """
    Sweeps the band plan, classifies, and publishes classified RF links.
    Backward-compatible constructor; extra behaviour is opt-in via kwargs or
    node config.
    """

    def __init__(
        self,
        device_index: int,
        publisher,
        gps,
        node_id: str,
        stop_event: threading.Event,
        backend: str = "rtlsdr",
        sample_rate: Optional[float] = None,
        source: Optional[IQSource] = None,   # inject for tests
        bands: Optional[list[str]] = None,
        dwell_s: float = 0.04,
        min_confidence: float = 0.6,
        publish_interval_s: float = 3.0,
        rssi_offset_dbm: float = -50.0,
    ):
        self.device_index = device_index
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self.backend = backend
        self.sample_rate = sample_rate
        self._source = source
        self.bands = bands
        self.dwell_s = dwell_s
        self.min_confidence = min_confidence
        self.publish_interval_s = publish_interval_s
        self.rssi_offset_dbm = rssi_offset_dbm
        self._last_pub: dict[str, float] = {}
        self._stats = {"captures": 0, "classified": 0, "published": 0}
        self._last_stats = time.monotonic()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self):
        if not _NUMPY:
            log.error("numpy not available — SDR scanner exiting")
            return

        source = self._source
        if source is None:
            try:
                source = make_source(self.backend, device_index=self.device_index,
                                     sample_rate=self.sample_rate)
            except SDRSourceError as e:
                log.error(f"SDR backend unavailable: {e}. SDR scanner exiting.")
                return
        try:
            source.open()
        except SDRSourceError as e:
            log.error(f"Could not open SDR: {e}. SDR scanner exiting.")
            return

        steps = self._build_steps(source)
        if not steps:
            log.error(f"No scan bands reachable by {source.name}. SDR scanner exiting.")
            source.close()
            return

        bands_seen = sorted({b for b, _ in steps})
        log.info(f"SDR scanner running on {source.name}: bands {', '.join(bands_seen)}, "
                 f"{len(steps)} steps, {source.sample_rate/1e6:.0f} MS/s")

        n = max(iq_to_spectrogram_min(), int(source.sample_rate * self.dwell_s))
        try:
            while not self.stop_event.is_set():
                for band, center in steps:
                    if self.stop_event.is_set():
                        break
                    self._scan_step(source, band, center, n)
                self._log_stats()
        finally:
            source.close()
            log.info("SDR scanner stopped.")

    def _build_steps(self, source: IQSource) -> list[tuple[str, int]]:
        bands = self.bands or BAND_ORDER
        steps: list[tuple[str, int]] = []
        skipped = set()
        for band in bands:
            for center in BAND_PLAN.get(band, []):
                if source.can_tune(center):
                    steps.append((band, center))
                else:
                    skipped.add(band)
        if skipped:
            log.info(f"{source.name} can't reach {', '.join(sorted(skipped))} "
                     f"(out of tuning range) — skipping")
        return steps

    # ── One capture ───────────────────────────────────────────────────────────

    def _scan_step(self, source: IQSource, band: str, center: int, n: int):
        try:
            source.set_center(center)
            iq = source.read(n)
        except Exception as e:
            log.debug(f"SDR read error at {center/1e6:.1f} MHz: {e}")
            return
        if iq is None or len(iq) < iq_to_spectrogram_min():
            return

        self._stats["captures"] += 1
        spec = iq_to_spectrogram(iq, source.sample_rate)
        c = classify(spec, band)
        if c.signal_type == "unknown" or c.confidence < self.min_confidence:
            return
        self._stats["classified"] += 1

        key = f"{band}:{c.signal_type}"
        now = time.monotonic()
        last = self._last_pub.get(key)
        # A missing key means this type has never been published, so publish it
        # now. Using 0.0 as the sentinel would wrongly suppress the first
        # detection whenever the monotonic clock reads below publish_interval_s
        # (e.g. on a freshly booted node).
        if last is not None and now - last < self.publish_interval_s:
            return
        self._last_pub[key] = now

        f = c.features
        bandwidth = int(max(c.burst_bw_hz, c.occupied_bw_hz))
        signal = {
            "signal_type":   c.signal_type,
            "band":          band,
            "center_freq_hz": int(center + c.center_offset_hz),
            "bandwidth_hz":  bandwidth,
            "rssi":          round(f.get("peak_dbfs", -100.0) + self.rssi_offset_dbm, 1),
            "snr_db":        round(f.get("snr_db", 0.0), 1),
            "confidence":    round(c.confidence, 2),
            "hop_rate_hz":   round(c.burst_rate_hz, 1),
            "burst_rate_hz": round(c.burst_rate_hz, 1),
        }
        try:
            self.publisher.publish_rf_detection(signal, self.gps)
            self._stats["published"] += 1
            log.info(f"RF link: {c.signal_type} @ {band} "
                     f"{signal['center_freq_hz']/1e6:.1f} MHz "
                     f"rssi={signal['rssi']:.0f} conf={c.confidence:.0%}")
        except Exception as e:
            log.debug(f"rf_detection publish error: {e}")

    def _log_stats(self):
        now = time.monotonic()
        if now - self._last_stats < 60.0:
            return
        s = self._stats
        log.info(f"SDR stats (last {int(now - self._last_stats)} s): "
                 f"{s['captures']} captures, {s['classified']} classified, "
                 f"{s['published']} published")
        self._stats = {"captures": 0, "classified": 0, "published": 0}
        self._last_stats = now


def iq_to_spectrogram_min() -> int:
    """Minimum sample count worth analysing (one FFT window)."""
    from scanner.sdr_classify import STFT_WIN
    return STFT_WIN
