# AEGIS ARGUS Node

Raspberry Pi ARGUS node for the AEGIS multi-node Remote ID detection system.
Scans Wi-Fi NAN, Bluetooth 4/5 LR, and optionally RTL-SDR, then publishes
detections to the AEGIS platform via MQTT.

## Directory Structure

```
argus-node/
├── agent.py                   ← Main entry point
├── config/
│   ├── settings.py            ← Config loader (YAML + env vars)
│   └── config.example.yaml    ← Copy to /etc/argus-node/config.yaml
├── scanner/
│   ├── wifi_nan.py            ← 802.11 NAN monitor-mode scanner (Scapy)
│   ├── bluetooth.py           ← BT4 + BT5 LR scanner (bleak + HCI)
│   └── sdr.py                 ← RTL-SDR RF energy scanner (optional)
├── parser/
│   └── opendroneid.py         ← ASTM F3411 frame decoder
├── publisher/
│   ├── mqtt_client.py         ← MQTT publisher (paho, auto-reconnect)
│   └── gps.py                 ← NMEA GPS daemon (pyserial + pynmea2)
├── systemd/
│   ├── argus-node.service       ← systemd unit file
│   └── setup_interfaces.sh    ← Wi-Fi monitor mode + BT setup
├── tests/
│   └── test_parser.py         ← pytest unit tests
├── requirements.txt
└── install.sh                 ← One-shot installer for fresh Pi OS
```

## Hardware Required (per node)

| Part | Model | ~Cost |
|------|-------|-------|
| Single-board computer | Raspberry Pi 4 Model B (2 GB) | $45 |
| Wi-Fi adapter (monitor mode) | Alfa AWUS036ACM | $35 |
| Bluetooth 5 LR dongle | Nordic nRF52840 USB Dongle | $15 |
| GPS module | u-blox NEO-M8N (UART) | $18 |
| RTL-SDR (optional) | RTL-SDR v3 | $25 |
| USB hub (powered) | Any 4-port | $12 |

## Quick Start

```bash
# 1. Flash Raspberry Pi OS Lite 64-bit to SD card
# 2. Enable SSH, configure Wi-Fi for management interface
# 3. SSH in and run:

git clone https://github.com/your-org/aegis /tmp/argus-node
cd /tmp/argus-node/argus-node
sudo bash install.sh

# 4. Edit config
sudo nano /etc/argus-node/config.yaml
#   Set: node_id, mqtt.host, mqtt.password

# 5. Reboot (enables UART for GPS)
sudo reboot

# 6. Start agent
sudo systemctl start argus-node
sudo journalctl -fu argus-node
```

## MQTT Topics Published

| Topic | QoS | Content |
|-------|-----|---------|
| `argus/<node_id>/detection` | 1 | Full RID detection event (JSON) |
| `argus/<node_id>/heartbeat` | 0 | Node health + GPS (every 10s) |
| `argus/<node_id>/status` | 1 | online/offline (retained, LWT) |
| `argus/<node_id>/rf_event` | 0 | SDR RF burst anomaly (if SDR enabled) |

## Detection JSON Schema

```json
{
  "node_id": "ARGUS-01",
  "transport": "wifi_nan",
  "rssi": -68,
  "src_addr": "fa:3b:92:0e:11:22",
  "node_position": { "lat": 42.3601, "lon": -71.0589, "alt": 12.0 },
  "drone": {
    "id": "FA3B920ETEST0001",
    "id_type": "serial_number",
    "ua_type": "helicopter_or_mr",
    "status": "airborne",
    "lat": 42.3710,
    "lon": -71.0420,
    "alt_baro": 42.0,
    "alt_geo": 43.5,
    "height_agl": 38.0,
    "speed_h": 8.2,
    "speed_v": 0.5,
    "heading": 127.0,
    "operator_id": "OP-US-29847",
    "operator_lat": 42.3601,
    "operator_lon": -71.0589,
    "description": ""
  },
  "ts": 1742000000.123
}
```

## Wi-Fi NAN — Important Notes

The Alfa AWUS036ACM with the `mt76x2u` kernel driver is the most reliable
option for capturing NAN action frames in monitor mode. Other cards (rtl88xx,
ath9k_htc) often don't pass vendor-specific action frames through.

Check your driver:
```bash
iw dev wlan1 info | grep driver
# or
dmesg | grep -i "mt76\|rtl8\|ath9"
```

## Dual Adapter Setup (2.4 GHz + 5 GHz)

Adding a second adapter enables simultaneous 5 GHz NAN scanning, which
significantly improves detection of DJI drones that broadcast on both bands.

**Hardware requirements:**
- Both adapters must be the same model — **Alfa AWUS036ACM** (mt76x2u driver)
  is confirmed to pass NAN frames on 5 GHz in monitor mode
- The Pi has enough USB bandwidth for two adapters; use a powered USB hub if
  you experience disconnects

**Configuration:**

1. Edit `/etc/argus-node/config.yaml` and set:

   ```yaml
   wifi:
     interface_2g: "wlan1"
     enabled_2g: true

     interface_5g: "wlan2"
     enabled_5g: true      # was false by default
   ```

2. Verify the second adapter's interface name:

   ```bash
   ip link show | grep wlan
   ```

3. Put both adapters into monitor mode:

   ```bash
   sudo bash /opt/argus-node/systemd/setup_interfaces.sh
   ```

4. Confirm both interfaces are in monitor mode:

   ```bash
   iwconfig wlan1
   iwconfig wlan2
   # Both should show "Mode:Monitor"
   ```

5. Restart the agent:

   ```bash
   sudo systemctl restart argus-node
   sudo journalctl -fu argus-node
   # Look for: "WiFi NAN scanning: 2.4 GHz (wlan1), 5 GHz (wlan2)"
   ```

**Single-adapter deployments** continue to work without any config changes —
`enabled_5g` defaults to `false` and the 5 GHz scanner is never started.

## Bluetooth 5 Long Range — Important Notes

Standard BlueZ doesn't expose Coded PHY scanning via normal APIs. The agent
sends raw HCI commands directly (`_enable_coded_phy()` in `scanner/bluetooth.py`).
This requires `CAP_NET_RAW` (granted by the systemd service unit).

The **Nordic nRF52840 USB Dongle** (PCA10059) flashed with Zephyr `hci_usb`
sample firmware is the recommended hardware. Most cheap BT5 dongles claim
Coded PHY support but don't implement it in firmware.

## Running Tests

```bash
cd /opt/argus-node
venv/bin/python -m pytest tests/ -v
```
