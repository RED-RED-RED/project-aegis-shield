"""
config/settings.py
==================
Loads node configuration from /etc/argus-node/config.yaml (or env vars).
All fields have sensible defaults for a Pi Zero 2W with standard hardware.
"""

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


CONFIG_PATH = Path(os.environ.get("ARGUS_CONFIG", "/etc/argus-node/config.yaml"))


@dataclass
class NodeConfig:
    # ---- Identity ----
    node_id: str = ""                    # e.g. "ARGUS-01" — auto-generated from hostname if empty
    site_name: str = "default"

    # ---- MQTT ----
    mqtt_host: str = "192.168.1.100"     # Central server IP
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_topic_prefix: str = "argus"       # Topics: argus/<node_id>/detection, argus/<node_id>/heartbeat

    # ---- GPS ----
    gps_serial_port: str = "/dev/ttyAMA0"
    gps_baud: int = 9600

    # ---- Wi-Fi NAN ----
    wifi_enabled: bool = True
    wifi_iface: str = "wlan1mon"         # Must already be in monitor mode
    wifi_channel: int = 6                # Remote ID uses ch6 by default; also scan ch1, ch11

    # ---- Bluetooth ----
    bt_enabled: bool = True
    bt_hci_index: int = 0                # hci0
    bt_coded_phy: bool = True            # BT5 Long Range — requires nRF52840

    # ---- SDR ----
    sdr_enabled: bool = False            # Optional; enable if RTL-SDR is attached
    sdr_device_index: int = 0
    sdr_sample_rate: int = 2_400_000
    sdr_center_freq: int = 2_437_000_000  # 2.4 GHz center

    # ---- Agent behaviour ----
    heartbeat_interval_s: int = 10
    dedup_window_s: float = 2.0          # Suppress duplicate detections within this window
    max_queue_size: int = 1000


def load_config() -> NodeConfig:
    cfg = NodeConfig()

    # Auto-generate node_id from hostname if not set
    cfg.node_id = socket.gethostname().upper()

    if not CONFIG_PATH.exists():
        print(f"[config] No config file at {CONFIG_PATH} — using defaults")
        _apply_env(cfg)
        return cfg

    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f) or {}

    # Flatten nested yaml into dataclass fields
    for section, values in data.items():
        if isinstance(values, dict):
            for k, v in values.items():
                key = f"{section}_{k}" if section != "agent" else k
                if hasattr(cfg, key):
                    setattr(cfg, key, v)
        elif hasattr(cfg, section):
            setattr(cfg, section, values)

    _apply_env(cfg)
    return cfg


def _apply_env(cfg: NodeConfig):
    """Environment variables override config file (useful for Docker/secrets)."""
    overrides = {
        "ARGUS_NODE_ID":       ("node_id",       str),
        "ARGUS_MQTT_HOST":     ("mqtt_host",      str),
        "ARGUS_MQTT_PORT":     ("mqtt_port",      int),
        "ARGUS_MQTT_USER":     ("mqtt_user",      str),
        "ARGUS_MQTT_PASSWORD": ("mqtt_password",  str),
    }
    for env_key, (attr, cast) in overrides.items():
        val = os.environ.get(env_key)
        if val is not None:
            setattr(cfg, attr, cast(val))
