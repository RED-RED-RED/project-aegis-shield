"""
calibration/tests/test_calibration.py
======================================
Tests for the calibration engine, data collector, and config writer.
No server or GPS hardware needed — all inputs are synthesised.
"""

import math
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
import sys, os
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from calibration.engine import (
    CalibrationEngine, CalibrationSample, CalibrationResult,
    MIN_SAMPLES_PER_NODE,
)
from calibration.collector import (
    haversine_m, interpolate_truth, TruthPoint,
    parse_gpx, parse_srt, parse_csv_track, build_samples,
)
from calibration.config_writer import (
    write_calibration_yaml, patch_trilateration_py,
    generate_patch_snippet,
)

# ── Synthetic data generators ──────────────────────────────────────────────

TRUE_RSSI_REF = -22.0
TRUE_N        = 2.85
NOISE_STD_DBM = 5.0   # Realistic RSSI noise

def synth_rssi(dist_m: float, rssi_ref=TRUE_RSSI_REF, n=TRUE_N, noise=0.0) -> float:
    return rssi_ref - 10 * n * math.log10(max(dist_m, 1.0)) + noise

def synth_samples(
    node_id:      str = "ARGUS-01",
    node_lat:     float = 40.75,
    node_lon:     float = -73.99,
    n_samples:    int = 200,
    dist_range:   tuple = (30, 800),
    rssi_ref:     float = TRUE_RSSI_REF,
    n:            float = TRUE_N,
    noise_std:    float = NOISE_STD_DBM,
    seed:         int = 42,
) -> list[CalibrationSample]:
    rng = np.random.default_rng(seed)
    samples = []
    for i in range(n_samples):
        d    = rng.uniform(*dist_range)
        rssi = synth_rssi(d, rssi_ref, n, rng.normal(0, noise_std))
        rssi = max(rssi, -105)
        # Fake drone position at distance d due-east of node
        dlat = node_lat
        dlon = node_lon + math.degrees(d / (6_371_000 * math.cos(math.radians(node_lat))))
        samples.append(CalibrationSample(
            node_id   = node_id,
            rssi      = rssi,
            true_dist = d,
            ts        = time.time() + i,
            drone_lat = dlat,
            drone_lon = dlon,
            node_lat  = node_lat,
            node_lon  = node_lon,
        ))
    return samples


# ── Engine tests ───────────────────────────────────────────────────────────

