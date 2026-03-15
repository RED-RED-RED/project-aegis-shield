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
from models.schemas import DetectionEvent, NodeHeartbeat
from mqtt.alert_engine import AlertEngine
from mqtt.ws_broadcaster import WSBroadcaster
from analysis.pipeline import AnalysisPipeline

log = logging.getLogger("mqtt")


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
        if len(parts) < 3:
            return
        msg_type = parts[-1]

        if msg_type == "detection":
            await self._handle_detection(payload)
        elif msg_type == "heartbeat":
            await self._handle_heartbeat(payload)
        elif msg_type == "status":
            await self._handle_status(payload)
        elif msg_type == "rf_event":
            await self._handle_rf_event(payload)

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
                    jamming_state, spoofing_state, survey_complete
                ) VALUES ($1, 'online', to_timestamp($2), $3, $4, $5, $6, $7,
                          $8, $9, $10, $11, $12, $13, $14, $15)
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
                    jamming_state   = $13,
                    spoofing_state  = $14,
                    survey_complete = $15
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

    def get_detection_rate(self) -> float:
        """Detections per minute over last 5 minutes."""
        now = time.time()
        recent = [t for t in self._detection_times if now - t < 300]
        return round(len(recent) / 5.0, 1) if recent else 0.0
