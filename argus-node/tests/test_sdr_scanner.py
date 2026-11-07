"""
SDR scanner loop and IQ sources, driven by a SyntheticSource — no hardware.

The synthetic source returns a signal chosen by the current centre frequency,
so one sweep exercises the band plan, classification, rate limiting and the
rf_detection publish path end to end.
"""

import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from scanner.sdr import SDRScanner, BAND_PLAN
from scanner.sdr_source import (
    SyntheticSource, RtlSdrSource, HackRFSource, make_source, SDRSourceError,
)

# Reuse the signal generators from the classifier tests.
from test_sdr_classify import (
    continuous_wideband, fhss_bursts, _noise, FS,
)


def band_of(center_hz):
    for band, centers in BAND_PLAN.items():
        if center_hz in centers:
            return band
    return "?"


def scene_generator(center_hz, n, fs):
    """Return a signal appropriate to the band the scanner tuned to."""
    band = band_of(center_hz)
    np.random.seed(int(center_hz) % 2_000_000_000)
    if band == "5g8":
        return continuous_wideband(n)             # video
    if band == "2g4":
        return fhss_bursts(n, narrow=True, regular=True)   # ELRS
    if band == "900":
        return fhss_bursts(n, narrow=True, regular=True)   # control
    return _noise(n)


def make_scanner(generator, bands=None, **kw):
    pub = MagicMock()
    gps = MagicMock(lat=40.0, lon=-100.0, alt=300.0)
    stop = threading.Event()
    src = SyntheticSource(generator, sample_rate=FS)
    kw.setdefault("min_confidence", 0.6)
    kw.setdefault("publish_interval_s", 0.0)
    s = SDRScanner(device_index=0, publisher=pub, gps=gps, node_id="ARGUS-01",
                   stop_event=stop, source=src, bands=bands,
                   dwell_s=N_over_fs(), **kw)
    return s, pub, stop, src


def N_over_fs():
    # dwell that yields ~60000 samples at FS (matches the generators' N)
    return 60000 / FS


# ── Sources ──────────────────────────────────────────────────────────────────

class TestSources:
    def test_rtlsdr_tune_range_blocks_24ghz(self):
        src = RtlSdrSource()
        assert src.can_tune(915e6)
        assert not src.can_tune(2.44e9)
        assert not src.can_tune(5.8e9)

    def test_hackrf_tune_range_covers_all(self):
        src = HackRFSource()
        for f in (433e6, 915e6, 1.26e9, 2.44e9, 5.8e9):
            assert src.can_tune(f)

    def test_make_source_unknown_backend(self):
        with pytest.raises(SDRSourceError):
            make_source("sdrplay")

    def test_make_source_builds_known(self):
        assert make_source("hackrf").name == "hackrf"
        assert make_source("rtlsdr").name == "rtlsdr"

    def test_synthetic_source_reads(self):
        src = SyntheticSource(lambda c, n, fs: _noise(n), sample_rate=FS)
        src.open(); src.set_center(915e6)
        assert src.read(1000).shape[0] == 1000


# ── Band plan ────────────────────────────────────────────────────────────────

class TestBandPlan:
    def test_rtlsdr_steps_exclude_high_bands(self):
        s, *_ = make_scanner(scene_generator)
        steps = s._build_steps(RtlSdrSource())
        bands = {b for b, _ in steps}
        assert "2g4" not in bands and "5g8" not in bands
        assert "900" in bands

    def test_hackrf_steps_include_all(self):
        s, *_ = make_scanner(scene_generator)
        steps = s._build_steps(HackRFSource())
        bands = {b for b, _ in steps}
        assert {"2g4", "5g8", "900"} <= bands


# ── Scanner loop ─────────────────────────────────────────────────────────────

class TestScannerLoop:
    def _run_one_sweep(self, scanner, stop, src):
        # Stop after a single pass by flipping the event once steps are done.
        real_read = src.read
        steps = scanner._build_steps(src)
        state = {"n": 0, "total": len(steps)}

        def counting_read(n):
            state["n"] += 1
            if state["n"] >= state["total"]:
                stop.set()
            return real_read(n)

        src.read = counting_read
        scanner.run()

    def test_publishes_classified_links(self):
        s, pub, stop, src = make_scanner(scene_generator, bands=["2g4", "5g8", "900"])
        self._run_one_sweep(s, stop, src)
        assert pub.publish_rf_detection.called
        types = {c.args[0]["signal_type"] for c in pub.publish_rf_detection.call_args_list}
        assert "fpv_video_5g" in types          # from 5g8 steps
        assert "elrs_2g" in types               # from 2g4 steps

    def test_signal_payload_shape(self):
        s, pub, stop, src = make_scanner(scene_generator, bands=["5g8"])
        self._run_one_sweep(s, stop, src)
        sig = pub.publish_rf_detection.call_args.args[0]
        for k in ("signal_type", "band", "center_freq_hz", "bandwidth_hz",
                  "rssi", "snr_db", "confidence", "hop_rate_hz"):
            assert k in sig
        assert sig["band"] == "5g8"
        assert 0.0 <= sig["confidence"] <= 1.0

    def test_noise_band_publishes_nothing(self):
        s, pub, stop, src = make_scanner(lambda c, n, fs: _noise(n), bands=["900"])
        self._run_one_sweep(s, stop, src)
        assert not pub.publish_rf_detection.called

    def test_rate_limit_suppresses_repeats(self):
        s, pub, stop, src = make_scanner(scene_generator, bands=["5g8"],
                                         publish_interval_s=999.0)
        # Two sweeps; the second must be suppressed by the per-key rate limit.
        steps = s._build_steps(src)
        state = {"n": 0}

        def counting_read(n):
            state["n"] += 1
            if state["n"] >= len(steps) * 2:
                stop.set()
            return _bandscene(src._center, n)

        src.read = counting_read
        s.run()
        # 5g8 has multiple centers; each distinct (band,type) key publishes once.
        # Same signal_type across repeat sweeps is rate-limited, so count stays
        # at one publish for the 'fpv_video_5g' key.
        vids = [c for c in pub.publish_rf_detection.call_args_list
                if c.args[0]["signal_type"] == "fpv_video_5g"]
        assert len(vids) == 1

    def test_min_confidence_gate(self):
        # An impossibly high confidence gate blocks everything.
        s, pub, stop, src = make_scanner(scene_generator, bands=["5g8"],
                                         min_confidence=1.01)
        self._run_one_sweep(s, stop, src)
        assert not pub.publish_rf_detection.called


def _bandscene(center, n):
    np.random.seed(int(center) % 2_000_000_000)
    return continuous_wideband(n)
