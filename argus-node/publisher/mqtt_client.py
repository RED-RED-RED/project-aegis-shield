"""
publisher/mqtt_client.py
========================
Publishes Remote ID detections + node heartbeats to the central MQTT broker.

Topic schema:
  argus/<node_id>/detection   — one JSON message per drone detection
  argus/<node_id>/heartbeat   — periodic node health/GPS update
  argus/<node_id>/status      — LWT (Last Will Testament) for offline detection

All messages are JSON. The AEGIS platform subscribes to argus/# and fans out
to TimescaleDB + WebSocket clients.
"""

import json
import logging
import queue
import threading
import time
from dataclasses import asdict
from typing import Optional

import paho.mqtt.client as mqtt

from config.settings import NodeConfig
from parser.opendroneid import RIDFrame

log = logging.getLogger("mqtt")


class MQTTPublisher:
    """
    Thread-safe MQTT publisher with:
      - Automatic reconnect with exponential backoff
      - Internal queue so scanners never block on network I/O
      - Last Will Testament for offline detection on the AEGIS platform
    """

    def __init__(self, cfg: NodeConfig):
        self.cfg = cfg
        self.node_id = cfg.node_id
        self._q: queue.Queue = queue.Queue(maxsize=cfg.max_queue_size)
        self._connected = threading.Event()
        self._stop = threading.Event()

        self._client = mqtt.Client(
            client_id=f"argus-node-{self.node_id}",
            clean_session=True,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        if cfg.mqtt_user:
            self._client.username_pw_set(cfg.mqtt_user, cfg.mqtt_password)

        if cfg.mqtt_tls:
            self._client.tls_set()

        # Last Will Testament: if we drop unexpectedly, broker publishes this
        lwt_topic = f"argus/{self.node_id}/status"
        lwt_payload = json.dumps({"node_id": self.node_id, "status": "offline", "ts": time.time()})
        self._client.will_set(lwt_topic, lwt_payload, qos=1, retain=True)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self):
        self._client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self._client.loop_start()

        # Start the drain thread
        self._drain_thread = threading.Thread(target=self._drain_queue, daemon=True)
        self._drain_thread.start()

        # Publish online status once connected
        log.info(f"MQTT connecting to {self.cfg.mqtt_host}:{self.cfg.mqtt_port}…")

    def disconnect(self):
        self._stop.set()
        self._publish_status("offline")
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------ #
    # Public publish methods
    # ------------------------------------------------------------------ #

    def publish_detection(
        self,
        node_id: str,
        transport: str,
        frame: RIDFrame,
        rssi: int,
        src_addr: str,
        node_lat: float,
        node_lon: float,
        node_alt: float,
    ):
        """Enqueue a drone detection event. Non-blocking."""
        payload = {
            "node_id": node_id,
            "transport": transport,
            "rssi": rssi,
            "src_addr": src_addr,
            "node_position": {
                "lat": node_lat,
                "lon": node_lon,
                "alt": node_alt,
            },
            "drone": {
                "id":           frame.drone_id,
                "id_type":      frame.id_type,
                "ua_type":      frame.ua_type,
                "status":       frame.status,
                "lat":          frame.lat,
                "lon":          frame.lon,
                "alt_baro":     frame.alt_baro,
                "alt_geo":      frame.alt_geo,
                "height_agl":   frame.height_agl,
                "speed_h":      frame.speed_h,
                "speed_v":      frame.speed_v,
                "heading":      frame.heading,
                "operator_id":  frame.operator_id,
                "operator_lat": frame.operator_lat,
                "operator_lon": frame.operator_lon,
                "description":  frame.description,
            },
            "ts": frame.timestamp,
        }
        topic = f"argus/{node_id}/detection"
        self._enqueue(topic, payload, qos=1)

    def send_heartbeat(self, gps):
        """Publish node health metrics. Called every N seconds by the main loop."""
        import psutil
        payload = {
            "node_id": self.node_id,
            "status": "online",
            "ts": time.time(),
            "gps": {
                "lat": gps.lat,
                "lon": gps.lon,
                "alt": gps.alt,
                "fix": gps.has_fix,
                "sats": gps.satellites,
            },
            "system": {
                "cpu_pct":  psutil.cpu_percent(interval=None),
                "mem_pct":  psutil.virtual_memory().percent,
                "disk_pct": psutil.disk_usage("/").percent,
                "temp_c":   _read_cpu_temp(),
                "uptime_s": int(time.time() - psutil.boot_time()),
            },
        }
        topic = f"argus/{self.node_id}/heartbeat"
        self._enqueue(topic, payload, qos=0)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _enqueue(self, topic: str, payload: dict, qos: int = 1):
        msg = (topic, json.dumps(payload), qos)
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            log.warning("MQTT queue full — dropping oldest message")
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(msg)

    def _drain_queue(self):
        """Worker thread: pull from queue and send to broker."""
        backoff = 1
        while not self._stop.is_set():
            try:
                topic, payload, qos = self._q.get(timeout=1)
            except queue.Empty:
                continue

            if not self._connected.wait(timeout=10):
                log.warning("MQTT not connected — requeueing message")
                self._q.put((topic, payload, qos))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            backoff = 1
            try:
                result = self._client.publish(topic, payload, qos=qos)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    log.error(f"Publish failed rc={result.rc} topic={topic}")
            except Exception as e:
                log.error(f"Publish exception: {e}")

    def _publish_status(self, status: str):
        topic = f"argus/{self.node_id}/status"
        payload = json.dumps({"node_id": self.node_id, "status": status, "ts": time.time()})
        try:
            self._client.publish(topic, payload, qos=1, retain=True)
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info(f"MQTT connected to {self.cfg.mqtt_host}")
            self._connected.set()
            self._publish_status("online")
        else:
            log.error(f"MQTT connect failed rc={rc}")
            self._connected.clear()

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        if rc != 0:
            log.warning(f"MQTT disconnected unexpectedly (rc={rc}) — will auto-reconnect")


def _read_cpu_temp() -> Optional[float]:
    """Read Raspberry Pi CPU temperature."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None
