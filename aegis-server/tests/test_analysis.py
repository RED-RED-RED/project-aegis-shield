"""
tests/test_analysis.py
======================
Unit tests for trilateration and threat scoring engines.
No real DB or network needed.
"""

import math
import time
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from analysis.trilateration import (
    TrilaterationEngine, NodeObservation,
    _rssi_to_distance, _latlng_to_xy, _xy_to_latlng, _haversine_m
)
from analysis.threat_scoring import ThreatScorer, ThreatFactors, WEIGHTS


# ── Fixtures ────────────────────────────────────────────────────────────────

# Real nodes placed around a test area (roughly Manhattan island)
NODE_POSITIONS = [
    ("ARGUS-01",  40.7600, -73.9890),   # ~400m NW of drone
    ("ARGUS-02",  40.7555, -73.9920),   # ~500m W of drone
    ("ARGUS-03",  40.7540, -73.9800),   # ~500m SE of drone
    ("ARGUS-04",  40.7625, -73.9820),   # ~500m NE of drone
]

def make_obs(node_id, node_lat, node_lon, rssi, ts=None):
    return NodeObservation(
        node_id=node_id, node_lat=node_lat, node_lon=node_lon,
        node_alt=10.0, rssi=rssi, ts=ts or time.time()
    )


def drone_dict(
    operator_id="OP-US-12345",
    height_agl=50.0,
    speed_h=8.0,
    lat=40.75, lon=-73.99,
    ua_type="helicopter_or_mr",
    detecting_nodes=None,
    description="Test drone",
    last_seen=None,
):
    from datetime import datetime, timezone
    return {
        "operator_id":     operator_id,
        "height_agl":      height_agl,
        "alt_baro":        height_agl + 5,
        "speed_h":         speed_h,
        "lat":             lat,
        "lon":             lon,
        "ua_type":         ua_type,
        "detecting_nodes": detecting_nodes or ["ARGUS-01", "ARGUS-02", "ARGUS-03"],
        "description":     description,
        "last_seen":       last_seen or datetime.now(timezone.utc).isoformat(),
    }


# ── Geometry helpers ─────────────────────────────────────────────────────────

class TestGeometryHelpers:

    def test_rssi_to_distance_at_reference(self):
        """At RSSI_REF distance should be 1m (model-independent check)."""
        from analysis.trilateration import RSSI_REF_DBM
        d = _rssi_to_distance(RSSI_REF_DBM)
        assert abs(d - 1.0) < 0.01

    def test_rssi_to_distance_decreases_with_weaker_signal(self):
        """Weaker signal → greater distance."""
        d1 = _rssi_to_distance(-60)
        d2 = _rssi_to_distance(-80)
        d3 = _rssi_to_distance(-100)
        assert d1 < d2 < d3

    def test_rssi_to_distance_reasonable_range(self):
        """Typical drone RSSI values should give sensible distances (RSSI_REF=-20 @ 1m)."""
        # With RSSI_REF=-20, n=2.7: d = 10^((-20-RSSI)/(27))
        d_near = _rssi_to_distance(-74)     # ~100m
        d_mid  = _rssi_to_distance(-87)     # ~300m
        d_far  = _rssi_to_distance(-93)     # ~500m
        assert 50  < d_near < 300
        assert 150 < d_mid  < 600
        assert 300 < d_far  < 1000

    def test_latlng_roundtrip(self):
        """ENU projection should round-trip back to original lat/lon."""
        lat0, lon0 = 40.75, -73.99
        for lat, lon in [(40.76, -73.98), (40.74, -74.01), (40.75, -73.99)]:
            x, y = _latlng_to_xy(lat, lon, lat0, lon0)
            lat2, lon2 = _xy_to_latlng(x, y, lat0, lon0)
            assert abs(lat - lat2) < 1e-6
            assert abs(lon - lon2) < 1e-6

    def test_haversine_known_distance(self):
        """NYC to roughly 1° north ≈ 111 km."""
        d = _haversine_m(40.0, -74.0, 41.0, -74.0)
        assert 110_000 < d < 112_000

    def test_haversine_zero(self):
        assert _haversine_m(40.0, -74.0, 40.0, -74.0) == 0.0

    def test_haversine_symmetry(self):
        d1 = _haversine_m(40.75, -73.99, 40.68, -74.04)
        d2 = _haversine_m(40.68, -74.04, 40.75, -73.99)
        assert abs(d1 - d2) < 0.01


# ── Trilateration ────────────────────────────────────────────────────────────