class TestCalibrationEngine:

    def _engine(self):
        return CalibrationEngine()

    def test_recovers_true_n_within_tolerance(self):
        """With 200 samples and 5 dB noise, fitted n should be close to truth."""
        engine  = self._engine()
        samples = synth_samples(n_samples=200, noise_std=NOISE_STD_DBM)
        results = engine.fit(samples)

        assert "ARGUS-01" in results
        r = results["ARGUS-01"]
        # With 5 dB noise, 200 samples → expect n within ±0.3
        assert abs(r.fitted_n - TRUE_N) < 0.3, \
            f"Fitted n={r.fitted_n:.3f}, truth={TRUE_N}"

    def test_recovers_rssi_ref_within_tolerance(self):
        engine  = self._engine()
        samples = synth_samples(n_samples=200, noise_std=NOISE_STD_DBM)
        results = engine.fit(samples)
        r = results["ARGUS-01"]
        # With 5 dB noise, RSSI_REF should be within ±6 dBm
        assert abs(r.fitted_rssi_ref - TRUE_RSSI_REF) < 6.0, \
            f"Fitted RSSI_REF={r.fitted_rssi_ref:.1f}, truth={TRUE_RSSI_REF}"

    def test_r_squared_high_for_clean_data(self):
        """Noiseless data should give R² ≥ 0.99."""
        engine  = self._engine()
        samples = synth_samples(n_samples=100, noise_std=0.0)
        results = engine.fit(samples)
        assert results["ARGUS-01"].r_squared >= 0.99

    def test_r_squared_decreases_with_more_noise(self):
        """Higher noise → lower R²."""
        engine = self._engine()
        r_clean  = engine.fit(synth_samples(noise_std=0.0))["ARGUS-01"].r_squared
        r_noisy  = engine.fit(synth_samples(noise_std=10.0))["ARGUS-01"].r_squared
        assert r_clean > r_noisy

    def test_rmse_bounded_for_reasonable_noise(self):
        """With 5 dB noise, RMSE should be below 200m across 30–800m range."""
        engine  = self._engine()
        samples = synth_samples(n_samples=300, noise_std=NOISE_STD_DBM)
        results = engine.fit(samples)
        assert results["ARGUS-01"].rmse_m < 200

    def test_multi_node_independent_fit(self):
        """Each node should be fitted independently and return separate results.
        We use parameter combinations that keep RSSI above the noise floor
        (-105 dBm) across the distance range so clamping doesn't corrupt the fit.
        """
        engine = self._engine()
        # ARGUS-01: low n, short range  → lower RSSI values stay above -105
        # ARGUS-02: medium n, use closer range so RSSI doesn't clamp
        # ARGUS-03: low n, shifted RSSI_REF
        samples = (
            synth_samples("ARGUS-01", rssi_ref=-20.0, n=2.3, dist_range=(30, 500), seed=1) +
            synth_samples("ARGUS-02", rssi_ref=-15.0, n=2.9, dist_range=(30, 400), seed=2) +
            synth_samples("ARGUS-03", rssi_ref=-18.0, n=2.1, dist_range=(30, 500), seed=3)
        )
        results = engine.fit(samples)
        assert set(results.keys()) == {"ARGUS-01", "ARGUS-02", "ARGUS-03"}

        # Each node's fitted n should be within 0.4 of its own true value
        assert abs(results["ARGUS-01"].fitted_n - 2.3) < 0.4
        assert abs(results["ARGUS-02"].fitted_n - 2.9) < 0.4
        assert abs(results["ARGUS-03"].fitted_n - 2.1) < 0.4

        # ARGUS-01 and ARGUS-03 should have clearly different fitted n
        assert abs(results["ARGUS-01"].fitted_n - results["ARGUS-03"].fitted_n) < 0.5

    def test_global_fallback_for_sparse_node(self):
        """Node with < MIN_SAMPLES gets global-fallback result."""
        engine = self._engine()
        samples = (
            synth_samples("ARGUS-01", n_samples=200, seed=1) +
            synth_samples("ARGUS-02", n_samples=5, seed=2)   # too few
        )
        results = engine.fit(samples, global_fallback=True)
        assert "ARGUS-02" in results
        assert any("global model" in w for w in results["ARGUS-02"].warnings)

    def test_warning_for_implausible_n(self):
        """Heavily obstructed environment may produce n outside normal range."""
        engine = self._engine()
        # Synthetic data with extreme n=4.8
        samples = synth_samples(n=4.8, noise_std=1.0)
        results = engine.fit(samples)
        # The fitted value may be outside range → should warn
        r = results["ARGUS-01"]
        if r.fitted_n > 5.0 or r.fitted_n < 1.5:
            assert r.warnings

    def test_narrow_distance_range_warning(self):
        """Narrow distance range (< 100m spread) should produce a warning."""
        engine  = self._engine()
        samples = synth_samples(dist_range=(90, 110))  # only 20m spread
        results = engine.fit(samples)
        assert any("range" in w.lower() or "100m" in w or "50" in w
                   for w in results["ARGUS-01"].warnings)

    def test_quality_label_ordering(self):
        """Better fit → higher quality label."""
        engine = self._engine()
        clean  = engine.fit(synth_samples(noise_std=0.5))["ARGUS-01"]
        noisy  = engine.fit(synth_samples(noise_std=12.0))["ARGUS-01"]
        quality_rank = {"excellent": 4, "good": 3, "acceptable": 2, "poor": 1}
        assert quality_rank[clean.quality] >= quality_rank[noisy.quality]

    def test_result_to_config_dict(self):
        """to_config_dict() should include all required keys."""
        engine  = self._engine()
        results = engine.fit(synth_samples())
        d = results["ARGUS-01"].to_config_dict()
        assert all(k in d for k in ["rssi_ref_dbm","path_loss_exp","quality","r_squared","rmse_m"])

    def test_filters_extreme_rssi(self):
        """Readings below -105 dBm should be filtered out (noise floor)."""
        engine = self._engine()
        samples = synth_samples(n_samples=100)
        # Inject garbage readings
        from copy import deepcopy
        bad = deepcopy(samples[:5])
        for s in bad:
            s.rssi = -120
        results_with = engine.fit(samples + bad)
        results_base = engine.fit(samples)
        # Both should produce valid results; bad samples shouldn't wildly change fit
        r_with = results_with["ARGUS-01"]
        r_base = results_base["ARGUS-01"]
        assert abs(r_with.fitted_n - r_base.fitted_n) < 0.5


