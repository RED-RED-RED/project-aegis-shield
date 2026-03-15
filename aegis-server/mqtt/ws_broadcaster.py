"""
mqtt/ws_broadcaster.py
======================
Singleton WebSocket connection manager.

Manages all active AEGIS Shield connections and broadcasts:
  - live_state  : full snapshot every 500ms (drones + nodes + alerts)
  - detection   : individual detection events (for packet feed)
  - alert       : new alert events
  - node_update : node status change

Uses a module-level singleton so the MQTT subscriber and WebSocket
route handlers share the same connection registry.
"""

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("ws")


class _ConnectionManager:
    """Thread-safe set of active WebSocket connections."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        log.info(f"WS client connected. Total: {len(self._connections)}")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._connections.discard(ws)
        log.info(f"WS client disconnected. Total: {len(self._connections)}")

    async def broadcast(self, message: dict):
        """Send to all connected clients. Remove dead connections."""
        if not self._connections:
            return

        payload = json.dumps(message, default=_json_default)
        dead = set()

        async with self._lock:
            connections = set(self._connections)

        for ws in connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._connections -= dead

    @property
    def count(self) -> int:
        return len(self._connections)


# Module-level singleton
_manager = _ConnectionManager()


def get_manager() -> _ConnectionManager:
    return _manager


def _json_default(obj: Any) -> Any:
    """Handle non-serializable types."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class WSBroadcaster:
    """
    Helper used by MQTT subscriber and alert engine to push messages.
    All methods are no-ops if no clients are connected.
    """

    async def broadcast_detection(self, event) -> None:
        """Push a raw detection to the packet feed."""
        if _manager.count == 0:
            return
        await _manager.broadcast({
            "type": "detection",
            "payload": {
                "node_id":   event.node_id,
                "transport": event.transport,
                "rssi":      event.rssi,
                "drone_id":  event.drone.id,
                "lat":       event.drone.lat,
                "lon":       event.drone.lon,
                "alt_baro":  event.drone.alt_baro,
                "speed_h":   event.drone.speed_h,
                "heading":   event.drone.heading,
                "status":    event.drone.status,
                "operator_id": event.drone.operator_id,
                "ts":        event.ts,
            }
        })

    async def broadcast_alert(self, alert: dict) -> None:
        """Push a new alert to all clients."""
        await _manager.broadcast({
            "type": "alert",
            "payload": alert,
        })

    async def broadcast_node_update(self, node_id: str, status: str, data: dict) -> None:
        """Push a node status/health update."""
        await _manager.broadcast({
            "type": "node_update",
            "payload": {
                "node_id": node_id,
                "status":  status,
                "data":    data,
            }
        })

    async def broadcast_live_state(self, state: dict) -> None:
        """Push full live state snapshot."""
        await _manager.broadcast({
            "type": "live_state",
            "payload": state,
        })