class TestTrilaterationEngine:

    def _engine(self):
        return TrilaterationEngine()

    def test_no_result_with_fewer_than_3_nodes(self):
        engine = self._engine()
        drone_lat, drone_lon = 40.748, -73.985

        for i, (nid, nlat, nlon) in enumerate(NODE_POSITIONS[:2]):
            result = engine.update(
                "DRONE1", drone_lat, drone_lon,
                make_obs(nid, nlat, nlon, rssi=-65)
            )
        assert result is None

    def test_result_returned_with_3_nodes(self):
        engine = self._engine()
        drone_lat, drone_lon = 40.748, -73.985

        result = None
        for nid, nlat, nlon in NODE_POSITIONS[:3]:
            result = engine.update(
                "DRONE1", drone_lat, drone_lon,
                make_obs(nid, nlat, nlon, rssi=-65)
            )
        assert result is not None
        assert result.drone_id == "DRONE1"
        assert result.node_count == 3

    def test_estimated_position_is_plausible(self):
        """
        Place a drone at a known position, synthesise RSSI from distances,
        and verify the trilateration output is within 500m of truth.
        (RSSI is noisy by design — 500m is realistic for 3 nodes.)
        """
        engine = self._engine()

        # Drone is at Times Square
        true_lat, true_lon = 40.7580, -73.9855

        result = None
        for nid, nlat, nlon in NODE_POSITIONS:
            true_dist = _haversine_m(nlat, nlon, true_lat, true_lon)
            # Back-calculate ideal RSSI from true distance
            from analysis.trilateration import RSSI_REF_DBM, PATH_LOSS_EXP
            if true_dist < 1:
                true_dist = 1
            rssi = RSSI_REF_DBM - 10 * PATH_LOSS_EXP * math.log10(true_dist)
            rssi = max(int(rssi), -100)

            result = engine.update(
                "DRONE_KNOWN", true_lat, true_lon,
                make_obs(nid, nlat, nlon, rssi=rssi)
            )

        assert result is not None
        error_m = _haversine_m(result.est_lat, result.est_lon, true_lat, true_lon)
        # With ideal (noiseless) RSSI and 4 nodes, should be within 500m
        assert error_m < 500, f"Trilateration error {error_m:.0f}m exceeds 500m threshold"

    def test_mismatch_zero_for_accurate_broadcast(self):
        """If broadcast position matches estimated position, mismatch should be low.
        Drone is placed at the centroid of the node cluster so RSSI geometry is
        well-conditioned and the estimate converges near the true position.
        """
        engine = self._engine()
        from analysis.trilateration import RSSI_REF_DBM, PATH_LOSS_EXP

        # Place drone at centroid of NODE_POSITIONS — well-conditioned geometry
        true_lat = sum(p[1] for p in NODE_POSITIONS) / len(NODE_POSITIONS)
        true_lon = sum(p[2] for p in NODE_POSITIONS) / len(NODE_POSITIONS)

        for nid, nlat, nlon in NODE_POSITIONS:
            dist = max(_haversine_m(nlat, nlon, true_lat, true_lon), 1)
            rssi = int(RSSI_REF_DBM - 10 * PATH_LOSS_EXP * math.log10(dist))
            result = engine.update(
                "DRONE_CENTROID", true_lat, true_lon,
                make_obs(nid, nlat, nlon, rssi=max(rssi, -100))
            )

        # With honest broadcast at node centroid, estimate should be close
        assert result is not None
        # Trilateration with ideal RSSI should yield < 1km mismatch for honest broadcast
        assert result.mismatch_m < 1000, \
            f"Honest broadcast mismatch {result.mismatch_m:.0f}m unexpectedly high"
        # Spoof confidence should be below high-confidence threshold
        assert result.spoof_confidence < 0.8, \
            f"Spoof confidence {result.spoof_confidence:.2f} too high for honest broadcast"

    def test_mismatch_high_for_spoofed_broadcast(self):
        """
        Drone is physically near ARGUS-01 but broadcasts position 2km away.
        Spoof confidence should be elevated.
        """
        engine = self._engine()

        # Physical location: near ARGUS-01 (Times Square)
        phys_lat, phys_lon = 40.7589, -73.9851
        # Spoofed broadcast: 2km south in Brooklyn
        fake_lat, fake_lon = 40.720, -73.990

        from analysis.trilateration import RSSI_REF_DBM, PATH_LOSS_EXP

        for nid, nlat, nlon in NODE_POSITIONS:
            dist = max(_haversine_m(nlat, nlon, phys_lat, phys_lon), 1)
            rssi = int(RSSI_REF_DBM - 10 * PATH_LOSS_EXP * math.log10(dist))
            result = engine.update(
                "DRONE_SPOOF", fake_lat, fake_lon,
                make_obs(nid, nlat, nlon, rssi=max(rssi, -100))
            )

        assert result is not None
        assert result.mismatch_m > 1000, f"Expected mismatch > 1km, got {result.mismatch_m:.0f}m"

    def test_weak_rssi_ignored(self):
        """Observations below MIN_RSSI_DBM should be ignored."""
        engine = self._engine()
        obs = make_obs("ARGUS-01", 40.75, -73.99, rssi=-105)  # below -100 threshold
        result = engine.update("DRONE_WEAK", 40.75, -73.99, obs)
        assert result is None

    def test_stale_observations_expire(self):
        """Observations older than obs_window_s should be expired."""
        engine = self._engine()
        engine._obs_window_s = 0.05   # 50ms for test speed

        now = time.time()
        for nid, nlat, nlon in NODE_POSITIONS[:3]:
            engine.update("DRONE_STALE", 40.75, -73.99,
                          make_obs(nid, nlat, nlon, rssi=-65, ts=now - 1.0))  # 1s old

        # Observations should have expired, engine state clean
        import time as tmod; tmod.sleep(0.1)
        result = engine.update("DRONE_STALE", 40.75, -73.99,
                               make_obs("ARGUS-01", 40.75, -73.99, rssi=-65))
        # Only 1 fresh obs → no result
        assert result is None