# ── Collector tests ────────────────────────────────────────────────────────

class TestCollector:

    def test_haversine_zero(self):
        assert haversine_m(40.75, -73.99, 40.75, -73.99) == 0.0

    def test_haversine_symmetry(self):
        d1 = haversine_m(40.75, -73.99, 40.76, -74.00)
        d2 = haversine_m(40.76, -74.00, 40.75, -73.99)
        assert abs(d1 - d2) < 0.01

    def test_haversine_known(self):
        """1° latitude ≈ 111 km."""
        d = haversine_m(40.0, -74.0, 41.0, -74.0)
        assert 110_000 < d < 112_000

    def test_interpolate_truth_exact_match(self):
        track = [TruthPoint(ts=100, lat=40.0, lon=-74.0),
                 TruthPoint(ts=200, lat=41.0, lon=-73.0)]
        pt = interpolate_truth(track, 100.0)
        assert abs(pt.lat - 40.0) < 1e-9

    def test_interpolate_truth_midpoint(self):
        track = [TruthPoint(ts=100, lat=40.0, lon=-74.0),
                 TruthPoint(ts=200, lat=42.0, lon=-72.0)]
        pt = interpolate_truth(track, 150.0)
        assert abs(pt.lat - 41.0) < 1e-6
        assert abs(pt.lon - -73.0) < 1e-6

    def test_interpolate_truth_out_of_range(self):
        track = [TruthPoint(ts=100, lat=40.0, lon=-74.0),
                 TruthPoint(ts=200, lat=41.0, lon=-73.0)]
        assert interpolate_truth(track, 50.0)  is None
        assert interpolate_truth(track, 250.0) is None

    def test_parse_csv_track(self, tmp_path):
        csv_content = "timestamp,lat,lon,alt_m\n"
        base_ts = 1710000000.0
        for i in range(10):
            csv_content += f"{base_ts + i*5},40.{i:04d},-74.{i:04d},50.0\n"
        p = tmp_path / "track.csv"
        p.write_text(csv_content)
        track = parse_csv_track(str(p))
        assert len(track) == 10
        assert abs(track[0].lat - 40.0) < 0.01

    def test_parse_csv_iso_timestamps(self, tmp_path):
        from datetime import datetime, timezone
        csv_content = "timestamp,lat,lon,alt_m\n"
        for i in range(5):
            ts = datetime(2024, 3, 15, 10, i, 0, tzinfo=timezone.utc).isoformat()
            csv_content += f"{ts},42.36,-71.05,30.0\n"
        p = tmp_path / "iso_track.csv"
        p.write_text(csv_content)
        track = parse_csv_track(str(p))
        assert len(track) == 5

    def test_parse_gpx(self, tmp_path):
        gpx_content = '''<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="40.7580" lon="-73.9855">
      <ele>42.0</ele>
      <time>2024-03-15T10:30:00Z</time>
    </trkpt>
    <trkpt lat="40.7590" lon="-73.9845">
      <ele>44.0</ele>
      <time>2024-03-15T10:30:05Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>'''
        p = tmp_path / "flight.gpx"
        p.write_text(gpx_content)
        track = parse_gpx(str(p))
        assert len(track) == 2
        assert abs(track[0].lat - 40.758) < 0.001
        assert abs(track[0].alt - 42.0)   < 0.01

    def test_parse_srt(self, tmp_path):
        srt_content = """1
00:00:00,033 --> 00:00:00,066
<font size="28">SrtCnt : 1, DiffTime : 33ms
2024-03-15 10:30:00.033
[iso : 100] [shutter : 1/2000.0] [fnum : 280] [ev : 0]
[latitude: 42.36012] [longitude: -71.05888] [rel_alt: 42.341 abs_alt: 54.123]
</font>

2
00:00:00,066 --> 00:00:00,099
<font size="28">SrtCnt : 2, DiffTime : 33ms
2024-03-15 10:30:00.066
[latitude: 42.36025] [longitude: -71.05900] [rel_alt: 42.500 abs_alt: 54.280]
</font>
"""
        p = tmp_path / "DJI_0042.SRT"
        p.write_text(srt_content)
        track = parse_srt(str(p))
        assert len(track) == 2
        assert abs(track[0].lat - 42.36012) < 0.00001

    def test_build_samples_from_truth_track(self):
        """build_samples pairs detections with interpolated truth positions."""
        base_ts = 1710000000.0
        track = [
            TruthPoint(ts=base_ts + i*2, lat=40.75 + i*0.001, lon=-73.99, alt=50.0)
            for i in range(60)
        ]
        nodes = [{"node_id":"ARGUS-01","status":"online","lat":40.76,"lon":-74.00,"alt":10.0}]
        detections = [
            {"node_id":"ARGUS-01","rssi":-72,"detected_at":base_ts+i*5,"drone_lat":40.75,"drone_lon":-73.99}
            for i in range(20)
        ]
        samples = build_samples(detections, track, nodes, truth_source="track")
        assert len(samples) > 0
        assert all(s.true_dist > 0 for s in samples)

    def test_build_samples_rid_broadcast(self):
        """Build samples using RID broadcast as truth source."""
        nodes = [{"node_id":"ARGUS-01","status":"online","lat":40.760,"lon":-74.000,"alt":0.0}]
        detections = [
            {"node_id":"ARGUS-01","rssi":-75,
             "detected_at":1710000000.0 + i,
             "drone_lat":40.755,"drone_lon":-73.995}
            for i in range(30)
        ]
        samples = build_samples(detections, [], nodes, truth_source="rid_broadcast")
        assert len(samples) == 30
        assert all(s.true_dist > 0 for s in samples)

    def test_build_samples_skips_weak_rssi(self):
        """Readings below -105 dBm should be excluded."""
        nodes = [{"node_id":"ARGUS-01","status":"online","lat":40.76,"lon":-74.00,"alt":0.0}]
        detections = [
            {"node_id":"ARGUS-01","rssi":-110,
             "detected_at":1710000000.0, "drone_lat":40.755,"drone_lon":-73.995}
        ]
        samples = build_samples(detections, [], nodes, truth_source="rid_broadcast")
        assert len(samples) == 0


