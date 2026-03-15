"""
tests/test_alert_engine.py
==========================
Unit tests for the alert engine rule evaluation.
Uses mocked DB pool and WebSocket broadcaster so no real services needed.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.config import Settings
from models.schemas import DetectionEvent, DroneData, NodePosition


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def make_event(
    drone_id="TEST001",
    operator_id="OP-US-12345",
    lat=42.36, lon=-71.06,
    alt_baro=50.0, height_agl=45.0,
    speed_h=5.0, status="airborne",
    node_id="ARGUS-01", transport="wifi_nan",
):
    return DetectionEvent(
        node_id=node_id,
        transport=transport,
        rssi=-68,
        src_addr="fa:3b:92:0e:11:22",
        node_position=NodePosition(lat=42.35, lon=-71.07, alt=10.0),
        drone=DroneData(
            id=drone_id,
            id_type="serial_number",
            ua_type="helicopter_or_mr",
            status=status,
            lat=lat, lon=lon,
            alt_baro=alt_baro,
            alt_geo=alt_baro + 1,
            height_agl=height_agl,
            speed_h=speed_h,
            speed_v=0.0,
            heading=127.0,
            operator_id=operator_id,
            description="",
        ),
        ts=time.time(),
    )


def build_mock_pool(detection_count=2):
    """
    Build a properly structured asyncpg pool mock.
    pool.acquire() must be an async context manager returning a connection.
    """
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"detection_count": detection_count})
    mock_conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def acquire():
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = acquire
    return mock_pool, mock_conn


@pytest.fixture
def settings():
    return Settings(
        db_host="localhost", db_port=5432,
        db_name="test", db_user="test", db_password="test",
        mqtt_host="localhost", mqtt_port=1883,
        mqtt_user="test", mqtt_password="test",
        alert_no_rid_min_alt_m=30.0,
        alert_speed_threshold_ms=30.0,
        alert_dedup_window_s=60,
    )


# ------------------------------------------------------------------ #
# Alert engine rule tests
# ------------------------------------------------------------------ #

class TestAlertEngineRules:

    def _get_engine(self, settings):
        from mqtt.alert_engine import AlertEngine
        return AlertEngine(settings)

    def _patch_pool(self, pool):
        """Patch get_pool() to return our mock pool directly (not a coroutine)."""
        async def _get_pool():
            return pool
        return patch("mqtt.alert_engine.get_pool", new=_get_pool)

    @pytest.mark.asyncio
    async def test_no_alert_for_compliant_drone(self, settings):
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(operator_id="OP-US-12345", height_agl=50.0, speed_h=5.0)

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert len(emitted) == 0, f"Expected no alerts, got: {[a['category'] for a in emitted]}"

    @pytest.mark.asyncio
    async def test_no_rid_alert_when_airborne(self, settings):
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(operator_id="", height_agl=50.0)

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert any(a["category"] == "no_rid" for a in emitted)
        assert any(a["level"] == "high" for a in emitted)

    @pytest.mark.asyncio
    async def test_no_rid_suppressed_below_altitude(self, settings):
        """No RID alert should NOT fire if drone is below the min altitude threshold."""
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(operator_id="", height_agl=10.0)  # below 30m threshold

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert not any(a["category"] == "no_rid" for a in emitted)

    @pytest.mark.asyncio
    async def test_speed_alert(self, settings):
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(speed_h=35.0, operator_id="OP-US-12345")

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert any(a["category"] == "speed" for a in emitted)
        assert any(a["level"] == "medium" for a in emitted)

    @pytest.mark.asyncio
    async def test_altitude_alert_without_operator(self, settings):
        """Above 400ft (121.92m) AGL without operator ID → medium alert."""
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(operator_id="", height_agl=130.0)

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert any(a["category"] == "altitude" for a in emitted)

    @pytest.mark.asyncio
    async def test_null_island_spoof_detection(self, settings):
        """Drone at (0,0) while airborne → spoofed_position alert."""
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(lat=0.0, lon=0.0, height_agl=50.0, operator_id="OP-US-12345")

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert any(a["category"] == "spoofed_position" for a in emitted)

    @pytest.mark.asyncio
    async def test_alert_deduplication(self, settings):
        """Same alert category+drone within dedup window should only fire once."""
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool()
        event = make_event(operator_id="", height_agl=50.0)

        emitted_calls = []
        async def noop_emit(alert):
            emitted_calls.append(alert["category"])
        engine._emit = noop_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)
            first_count = emitted_calls.count("no_rid")
            # Second call — same drone, same category — should be deduped
            await engine.evaluate(event)
            second_count = emitted_calls.count("no_rid")

        assert first_count == 1
        assert second_count == 1  # No new emission within dedup window

    @pytest.mark.asyncio
    async def test_new_drone_low_alert(self, settings):
        """First detection of a drone (count=1) → low 'new_drone' alert."""
        engine = self._get_engine(settings)
        pool, _ = build_mock_pool(detection_count=1)  # first detection
        event = make_event(operator_id="OP-US-12345", height_agl=50.0, speed_h=5.0)

        emitted = []
        async def fake_emit(alert): emitted.append(alert)
        engine._emit = fake_emit

        with self._patch_pool(pool), patch("mqtt.ws_broadcaster._manager") as m:
            m.count = 0
            await engine.evaluate(event)

        assert any(a["category"] == "new_drone" for a in emitted)
        assert any(a["level"] == "low" for a in emitted)


# ------------------------------------------------------------------ #
# Detection event validation tests (no DB needed)
# ------------------------------------------------------------------ #

class TestDetectionEventValidation:

    def test_valid_event_parses(self):
        event = make_event()
        assert event.drone.id == "TEST001"
        assert event.node_id == "ARGUS-01"
        assert event.drone.lat == 42.36

    def test_missing_operator_id_is_falsy(self):
        event = make_event(operator_id="")
        assert not event.drone.operator_id

    def test_node_position_fields(self):
        event = make_event()
        assert event.node_position.lat == 42.35
        assert event.node_position.alt == 10.0

    def test_drone_speed_stored(self):
        event = make_event(speed_h=12.5)
        assert event.drone.speed_h == 12.5

    def test_transport_field(self):
        event = make_event(transport="bluetooth")
        assert event.transport == "bluetooth"
