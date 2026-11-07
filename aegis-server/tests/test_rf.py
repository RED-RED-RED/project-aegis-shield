"""
tests/test_rf.py
================
RF contact path: scoring, Remote ID correlation, the subscriber handler, and
the "RF link, no Remote ID" alert. Mocked DB pool and broadcaster, no services.
"""

import time
import sys, os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.config import Settings
from models.schemas import RFDetectionEvent, RFSignal, NodePosition
from analysis.rf_scoring import score_rf_contact, SIGNAL_SEVERITY


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_rf_event(signal_type="elrs_2g", band="2g4", rssi=-55.0, confidence=0.9,
                  center=2440_000_000, node_id="ARGUS-01",
                  node_lat=40.0, node_lon=-100.0, contact_key=None):
    return RFDetectionEvent(
        node_id=node_id,
        node_position=NodePosition(lat=node_lat, lon=node_lon, alt=300.0),
        signal=RFSignal(
            signal_type=signal_type, band=band, center_freq_hz=center,
            bandwidth_hz=2_000_000, rssi=rssi, snr_db=20.0,
            confidence=confidence, hop_rate_hz=200.0,
        ),
        ts=time.time(),
        contact_key=contact_key,
    )


def mock_pool(tracks=None, insert_row=None):
    """A pool whose connection returns `tracks` for fetch() and insert_row for fetchrow()."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=tracks or [])
    conn.fetchrow = AsyncMock(return_value=insert_row or {
        "contact_id": "ARGUS-01:elrs_2g:2g4", "signal_type": "elrs_2g",
        "band": "2g4", "best_rssi": -55.0, "last_rssi": -55.0,
        "detecting_nodes": ["ARGUS-01"], "detection_count": 1, "correlated": False,
    })
    conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def acquire():
        yield conn
    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


# ── Scoring ──────────────────────────────────────────────────────────────────

class TestRFScoring:
    def test_dark_drone_link_scores_high(self):
        t = score_rf_contact({
            "signal_type": "dji_ocusync", "band": "2g4", "correlated": False,
            "best_rssi": -50, "detecting_nodes": ["A", "B", "C"], "detection_count": 12,
        })
        assert t.level == "high" and t.score >= 70

    def test_correlated_link_scores_low(self):
        """Same strong link, but a Remote ID broadcast lines up with it."""
        t = score_rf_contact({
            "signal_type": "dji_ocusync", "band": "2g4", "correlated": True,
            "best_rssi": -50, "detecting_nodes": ["A"], "detection_count": 12,
        })
        assert t.no_rid_correlation == 0.0
        assert t.level in ("low", "medium")

    def test_lora_does_not_over_alert(self):
        """A single weak 900 MHz LoRa burst should not read as a drone."""
        t = score_rf_contact({
            "signal_type": "lora_900", "band": "900", "correlated": False,
            "best_rssi": -85, "detecting_nodes": ["A"], "detection_count": 1,
        })
        assert t.level == "low"

    def test_multi_node_raises_score(self):
        one = score_rf_contact({"signal_type": "elrs_2g", "band": "2g4",
                                "correlated": False, "best_rssi": -70,
                                "detecting_nodes": ["A"], "detection_count": 3})
        three = score_rf_contact({"signal_type": "elrs_2g", "band": "2g4",
                                  "correlated": False, "best_rssi": -70,
                                  "detecting_nodes": ["A", "B", "C"], "detection_count": 3})
        assert three.score > one.score

    def test_lone_900_link_capped_low(self):
        """A strong, sustained 900 MHz control link from a single node stays
        'low' — on its own it can't be told from a smart meter."""
        t = score_rf_contact({
            "signal_type": "elrs_900", "band": "900", "correlated": False,
            "best_rssi": -50, "detecting_nodes": ["A"], "detection_count": 20,
        })
        assert t.level == "low"

    def test_900_link_escalates_with_multiple_nodes(self):
        """The same 900 link seen by several nodes is corroborated and escalates."""
        lone = score_rf_contact({
            "signal_type": "elrs_900", "band": "900", "correlated": False,
            "best_rssi": -60, "detecting_nodes": ["A"], "detection_count": 5,
        })
        many = score_rf_contact({
            "signal_type": "elrs_900", "band": "900", "correlated": False,
            "best_rssi": -60, "detecting_nodes": ["A", "B", "C"], "detection_count": 5,
        })
        assert lone.level == "low"
        assert many.level in ("medium", "high") and many.score > lone.score

    def test_no_rid_weight_lower_on_900_than_2g4(self):
        """A dark link's no-RID contribution is far smaller on 900 than on 2.4."""
        common = dict(correlated=False, best_rssi=-60,
                      detecting_nodes=["A"], detection_count=5)
        g24 = score_rf_contact({"signal_type": "elrs_2g", "band": "2g4", **common})
        g900 = score_rf_contact({"signal_type": "elrs_900", "band": "900", **common})
        assert g24.no_rid_correlation > 5 * g900.no_rid_correlation

    def test_5g8_video_still_alerts_without_corroboration(self):
        """5.8 GHz video implies a flying FPV aircraft, so a lone dark link
        there is still suspicious and is not gated like 900."""
        t = score_rf_contact({
            "signal_type": "fpv_video_5g", "band": "5g8", "correlated": False,
            "best_rssi": -55, "detecting_nodes": ["A"], "detection_count": 8,
        })
        assert t.level in ("medium", "high")

    def test_every_signal_type_has_severity(self):
        for name in ("dji_ocusync", "elrs_2g", "elrs_900", "crossfire_900",
                     "fpv_video_5g", "fpv_video_analog", "lora_900", "unknown"):
            assert name in SIGNAL_SEVERITY

    def test_score_is_bounded(self):
        t = score_rf_contact({"signal_type": "dji_ocusync", "band": "2g4",
                              "correlated": False, "best_rssi": -10,
                              "detecting_nodes": ["A", "B", "C", "D"],
                              "detection_count": 999})
        assert 0 <= t.score <= 100


