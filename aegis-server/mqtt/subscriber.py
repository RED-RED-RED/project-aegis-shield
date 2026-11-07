"""
mqtt/subscriber.py
==================
Async MQTT subscriber. Connects to Mosquitto, subscribes to all node topics,
parses incoming messages, writes to TimescaleDB, runs the alert engine,
and pushes live updates to WebSocket clients.

Topic subscriptions:
  argus/+/detection  → DetectionEvent → write to DB + alert engine + WS broadcast
  argus/+/heartbeat  → NodeHeartbeat  → upsert node health in DB + WS broadcast
  argus/+/status     → node online/offline → upsert node status
  argus/+/rf_event   → RF burst → write to rf_events table

Uses aiomqtt (async wrapper around paho).
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiomqtt
from pydantic import ValidationError

from core.config import Settings
from db.database import get_pool
from models.schemas import DetectionEvent, NodeHeartbeat, RFDetectionEvent
from mqtt.alert_engine import AlertEngine
from mqtt.ws_broadcaster import WSBroadcaster
from analysis.pipeline import AnalysisPipeline
from analysis.rf_scoring import score_rf_contact

log = logging.getLogger("mqtt")


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres."""
    from math import radians, sin, cos, asin, sqrt
    r = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _rssi_range_m(rssi: float) -> float:
    """
    Very rough distance ring from RSSI, for drawing a single-node contact.
    Not calibrated: -40 dBm ~ 50 m (close), -90 dBm ~ 2 km (far). Clamped.
    This is a visual aid, never a position fix.
    """
    if rssi is None:
        return 1000.0
    frac = min(max((-40 - rssi) / 50.0, 0.0), 1.0)   # 0 at -40, 1 at -90
    return 50.0 + frac * 1950.0


class MQTTSubscriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.alert_engine = AlertEngine(settings)
        self.broadcaster = WSBroadcaster()
        self.pipeline = AnalysisPipeline(settings)
        # Track detection timestamps for rate calculation
        self._detection_times: list[float] = []

    async def run(self):
        """Main async MQTT loop — reconnects automatically on disconnect."""
        backoff = 1
        while True:
            try:
                await self._connect_and_subscribe()
                backoff = 1
            except aiomqtt.MqttError as e:
                log.warning(f"MQTT error: {e} — reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except asyncio.CancelledError:
                log.info("MQTT subscriber cancelled.")
                return
            except Exception as e:
                log.error(f"Unexpected MQTT error: {e}")
                await asyncio.sleep(backoff)

    async def _connect_and_subscribe(self):
        prefix = self.settings.mqtt_topic_prefix
        async with aiomqtt.Client(
            hostname=self.settings.mqtt_host,
            port=self.settings.mqtt_port,
            username=self.settings.mqtt_user,
            password=self.settings.mqtt_password,
            identifier="aegis-server",
            keepalive=30,
        ) as client:
            log.info(f"MQTT connected to {self.settings.mqtt_host}:{self.settings.mqtt_port}")

            # Subscribe to all node topics
            await client.subscribe(f"{prefix}/+/detection")
            await client.subscribe(f"{prefix}/+/heartbeat")
            await client.subscribe(f"{prefix}/+/status")
            await client.subscribe(f"{prefix}/+/rf_event")
            await client.subscribe(f"{prefix}/+/rf_detection")
            log.info(f"Subscribed to {prefix}/#")

            async for message in client.messages:
                topic = str(message.topic)
                try:
                    payload = json.loads(message.payload.decode("utf-8"))
                    await self._dispatch(topic, payload)
                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON on topic {topic}")
                except Exception as e:
                    log.error(f"Error processing {topic}: {e}", exc_info=True)

    async def _dispatch(self, topic: str, payload: dict):
        """Route incoming MQTT message to the right handler."""
        parts = topic.split("/")
        # Require exactly <prefix>/<node_id>/<type> — reject malformed topics
        if len(parts) != 3:
            log.warning(f"Ignoring message on unexpected topic: {topic}")
            return
        msg_type = parts[2]

        if msg_type == "detection":
            await self._handle_detection(payload)
        elif msg_type == "heartbeat":
            await self._handle_heartbeat(payload)
        elif msg_type == "status":
            await self._handle_status(payload)
        elif msg_type == "rf_event":
            await self._handle_rf_event(payload)
        elif msg_type == "rf_detection":
            await self._handle_rf_detection(payload)

    # ------------------------------------------------------------------ #
    # Detection handler
    # ------------------------------------------------------------------ #

    async def _handle_detection(self, payload: dict):
        try:
            event = DetectionEvent(**payload)
        except ValidationError as e:
            log.warning(f"Detection validation error: {e}")
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Insert into detections (append-only)
                await conn.execute("""
                    INSERT INTO detections (
                        detected_at, node_id, transport, rssi, src_addr,
                        drone_id, drone_lat, drone_lon,
                        alt_baro, alt_geo, height_agl,
                        speed_h, speed_v, heading, status,
                        id_type, ua_type, operator_id,
                        operator_lat, operator_lon, description,
                        node_lat, node_lon, node_alt
                    ) VALUES (
                        to_timestamp($1), $2, $3, $4, $5,
                        $6, $7, $8,
                        $9, $10, $11,
                        $12, $13, $14, $15,
                        $16, $17, $18,
                        $19, $20, $21,
                        $22, $23, $24
                    )
                """,
                    event.ts, event.node_id, event.transport,
                    event.rssi, event.src_addr,
                    event.drone.id, event.drone.lat, event.drone.lon,
                    event.drone.alt_baro, event.drone.alt_geo, event.drone.height_agl,
                    event.drone.speed_h, event.drone.speed_v, event.drone.heading,
                    event.drone.status,
                    event.drone.id_type, event.drone.ua_type, event.drone.operator_id,
                    event.drone.operator_lat, event.drone.operator_lon,
                    event.drone.description,
                    event.node_position.lat, event.node_position.lon,
                    event.node_position.alt,
                )

                # 2. Upsert drone_tracks (live state — one row per drone)
                has_valid_rid = bool(
                    event.drone.operator_id
                    and event.drone.operator_id.strip()
                    and event.drone.id
                )
                await conn.execute("""
                    INSERT INTO drone_tracks (
                        drone_id, first_seen, last_seen,
                        last_node_id, last_transport, last_rssi,
                        lat, lon, alt_baro, alt_geo, height_agl,
                        speed_h, speed_v, heading, status,
                        id_type, ua_type, operator_id,
                        operator_lat, operator_lon, description,
                        has_valid_rid, detection_count, detecting_nodes
                    ) VALUES (
                        $1, to_timestamp($2), to_timestamp($2),
                        $3, $4, $5,
                        $6, $7, $8, $9, $10,
                        $11, $12, $13, $14,
                        $15, $16, $17,
                        $18, $19, $20,
                        $21, 1, ARRAY[$3]
                    )
                    ON CONFLICT (drone_id) DO UPDATE SET
                        last_seen       = to_timestamp($2),
                        last_node_id    = $3,
                        last_transport  = $4,
                        last_rssi       = $5,
                        lat             = $6,
                        lon             = $7,
                        alt_baro        = $8,
                        alt_geo         = $9,
                        height_agl      = $10,
                        speed_h         = $11,
                        speed_v         = $12,
                        heading         = $13,
                        status          = $14,
                        id_type         = $15,
                        ua_type         = $16,
                        operator_id     = COALESCE($17, drone_tracks.operator_id),
                        operator_lat    = COALESCE($18, drone_tracks.operator_lat),
                        operator_lon    = COALESCE($19, drone_tracks.operator_lon),
                        description     = COALESCE($20, drone_tracks.description),
                        has_valid_rid   = $21,
                        detection_count = drone_tracks.detection_count + 1,
                        detecting_nodes = (
                            SELECT ARRAY(
                                SELECT DISTINCT unnest(
                                    drone_tracks.detecting_nodes || ARRAY[$3]
                                )
                            )
                        )
                """,
                    event.drone.id, event.ts,
                    event.node_id, event.transport, event.rssi,
                    event.drone.lat, event.drone.lon,
                    event.drone.alt_baro, event.drone.alt_geo, event.drone.height_agl,
                    event.drone.speed_h, event.drone.speed_v, event.drone.heading,
                    event.drone.status,
                    event.drone.id_type, event.drone.ua_type, event.drone.operator_id,
                    event.drone.operator_lat, event.drone.operator_lon,
                    event.drone.description,
                    has_valid_rid,
                )

        # 3. Run alert engine (outside transaction — non-critical)
        await self.alert_engine.evaluate(event)

        # 3b. Run analysis pipeline (trilateration + threat scoring)
        async with pool.acquire() as _ac:
            await self.pipeline.process(event, _ac)

        # 4. Track detection rate
        now = time.time()
        self._detection_times.append(now)
        self._detection_times = [t for t in self._detection_times if now - t < 300]

        # 5. Broadcast to WebSocket clients
        await self.broadcaster.broadcast_detection(event)

    # ------------------------------------------------------------------ #
    # Heartbeat handler
    # ------------------------------------------------------------------ #

    async def _handle_heartbeat(self, payload: dict):
        try:
            hb = NodeHeartbeat(**payload)
        except (ValidationError, KeyError) as e:
            log.warning(f"Heartbeat validation error: {e}")
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO nodes (
                    node_id, status, last_seen,
                    lat, lon, alt, gps_fix, satellites,
                    cpu_pct, mem_pct, disk_pct, temp_c, uptime_s,
                    radios, jamming_state, spoofing_state, survey_complete
                ) VALUES ($1, 'online', to_timestamp($2), $3, $4, $5, $6, $7,
                          $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (node_id) DO UPDATE SET
                    status          = 'online',
                    last_seen       = to_timestamp($2),
                    lat             = $3,
                    lon             = $4,
                    alt             = $5,
                    gps_fix         = $6,
                    satellites      = $7,
                    cpu_pct         = $8,
                    mem_pct         = $9,
                    disk_pct        = $10,
                    temp_c          = $11,
                    uptime_s        = $12,
                    radios          = $13,
                    jamming_state   = $14,
                    spoofing_state  = $15,
                    survey_complete = $16
            """,
                hb.node_id, hb.ts,
                hb.gps.get("lat", 0.0),
                hb.gps.get("lon", 0.0),
                hb.gps.get("alt", 0.0),
                hb.gps.get("fix", False),
                hb.gps.get("sats", 0),
                hb.system.get("cpu_pct"),
                hb.system.get("mem_pct"),
                hb.system.get("disk_pct"),
                hb.system.get("temp_c"),
                hb.system.get("uptime_s"),
                hb.radios or [],
                hb.jamming_state,
                hb.spoofing_state,
                hb.gps.get("survey_complete", False),
            )

        # Fire GPS jamming alert if needed
        await self.alert_engine.evaluate_heartbeat(hb)

        await self.broadcaster.broadcast_node_update(hb.node_id, "online", payload)

    # ------------------------------------------------------------------ #
    # Status (LWT) handler
    # ------------------------------------------------------------------ #

    async def _handle_status(self, payload: dict):
        node_id = payload.get("node_id")
        status = payload.get("status", "offline")
        if not node_id:
            return

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO nodes (node_id, status, last_seen)
                VALUES ($1, $2, NOW())
                ON CONFLICT (node_id) DO UPDATE SET
                    status    = $2,
                    last_seen = NOW()
            """, node_id, status)

        if status == "offline":
            log.warning(f"Node {node_id} went offline (LWT received)")
            await self.alert_engine.node_offline(node_id)

        await self.broadcaster.broadcast_node_update(node_id, status, payload)

    # ------------------------------------------------------------------ #
    # RF event handler
    # ------------------------------------------------------------------ #

    async def _handle_rf_event(self, payload: dict):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO rf_events (detected_at, node_id, freq_hz, power_db, snr_db,
                                       node_lat, node_lon, node_alt)
                VALUES (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8)
            """,
                payload.get("ts", time.time()),
                payload.get("node_id"),
                payload.get("freq_hz"),
                payload.get("power_db"),
                payload.get("snr_db"),
                payload.get("node_position", {}).get("lat"),
                payload.get("node_position", {}).get("lon"),
                payload.get("node_position", {}).get("alt"),
            )

    # ------------------------------------------------------------------ #
    # RF detection handler (classified SDR links)
    # ------------------------------------------------------------------ #

    async def _handle_rf_detection(self, payload: dict):
        try:
            event = RFDetectionEvent(**payload)
        except ValidationError as e:
            log.warning(f"RF detection validation error: {e}")
            return

        sig = event.signal
        contact_id = event.contact_key or f"{event.node_id}:{sig.signal_type}:{sig.band}"

        pool = await get_pool()
        async with pool.acquire() as conn:
            # ── Correlate with Remote ID ──────────────────────────────────
            # A link that classifies as a drone but has no Remote ID track
            # near the detecting node is a likely non-compliant drone.
            correlated, corr_drone_id = await self._correlate_rf(conn, event)

            est_lat = event.node_position.lat
            est_lon = event.node_position.lon
            est_radius = _rssi_range_m(sig.rssi)

            # ── Upsert the live contact ──────────────────────────────────
            row = await conn.fetchrow("""
                INSERT INTO rf_contacts (
                    contact_id, first_seen, last_seen, signal_type, band,
                    center_freq_hz, bandwidth_hz, last_rssi, best_rssi,
                    confidence, hop_rate_hz, detecting_nodes, detection_count,
                    correlated, correlated_drone_id, compliant,
                    est_lat, est_lon, est_radius_m
                ) VALUES (
                    $1, to_timestamp($2), to_timestamp($2), $3, $4,
                    $5, $6, $7, $7,
                    $8, $9, ARRAY[$10], 1,
                    $11, $12, $13,
                    $14, $15, $16
                )
                ON CONFLICT (contact_id) DO UPDATE SET
                    last_seen       = to_timestamp($2),
                    center_freq_hz  = $5,
                    bandwidth_hz    = $6,
                    last_rssi       = $7,
                    best_rssi       = GREATEST(rf_contacts.best_rssi, $7),
                    confidence      = $8,
                    hop_rate_hz     = $9,
                    detecting_nodes = (
                        SELECT ARRAY(SELECT DISTINCT unnest(
                            rf_contacts.detecting_nodes || ARRAY[$10]))
                    ),
                    detection_count = rf_contacts.detection_count + 1,
                    correlated          = $11,
                    correlated_drone_id = COALESCE($12, rf_contacts.correlated_drone_id),
                    compliant           = $13,
                    est_lat         = $14,
                    est_lon         = $15,
                    est_radius_m    = $16
                RETURNING contact_id, signal_type, band, best_rssi, last_rssi,
                          detecting_nodes, detection_count, correlated
            """,
                contact_id, event.ts, sig.signal_type, sig.band,
                sig.center_freq_hz, sig.bandwidth_hz, sig.rssi,
                sig.confidence, sig.hop_rate_hz, event.node_id,
                correlated, corr_drone_id, (True if correlated else False),
                est_lat, est_lon, est_radius,
            )

            # ── Score and persist threat ─────────────────────────────────
            contact = dict(row)
            threat = score_rf_contact(contact)
            await conn.execute(
                "UPDATE rf_contacts SET threat_score=$2, threat_level=$3 WHERE contact_id=$1",
                contact_id, threat.score, threat.level,
            )

        # ── Alerts (outside the DB transaction) ──────────────────────────
        await self.alert_engine.evaluate_rf(event, contact_id, threat, correlated, corr_drone_id)

        # ── Broadcast to the packet feed ─────────────────────────────────
        await self.broadcaster.broadcast_rf_detection(
            event, contact_id, threat, correlated, est_lat, est_lon, est_radius
        )

    async def _correlate_rf(self, conn, event: RFDetectionEvent):
        """
        Return (correlated: bool, drone_id | None).

        Correlated when an active Remote ID track sits within
        rf_correlation_radius_m of the detecting node inside the time window.
        One node can't position the emitter, so proximity to the node is the
        honest test: if a compliant drone is right here, this link is probably
        it; if nothing is broadcasting RID nearby, the link is dark.
        """
        window = getattr(self.settings, "rf_correlation_window_s", 20)
        radius = getattr(self.settings, "rf_correlation_radius_m", 750.0)
        rows = await conn.fetch(
            """SELECT drone_id, lat, lon FROM drone_tracks
               WHERE last_seen > NOW() - ($1 || ' seconds')::interval
                 AND lat IS NOT NULL AND lon IS NOT NULL""",
            str(window),
        )
        nlat, nlon = event.node_position.lat, event.node_position.lon
        best = None
        best_d = radius
        for r in rows:
            d = _haversine_m(nlat, nlon, r["lat"], r["lon"])
            if d <= best_d:
                best_d = d
                best = r["drone_id"]
        return (best is not None), best

    def get_detection_rate(self) -> float:
        """Detections per minute over last 5 minutes."""
        now = time.time()
        recent = [t for t in self._detection_times if now - t < 300]
        return round(len(recent) / 5.0, 1) if recent else 0.0
