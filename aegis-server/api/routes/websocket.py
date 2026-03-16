"""
api/routes/websocket.py
=======================
WebSocket endpoint for the AEGIS Shield.

Clients connect to ws://<server>/ws and receive:
  - Immediate full live_state snapshot on connect
  - live_state push every 500ms (configurable)
  - Real-time detection / alert / node_update events as they arrive

The live_state loop runs as a per-connection task so each client gets
independent timing without blocking the MQTT subscriber.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import get_settings
from db.database import get_pool
from mqtt.ws_broadcaster import get_manager

log = logging.getLogger("ws-route")
router = APIRouter()
settings = get_settings()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    manager = get_manager()
    await manager.connect(ws)

    # Start a per-connection task that pushes live state snapshots
    push_task = asyncio.create_task(_live_state_pusher(ws))

    try:
        while True:
            # Keep connection alive; handle any client messages (e.g. ping)
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug(f"WS connection error: {e}")
    finally:
        push_task.cancel()
        await manager.disconnect(ws)


async def _live_state_pusher(ws: WebSocket):
    """
    Periodically builds a full live state snapshot and sends it to one client.
    Runs as a background task per connection.

    A query_running guard prevents a new DB query from starting if the previous
    one hasn't finished yet — this stops connection-pool exhaustion when the DB
    is slow.
    """
    interval = settings.ws_broadcast_interval_ms / 1000.0
    query_running = False

    while True:
        if not query_running:
            try:
                query_running = True
                state = await _build_live_state()
                query_running = False
                await ws.send_json({"type": "live_state", "payload": state})
            except Exception:
                break   # Connection is gone
        await asyncio.sleep(interval)


async def _build_live_state() -> dict:
    """Query current state from DB for the live AEGIS Shield snapshot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Active drones (seen in last 5 minutes)
        drone_rows = await conn.fetch("""
            SELECT drone_id, first_seen, last_seen,
                   last_node_id, last_transport, last_rssi,
                   lat, lon, alt_baro, alt_geo, height_agl,
                   speed_h, speed_v, heading, status,
                   id_type, ua_type, operator_id,
                   operator_lat, operator_lon, description,
                   has_valid_rid, detection_count, detecting_nodes
            FROM drone_tracks
            WHERE last_seen > NOW() - INTERVAL '5 minutes'
            ORDER BY last_seen DESC
        """)

        # All nodes
        node_rows = await conn.fetch("""
            SELECT node_id, site_name, status, last_seen,
                   lat, lon, alt, gps_fix, satellites,
                   cpu_pct, mem_pct, disk_pct, temp_c, uptime_s, radios
            FROM nodes
            ORDER BY node_id
        """)

        # Recent unacknowledged alerts (last 100)
        alert_rows = await conn.fetch("""
            SELECT id, created_at, level, category,
                   drone_id, node_id, title, description,
                   lat, lon, acknowledged, acknowledged_at
            FROM alerts
            WHERE acknowledged = FALSE
            ORDER BY created_at DESC
            LIMIT 100
        """)

        # Detection rate (per minute, last 5 min)
        rate_row = await conn.fetchrow("""
            SELECT COUNT(*) AS cnt
            FROM detections
            WHERE detected_at > NOW() - INTERVAL '5 minutes'
        """)
        det_rate = round((rate_row["cnt"] or 0) / 5.0, 1)

    return {
        "drones":         [_row_to_dict(r) for r in drone_rows],
        "nodes":          [_row_to_dict(r) for r in node_rows],
        "recent_alerts":  [_row_to_dict(r) for r in alert_rows],
        "detection_rate": det_rate,
        "ts":             time.time(),
    }


def _row_to_dict(row) -> dict:
    d = dict(row)
    # Serialize datetimes to ISO strings
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d