# ── Correlation ──────────────────────────────────────────────────────────────

class TestCorrelation:
    @pytest.fixture
    def subscriber(self):
        with patch("mqtt.subscriber.AlertEngine"), \
             patch("mqtt.subscriber.AnalysisPipeline"), \
             patch("mqtt.subscriber.WSBroadcaster"):
            from mqtt.subscriber import MQTTSubscriber
            return MQTTSubscriber(Settings())

    async def test_correlates_with_nearby_rid_track(self, subscriber):
        _, conn = mock_pool(tracks=[{"drone_id": "DJI-1", "lat": 40.0001, "lon": -99.9999}])
        event = make_rf_event(node_lat=40.0, node_lon=-100.0)
        correlated, drone_id = await subscriber._correlate_rf(conn, event)
        assert correlated and drone_id == "DJI-1"

    async def test_no_correlation_when_rid_track_far(self, subscriber):
        _, conn = mock_pool(tracks=[{"drone_id": "DJI-1", "lat": 40.5, "lon": -100.9}])
        event = make_rf_event(node_lat=40.0, node_lon=-100.0)
        correlated, drone_id = await subscriber._correlate_rf(conn, event)
        assert not correlated and drone_id is None

    async def test_no_correlation_when_no_tracks(self, subscriber):
        _, conn = mock_pool(tracks=[])
        correlated, drone_id = await subscriber._correlate_rf(conn, make_rf_event())
        assert not correlated and drone_id is None

    async def test_picks_closest_track(self, subscriber):
        _, conn = mock_pool(tracks=[
            {"drone_id": "FAR", "lat": 40.004, "lon": -100.0},
            {"drone_id": "NEAR", "lat": 40.0001, "lon": -99.9999},
        ])
        event = make_rf_event(node_lat=40.0, node_lon=-100.0)
        _, drone_id = await subscriber._correlate_rf(conn, event)
        assert drone_id == "NEAR"


# ── Handler end to end ───────────────────────────────────────────────────────