# ── Threat Scoring ───────────────────────────────────────────────────────────

class TestThreatScorer:

    def _scorer(self):
        return ThreatScorer()

    def test_weights_sum_to_100(self):
        assert sum(WEIGHTS.values()) == 100

    def test_clean_compliant_drone_low_score(self):
        scorer = self._scorer()
        d = drone_dict(operator_id="OP-US-12345", height_agl=50, speed_h=5,
                       ua_type="helicopter_or_mr", detecting_nodes=["N1","N2","N3"],
                       description="Aerial photography")
        result = scorer.score(d, mlat_result=None)
        assert result.level == "low"
        assert result.score < 40

    def test_no_operator_id_raises_score(self):
        scorer = self._scorer()
        d = drone_dict(operator_id="")
        result = scorer.score(d, mlat_result=None)
        # no_operator_id weight = 30
        assert result.no_operator_id == 1.0
        assert result.score >= 30

    def test_null_island_coordinates_flagged(self):
        scorer = self._scorer()
        d = drone_dict(lat=0.0, lon=0.0, operator_id="OP-US-12345")
        result = scorer.score(d, mlat_result=None)
        assert result.position_mismatch > 0.5

    def test_high_altitude_no_rid_scores_medium(self):
        scorer = self._scorer()
        # height_agl=300m (2.46x FAA limit) → high_altitude_no_rid factor=1.0 → +15pts
        # no_operator_id → +30pts, no_description → +3pts → total 48+
        d = drone_dict(operator_id="", height_agl=300, description="")
        result = scorer.score(d, mlat_result=None)
        assert result.high_altitude_no_rid > 0
        assert result.score >= 40

    def test_single_node_scores_full_weight(self):
        scorer = self._scorer()
        d = drone_dict(detecting_nodes=["ARGUS-01"])
        result = scorer.score(d, mlat_result=None)
        assert result.single_node_only == 1.0

    def test_two_nodes_partial_weight(self):
        scorer = self._scorer()
        d = drone_dict(detecting_nodes=["ARGUS-01","ARGUS-02"])
        result = scorer.score(d, mlat_result=None)
        assert 0 < result.single_node_only < 1.0

    def test_implausible_speed_maxes_factor(self):
        scorer = self._scorer()
        d = drone_dict(speed_h=55.0)  # above 50 m/s cap
        result = scorer.score(d, mlat_result=None)
        assert result.high_speed == 1.0

    def test_unknown_ua_type_maxes_factor(self):
        scorer = self._scorer()
        d = drone_dict(ua_type="unknown_0")
        result = scorer.score(d, mlat_result=None)
        assert result.unknown_ua_type == 1.0

    def test_ground_obstacle_ua_near_max(self):
        scorer = self._scorer()
        d = drone_dict(ua_type="ground_obstacle")
        result = scorer.score(d, mlat_result=None)
        assert result.unknown_ua_type >= 0.9

    def test_no_description_adds_points(self):
        scorer = self._scorer()
        d = drone_dict(description="")
        result_no_desc = scorer.score(d)
        d2 = drone_dict(description="Aerial survey")
        result_with_desc = scorer.score(d2)
        assert result_no_desc.score > result_with_desc.score

    def test_high_mlat_mismatch_raises_to_high(self):
        """With large position mismatch drone should hit HIGH threshold.
        Factors: no_op=30 + mismatch≈23 + high_alt=15 + single_node=7 + no_desc=3 = 78
        """
        from unittest.mock import MagicMock
        scorer = self._scorer()
        d = drone_dict(
            operator_id="", height_agl=300, description="",
            ua_type="unknown_0", detecting_nodes=["N1"]
        )

        mlat = MagicMock()
        mlat.mismatch_m = 800.0
        mlat.spoof_confidence = 0.92

        result = scorer.score(d, mlat_result=mlat)
        assert result.level == "high", f"score={result.score}, factors={result.to_dict()['factors']}"
        assert result.score >= 70

    def test_score_bounded_0_to_100(self):
        """Worst case drone should not exceed 100."""
        scorer = self._scorer()
        d = drone_dict(
            operator_id="", height_agl=500, speed_h=60,
            lat=0.0, lon=0.0, ua_type="unknown_0",
            detecting_nodes=["ARGUS-01"], description=""
        )
        result = scorer.score(d)
        assert 0 <= result.score <= 100

    def test_threat_factors_to_dict(self):
        scorer = self._scorer()
        d = drone_dict()
        result = scorer.score(d)
        d_out = result.to_dict()
        assert "score" in d_out
        assert "level" in d_out
        assert "factors" in d_out
        assert set(d_out["factors"].keys()) == set(WEIGHTS.keys())
