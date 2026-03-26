"""
config/settings.py
==================
Loads node configuration from /etc/argus-node/config.yaml (or env vars).
All fields have sensible defaults for a Pi Zero 2W with standard hardware.
"""

import os
import socket
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Optional

import yaml


CONFIG_PATH = Path(os.environ.get("ARGUS_CONFIG", "/etc/argus-node/config.yaml"))

_cfg_log = logging.getLogger("config")


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
    gps_serial_port: str = "/dev/ttyACM0"
    gps_baud: int = 38400          # NEO-M9N USB default (legacy UART modules use 9600)
    gps_mode: str = "usb"          # "usb" or "uart"
    gps_auto_detect: bool = False  # False = use configured port directly; True = scan all candidates

    # ---- Wi-Fi NAN ----
    # Primary adapter — 2.4 GHz
    wifi_enabled_2g: bool = True
    wifi_interface_2g: str = "wlan1"
    wifi_channels_2g: list = field(default_factory=lambda: [1, 6, 11])
    wifi_dwell_ms_2g: int = 200

    # Secondary adapter — 5 GHz (disabled by default; opt-in when second adapter present)
    wifi_enabled_5g: bool = False
    wifi_interface_5g: str = "wlan2"
    wifi_channels_5g: list = field(default_factory=lambda: [36, 40, 44, 48, 149, 153, 157, 161])
    wifi_dwell_ms_5g: int = 200

    # Deprecated — kept for backward compatibility only; use interface_2g / enabled_2g
    wifi_enabled: bool = True
    wifi_iface: str = ""
    wifi_channel: int = 6

    # ---- Bluetooth ----
    bt_enabled: bool = True
    bt_hci_index: int = 1                # nRF52840 USB dongle (hci1); Pi onboard BT is hci0
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
    _apply_wifi_backward_compat(cfg, data)
    return cfg



def _apply_wifi_backward_compat(cfg: NodeConfig, data: dict):
    """
    Map legacy wifi.iface / wifi.enabled keys to the new dual-adapter fields.
    Old configs continue to work without modification.
    """
    wifi = data.get("wifi", {})
    if not isinstance(wifi, dict):
        return

    # Detect old single-adapter config by presence of legacy keys
    has_legacy_iface   = "iface" in wifi
    has_legacy_enabled = "enabled" in wifi
    has_new_iface      = "interface_2g" in wifi

    if has_legacy_iface and not has_new_iface:
        _cfg_log.warning(
            "wifi.iface is deprecated — replace with wifi.interface_2g in your config"
        )
        cfg.wifi_interface_2g = wifi["iface"]

    if has_legacy_enabled and "enabled_2g" not in wifi:
        cfg.wifi_enabled_2g = wifi["enabled"]


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
            try:
                setattr(cfg, attr, cast(val))
            except (ValueError, TypeError) as e:
                print(f"[config] Warning: invalid value for {env_key}={val!r}: {e} — using default")
