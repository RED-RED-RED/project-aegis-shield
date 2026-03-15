#!/usr/bin/env python3
"""
AEGIS ARGUS Node
====================
Main entry point. Starts all scanner threads and the MQTT publisher.
Runs on each Raspberry Pi ARGUS node.

Hardware expected:
  - Alfa AWUS036ACM  (Wi-Fi, monitor mode)   → wlan1mon
  - nRF52840 USB     (Bluetooth 4/5 LR)      → hci0
  - u-blox NEO-M8N   (GPS, serial)           → /dev/ttyAMA0
  - RTL-SDR v3       (optional SDR)          → auto-detect

Author: AEGIS project
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from config.settings import load_config, NodeConfig
from scanner.wifi_nan import WiFiNANScanner
from scanner.bluetooth import BluetoothScanner
from scanner.sdr import SDRScanner
from publisher.mqtt_client import MQTTPublisher
from publisher.gps import GPSDaemon

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LOG_DIR = "/var/log/argus-node"
_LOG_FILE = f"{_LOG_DIR}/argus-node.log"

os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE),
    ],
)
log = logging.getLogger("agent")


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #
_stop_event = threading.Event()

def _handle_signal(sig, frame):
    log.info(f"Received signal {sig}, shutting down…")
    _stop_event.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg: NodeConfig = load_config()
    log.info(f"Starting AEGIS ARGUS Node — ID={cfg.node_id}, site={cfg.site_name}")

    # ---- MQTT publisher (shared across all scanners) ----
    publisher = MQTTPublisher(cfg)
    publisher.connect()

    # ---- GPS daemon (runs in background, updates shared position) ----
    gps = GPSDaemon(cfg.gps_serial_port, cfg.gps_baud)
    gps_thread = threading.Thread(target=gps.run, args=(_stop_event,), daemon=True)
    gps_thread.start()

    # Give GPS a moment to get a fix
    log.info("Waiting for GPS fix (up to 60 s)…")
    gps.wait_for_fix(timeout=60)
    if not gps.has_fix:
        log.warning("No GPS fix yet — will use last known position or (0,0)")

    # ---- Scanner threads ----
    threads = []

    # Wi-Fi NAN scanner (requires interface in monitor mode)
    if cfg.wifi_enabled:
        wifi = WiFiNANScanner(
            iface=cfg.wifi_iface,
            publisher=publisher,
            gps=gps,
            node_id=cfg.node_id,
            stop_event=_stop_event,
        )
        t = threading.Thread(target=wifi.run, name="wifi-nan", daemon=True)
        threads.append(t)
        log.info(f"Wi-Fi NAN scanner → {cfg.wifi_iface}")

    # Bluetooth 4/5 LR scanner (uses asyncio internally via bleak)
    if cfg.bt_enabled:
        bt = BluetoothScanner(
            hci_index=cfg.bt_hci_index,
            publisher=publisher,
            gps=gps,
            node_id=cfg.node_id,
            stop_event=_stop_event,
            enable_coded_phy=cfg.bt_coded_phy,  # BT5 LR — nRF52840 only
        )
        t = threading.Thread(target=bt.run_sync, name="bluetooth", daemon=True)
        threads.append(t)
        log.info(f"Bluetooth scanner → hci{cfg.bt_hci_index} (Coded PHY={cfg.bt_coded_phy})")

    # RTL-SDR (optional — passive RF fingerprinting)
    if cfg.sdr_enabled:
        sdr = SDRScanner(
            device_index=cfg.sdr_device_index,
            publisher=publisher,
            gps=gps,
            node_id=cfg.node_id,
            stop_event=_stop_event,
        )
        t = threading.Thread(target=sdr.run, name="sdr", daemon=True)
        threads.append(t)
        log.info(f"SDR scanner → device {cfg.sdr_device_index}")

    if not threads:
        log.error("No scanners enabled — check config. Exiting.")
        sys.exit(1)

    # Start all threads
    for t in threads:
        t.start()
    log.info(f"All {len(threads)} scanner(s) running. Press Ctrl+C to stop.")

    # ---- Heartbeat loop ----
    try:
        while not _stop_event.is_set():
            publisher.send_heartbeat(gps)
            time.sleep(cfg.heartbeat_interval_s)
    finally:
        log.info("Stopping…")
        _stop_event.set()
        for t in threads:
            t.join(timeout=5)
        publisher.disconnect()
        log.info("Node agent stopped.")


if __name__ == "__main__":
    main()