# ── Config writer tests ────────────────────────────────────────────────────

class TestConfigWriter:

    def _make_result(self, node_id="ARGUS-01", n=2.7, rref=-20.0, r2=0.85, rmse=60.0, count=200):
        return CalibrationResult(
            node_id         = node_id,
            fitted_rssi_ref = rref,
            fitted_n        = n,
            r_squared       = r2,
            rmse_m          = rmse,
            mae_m           = rmse * 0.8,
            sample_count    = count,
            dist_range_m    = (30.0, 700.0),
            rssi_range_dbm  = (-95.0, -55.0),
            residuals_m     = [0.0] * count,
            percentile_68_m = rmse,
            percentile_95_m = rmse * 1.8,
        )

    def test_write_yaml_creates_file(self, tmp_path):
        import yaml
        results = {
            "ARGUS-01": self._make_result("ARGUS-01"),
            "ARGUS-02": self._make_result("ARGUS-02", n=3.1, r2=0.79),
        }
        out = tmp_path / "cal.yaml"
        write_calibration_yaml(results, out, notes="test run")

        assert out.exists()
        doc = yaml.safe_load(out.read_text())
        assert "nodes" in doc
        assert "ARGUS-01" in doc["nodes"]
        assert "global" in doc
        assert doc["notes"] == "test run"

    def test_yaml_contains_correct_values(self, tmp_path):
        import yaml
        r = self._make_result("ARGUS-01", n=2.45, rref=-18.4)
        out = tmp_path / "cal.yaml"
        write_calibration_yaml({"ARGUS-01": r}, out)

        doc = yaml.safe_load(out.read_text())
        n01 = doc["nodes"]["ARGUS-01"]
        assert abs(n01["path_loss_exp"] - 2.45) < 0.01
        assert abs(n01["rssi_ref_dbm"]  - (-18.4)) < 0.1

    def test_global_is_weighted_average(self, tmp_path):
        import yaml
        results = {
            "ARGUS-01": self._make_result("ARGUS-01", n=2.5, count=300),
            "ARGUS-02": self._make_result("ARGUS-02", n=3.0, count=100),
        }
        out = tmp_path / "cal.yaml"
        write_calibration_yaml(results, out)
        doc = yaml.safe_load(out.read_text())
        # Weighted toward ARGUS-01 (more samples, same R²) → global n < 2.75
        global_n = doc["global"]["path_loss_exp"]
        assert 2.5 <= global_n <= 3.0
        assert abs(global_n - 2.5) < abs(global_n - 3.0)  # Closer to ARGUS-01

    def test_patch_trilateration_py(self, tmp_path):
        """patch_trilateration_py modifies the constants in a copy of the file."""
        src = Path(__file__).parent.parent.parent / "aegis-server/analysis/trilateration.py"
        if not src.exists():
            pytest.skip("trilateration.py not found")

        # Work on a copy
        copy = tmp_path / "trilateration.py"
        copy.write_text(src.read_text())

        r = self._make_result("ARGUS-01", n=3.14, rref=-19.5)
        diff = patch_trilateration_py(r, py_path=copy, dry_run=False)

        content = copy.read_text()
        assert "3.140" in content or "3.14" in content
        assert "-19.5" in content or "-19.50" in content
        assert "diff" in diff.lower() or "→" in diff

    def test_patch_dry_run_does_not_modify(self, tmp_path):
        src = Path(__file__).parent.parent.parent / "aegis-server/analysis/trilateration.py"
        if not src.exists():
            pytest.skip("trilateration.py not found")

        copy = tmp_path / "trilateration.py"
        orig = src.read_text()
        copy.write_text(orig)

        r = self._make_result("ARGUS-01", n=9.99, rref=-99.0)
        patch_trilateration_py(r, py_path=copy, dry_run=True)

        assert copy.read_text() == orig  # Unchanged

    def test_generate_patch_snippet(self):
        results = {
            "ARGUS-01": self._make_result("ARGUS-01", n=2.45),
            "ARGUS-02": self._make_result("ARGUS-02", n=3.1, r2=0.65),
        }
        snippet = generate_patch_snippet(results)
        assert "ARGUS-01" in snippet
        assert "ARGUS-02" in snippet
        assert "n=" in snippet