class TestRFHandler:
    @pytest.fixture
    def subscriber(self):
        with patch("mqtt.subscriber.AlertEngine"), \
             patch("mqtt.subscriber.AnalysisPipeline"), \
             patch("mqtt.subscriber.WSBroadcaster"):
            from mqtt.subscriber import MQTTSubscriber
            s = MQTTSubscriber(Settings())
            s.alert_engine.evaluate_rf = AsyncMock()
            s.broadcaster.broadcast_rf_detection = AsyncMock()
            return s

    async def test_handler_upserts_scores_alerts_broadcasts(self, subscriber):
        pool, conn = mock_pool(
            tracks=[],
            insert_row={"contact_id": "ARGUS-01:dji_ocusync:2g4",
                        "signal_type": "dji_ocusync", "band": "2g4",
                        "best_rssi": -50.0, "last_rssi": -50.0,
                        "detecting_nodes": ["ARGUS-01"], "detection_count": 5,
                        "correlated": False},
        )
        with patch("mqtt.subscriber.get_pool", new=AsyncMock(return_value=pool)):
            await subscriber._handle_rf_detection(
                make_rf_event(signal_type="dji_ocusync", rssi=-50.0).model_dump())

        # Upsert happened, threat UPDATE happened
        assert conn.fetchrow.await_count == 1
        assert conn.execute.await_count == 1
        # Alert + broadcast called with a high threat
        subscriber.alert_engine.evaluate_rf.assert_awaited_once()
        subscriber.broadcaster.broadcast_rf_detection.assert_awaited_once()
        threat = subscriber.alert_engine.evaluate_rf.await_args.args[2]
        assert threat.level == "high"

    async def test_contact_key_override_used(self, subscriber):
        pool, conn = mock_pool(tracks=[])
        with patch("mqtt.subscriber.get_pool", new=AsyncMock(return_value=pool)):
            await subscriber._handle_rf_detection(
                make_rf_event(contact_key="global-emitter-7").model_dump())
        contact_id = conn.fetchrow.await_args.args[1]
        assert contact_id == "global-emitter-7"

    async def test_default_contact_id_shape(self, subscriber):
        pool, conn = mock_pool(tracks=[])
        with patch("mqtt.subscriber.get_pool", new=AsyncMock(return_value=pool)):
            await subscriber._handle_rf_detection(
                make_rf_event(signal_type="elrs_2g", band="2g4").model_dump())
        assert conn.fetchrow.await_args.args[1] == "ARGUS-01:elrs_2g:2g4"

    async def test_invalid_payload_ignored(self, subscriber):
        pool, conn = mock_pool()
        with patch("mqtt.subscriber.get_pool", new=AsyncMock(return_value=pool)):
            await subscriber._handle_rf_detection({"garbage": True})
        conn.fetchrow.assert_not_called()
        subscriber.broadcaster.broadcast_rf_detection.assert_not_called()


# ── Alert engine ─────────────────────────────────────────────────────────────

class TestRFAlert:
    @pytest.fixture
    def engine(self):
        with patch("mqtt.alert_engine.WSBroadcaster"), \
             patch("mqtt.alert_engine.AlgoNotifier"):
            from mqtt.alert_engine import AlertEngine
            e = AlertEngine(Settings())
            e._emit = AsyncMock()
            return e

    async def test_dark_link_emits_alert(self, engine):
        event = make_rf_event(signal_type="dji_ocusync", confidence=0.9)
        threat = score_rf_contact({"signal_type": "dji_ocusync", "band": "2g4",
                                   "correlated": False, "best_rssi": -50,
                                   "detecting_nodes": ["A", "B"], "detection_count": 8})
        await engine.evaluate_rf(event, "c1", threat, correlated=False, corr_drone_id=None)
        engine._emit.assert_awaited_once()
        assert engine._emit.await_args.args[0]["category"] == "rf_no_rid"

    async def test_correlated_link_no_alert(self, engine):
        event = make_rf_event(signal_type="dji_ocusync", confidence=0.9)
        threat = score_rf_contact({"signal_type": "dji_ocusync", "band": "2g4",
                                   "correlated": True, "best_rssi": -50,
                                   "detecting_nodes": ["A"], "detection_count": 8})
        await engine.evaluate_rf(event, "c1", threat, correlated=True, corr_drone_id="DJI-1")
        engine._emit.assert_not_called()

    async def test_low_confidence_no_alert(self, engine):
        event = make_rf_event(signal_type="unknown", confidence=0.3)
        threat = score_rf_contact({"signal_type": "unknown", "band": "900",
                                   "correlated": False, "best_rssi": -80,
                                   "detecting_nodes": ["A"], "detection_count": 1})
        await engine.evaluate_rf(event, "c1", threat, correlated=False, corr_drone_id=None)
        engine._emit.assert_not_called()

    async def test_dedup_suppresses_repeat(self, engine):
        event = make_rf_event(signal_type="elrs_2g", confidence=0.9)
        threat = score_rf_contact({"signal_type": "elrs_2g", "band": "2g4",
                                   "correlated": False, "best_rssi": -55,
                                   "detecting_nodes": ["A", "B"], "detection_count": 8})
        await engine.evaluate_rf(event, "c1", threat, False, None)
        await engine.evaluate_rf(event, "c1", threat, False, None)
        assert engine._emit.await_count == 1
