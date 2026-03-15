# AEGIS Platform

> **A**irspace **E**nforcement and **G**round **I**ntelligence **S**ystem  
> Multi-node Remote ID detection, threat scoring, and MLAT position estimation

```
                    ┌──────────────────────────────────────────┐
                    │            AEGIS SHIELD (UI)              │
                    │  Live map · Threat scores · MLAT circles  │
                    │  Packet feed · Node health · Alert feed   │
                    └──────────────────┬───────────────────────┘
                                       │ WebSocket /ws
                    ┌──────────────────▼───────────────────────┐
                    │            AEGIS SERVER                    │
                    │  FastAPI · TimescaleDB · MQTT broker       │
                    │  Alert engine · Trilateration · Threat AI  │
                    └────┬──────────────┬───────────────┬───────┘
                         │ MQTT         │ MQTT           │ MQTT
              ┌──────────▼──┐  ┌────────▼──┐  ┌─────────▼──┐
              │  ARGUS-01    │  │  ARGUS-02  │  │  ARGUS-03  │
              │  Wi-Fi NAN   │  │  Wi-Fi NAN │  │  Wi-Fi NAN │
              │  BT5 LR      │  │  BT5 LR    │  │  BT5 LR    │
              │  RTL-SDR     │  │  GPS       │  │  RTL-SDR   │
              │  GPS         │  │            │  │  GPS       │
              └─────────────┘  └────────────┘  └────────────┘
```

AEGIS detects FAA-mandated Remote ID broadcasts from drones across all three radio technologies (Wi-Fi NAN, Bluetooth 4 legacy, Bluetooth 5 Long Range), correlates detections from multiple sensor nodes, estimates true drone position via RSSI trilateration (MLAT), and scores each drone with an 8-factor threat model. Everything streams live to AEGIS Shield — a tactical operations dashboard with a real Leaflet map, animated threat gauges, and a live packet feed.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Hardware](#hardware)
- [Deployment](#deployment)
  - [AEGIS Server](#aegis-server-deployment)
  - [ARGUS Nodes](#argus-node-deployment)
  - [WireGuard VPN](#wireguard-vpn-optional)
- [Calibration](#calibration)
- [AEGIS Shield](#aegis-shield)
- [API Reference](#api-reference)
- [Threat Scoring](#threat-scoring)
- [MLAT / Trilateration](#mlat--trilateration)
- [Development](#development)
- [Test Suite](#test-suite)

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/your-org/aegis-platform
cd aegis-platform

# 2. Deploy AEGIS Server (Docker required, Ubuntu/Debian/Pi OS 64-bit)
sudo bash scripts/deploy-aegis-server.sh

# 3. Deploy each ARGUS Node (run on each Pi)
sudo bash scripts/deploy-argus-node.sh

# 4. Verify
python scripts/healthcheck.py --server <SERVER_IP>
```

Open `http://<server-ip>` — AEGIS Shield will appear.

---

## Architecture

### Components

| Component | Description | Runs on |
|---|---|---|
| **ARGUS Node** | Sensor node — scans Wi-Fi NAN, BT4/5, RTL-SDR; publishes to MQTT | Raspberry Pi 4 Model B (2 GB) |
| **AEGIS Server** | Central platform — ingests MQTT, stores detections, runs analysis | Pi 4 / any Linux |
| **AEGIS Shield** | Tactical dashboard — live map, threats, alerts, packet feed | Browser (Nginx) |
| **Calibration** | RSSI path-loss model fitting for MLAT accuracy | Runs anywhere |

### Data flow

```
ARGUS Node                    AEGIS Server                  AEGIS Shield
──────────                    ────────────                  ────────────
WiFi NAN scan  ─── MQTT ───►  subscriber.py                useStore.js
BT5 LR scan    ─── MQTT ───►    │── INSERT detections       │── /ws WebSocket
GPS fix        ─── MQTT ───►    │── UPSERT drone_tracks     │── /api/analysis
heartbeat      ─── MQTT ───►    │── alert_engine.evaluate   │
                                │── pipeline.process        MapView.jsx
                                │     ├── trilateration     ThreatPanel.jsx
                                │     └── threat_scoring    RightPanel.jsx
                                └── ws_broadcaster.broadcast
```

### MQTT topic schema

| Topic | Direction | Content |
|---|---|---|
| `argus/<id>/detection` | node → server | Full RID frame, RSSI, node GPS |
| `argus/<id>/heartbeat` | node → server | CPU, memory, GPS fix, jamming/spoofing state, survey status (10s) |
| `argus/<id>/status` | node → server | `online`/`offline` (LWT, retained) |
| `argus/<id>/rf_event` | node → server | RTL-SDR RF burst (optional) |

**Heartbeat extra fields (NEO-M9N only):**

| Field | Values | Description |
|---|---|---|
| `jamming_state` | `ok` / `warning` / `critical` | Hardware GPS jamming indicator from UBX-NAV-STATUS |
| `spoofing_state` | `ok` / `spoofing` / `multiple` | Hardware GPS spoofing indicator from UBX-NAV-STATUS |
| `survey_complete` | `true` / `false` | Whether the node has completed survey-in |
| `detected_port` | e.g. `/dev/ttyACM0` | Serial port the GPS daemon auto-detected |
| `gps_mode` | `usb` / `uart` | Active GPS mode |

---

## Hardware

### Per ARGUS Node (~$150)

| Part | Model | Role |
|---|---|---|
| Single-board computer | Raspberry Pi 4 Model B (2 GB) | Host |
| Wi-Fi adapter | Alfa AWUS036ACM | Wi-Fi NAN monitor mode (mt76x2u driver) |
| Bluetooth dongle | Nordic nRF52840 USB (PCA10059) | BT4 + BT5 Long Range |
| GPS module | u-blox NEO-M9N (USB, recommended) or NEO-M8N (UART, legacy) | Node geolocation |
| RTL-SDR (optional) | RTL-SDR v3 | RF fingerprinting |
| Powered USB hub | Any 4-port | Powers all USB peripherals |

### AEGIS Server (~$60–100)

Any Linux machine with 2GB+ RAM and Docker. Raspberry Pi 4 works well; an old NUC or VPS works equally well.

### Critical hardware notes

**Alfa AWUS036ACM** — the `mt76x2u` driver is the only reliable option for capturing Wi-Fi NAN action frames in monitor mode. Most other adapters filter them out at the driver level.

**nRF52840 USB dongle** — must be flashed with [Zephyr `hci_usb` firmware](https://docs.zephyrproject.org/latest/samples/bluetooth/hci_usb/README.html) to appear as a standard HCI USB device. Most cheap "BT5" dongles advertise Coded PHY support but do not implement it in firmware — the nRF52840 is the reliable exception.

**u-blox NEO-M9N (USB, recommended)** — plug into any Pi USB port. The driver creates `/dev/ttyACM0`; the deploy script adds a udev rule so it also appears at the stable symlink `/dev/ttyGPS`. No UART configuration or serial console changes are needed. The GPS daemon auto-detects the port at startup, initialises the receiver via UBX binary protocol, and runs a **survey-in** (10 min minimum / 3 m accuracy target) to pin the node's precise position. Once the survey completes, `survey_complete` is set in the nodes table and that node receives a **3× weight multiplier** in the WLS trilateration solver. The NEO-M9N also reports hardware **jamming** and **spoofing** state via `UBX-NAV-STATUS`; critical/warning states trigger a `gps_jamming` alert on the server.

**u-blox NEO-M8N (UART, legacy)** — connect to Pi UART (`/dev/ttyAMA0`). Pass `--gps-mode uart` to the deploy script; it enables the UART in `/boot/firmware/config.txt` and disables the serial console. **A reboot is required** after this setup. The NEO-M8N does not support UBX survey-in or hardware jamming/spoofing detection.

---

## Deployment

### AEGIS Server deployment

```bash
# Interactive
sudo bash scripts/deploy-aegis-server.sh

# Non-interactive (Ansible, cloud-init, CI)
AEGIS_DB_PASSWORD=yourdbpass \
AEGIS_MQTT_PASSWORD=yourmqttpass \
sudo bash scripts/deploy-aegis-server.sh --yes
```

The script: installs Docker, builds AEGIS Shield, generates credentials, configures Mosquitto auth, starts all containers via Docker Compose, and installs an `aegis-server.service` systemd unit for auto-start on boot.

**Running services after deployment:**

| Service | Port | Description |
|---|---|---|
| Nginx | 80 | AEGIS Shield UI + API reverse proxy |
| AEGIS Server (FastAPI) | 8000 | Internal, proxied by Nginx |
| Mosquitto | 1883 | MQTT broker (authenticated) |
| TimescaleDB | 5432 | Internal only |

Credentials are written to `aegis-server/docker/.env` — back this file up.

```bash
# Useful commands after install
systemctl status aegis-server
cd /opt/aegis/aegis-server/docker && docker compose logs -f aegis-server
```

### API authentication

All `/api/*` endpoints and the WebSocket are protected by an optional API key. Authentication is **disabled by default** (safe for trusted LAN deployments). To enable it, set `AEGIS_API_KEY` in `aegis-server/docker/.env`:

```env
AEGIS_API_KEY=your-secret-key-here
```

Once set, every request must include the key as a header:

```
X-Api-Key: your-secret-key-here
```

The `/health` endpoint is always unauthenticated.

If you are running AEGIS Shield from the same origin (via Nginx reverse proxy), add the key to the Shield's build environment so it is included in API calls:

```env
# aegis-shield/.env.production
VITE_API_KEY=your-secret-key-here
```

> **Recommendation:** Enable API key auth whenever AEGIS Server is reachable from outside your local network, such as over WireGuard VPN or a public interface.

### ARGUS Node deployment

Flash Pi OS Lite 64-bit to SD card, enable SSH, then:

```bash
# Copy project to node
scp -r aegis-platform/ pi@argus-01.local:~/
ssh pi@argus-01.local

# Interactive install — prompts for all values
sudo bash aegis-platform/scripts/deploy-argus-node.sh

# Non-interactive install (USB GPS, default)
sudo bash aegis-platform/scripts/deploy-argus-node.sh \
  --unattended \
  --node-id ARGUS-01 \
  --server-ip 192.168.1.100 \
  --mqtt-password yourmqttpass

# Non-interactive install (legacy UART GPS — requires reboot)
sudo bash aegis-platform/scripts/deploy-argus-node.sh \
  --unattended \
  --node-id ARGUS-01 \
  --server-ip 192.168.1.100 \
  --mqtt-password yourmqttpass \
  --gps-mode uart
sudo reboot   # only required for uart mode

# After (re)boot
sudo systemctl status argus-node
sudo journalctl -fu argus-node
```

Or run interactively — the script prompts for all values, including GPS mode, and explains each option.

**GPS mode flag:**

| Flag | Default | Description |
|---|---|---|
| `--gps-mode usb` | yes | u-blox NEO-M9N via USB (`/dev/ttyACM0` → `/dev/ttyGPS`) |
| `--gps-mode uart` | no | Legacy NEO-M8N via UART (`/dev/ttyAMA0`); enables UART in boot config, reboot required |

**Deploying multiple nodes:**

```bash
# Node 1 (USB GPS)
sudo bash scripts/deploy-argus-node.sh \
  --unattended --node-id ARGUS-01 --server-ip 192.168.1.100 --mqtt-password pass

# Node 2
sudo bash scripts/deploy-argus-node.sh \
  --unattended --node-id ARGUS-02 --server-ip 192.168.1.100 --mqtt-password pass
```

### WireGuard VPN (optional)

Required when ARGUS nodes are deployed outside your LAN:

```bash
# On AEGIS server first
sudo bash scripts/setup-wireguard.sh server

# On each ARGUS node (prompts for server public key)
sudo bash scripts/setup-wireguard.sh node <SERVER_PUBLIC_IP> <NODE_INDEX>
```

Or pass `ARGUS_USE_VPN=1` to `deploy-argus-node.sh` and it handles VPN setup automatically.

VPN addressing: AEGIS server = `10.100.0.1`, nodes = `10.100.0.2`, `.3`, `.4`…

### Health check

```bash
python scripts/healthcheck.py --server 192.168.1.100
```

Verifies TCP connectivity to all services, checks the REST API, reports node online/offline status, and flags any open HIGH alerts.

### Updating

```bash
sudo bash scripts/update.sh server   # Pull latest, rebuild UI, restart containers
sudo bash scripts/update.sh node     # Pull latest, update deps, restart service
bash scripts/update.sh version       # Show current commit + API version
```

---

## Calibration

The MLAT trilateration engine uses a log-distance path-loss model with default parameters (`RSSI_REF = −20 dBm`, `n = 2.7`). These should be calibrated for your specific site and hardware to improve position accuracy and lower the spoof-detection false-positive rate.

```bash
# Fly a cooperative drone for 5–10 min, export GPX from controller app
python calibration/calibrate.py \
  --server http://192.168.1.100:8000 \
  --drone-id YOUR_DRONE_SERIAL \
  --gpx my_flight.gpx \
  --notes "Open field, 3 ARGUS nodes" \
  --plots

# Outputs:
#   aegis-server/analysis/calibration.yaml  (per-node fitted parameters)
#   calibration/cal_report.txt              (human-readable report)
#   calibration/cal_plots/                  (diagnostic plots, if matplotlib installed)

# Restart server to load calibration
cd aegis-server/docker && docker compose restart aegis-server
```

Supports GPX (DJI Fly, Litchi, Mission Planner), DJI SRT subtitle files, and CSV tracks. Each ARGUS node gets independent fitted parameters. See [`calibration/`](calibration/) for full documentation.

---

## AEGIS Shield

AEGIS Shield connects to the AEGIS Server via WebSocket (`/ws`) and receives a full state snapshot every 500ms. Threat data is polled from the REST API every 3 seconds.

### Views

| View | Description |
|---|---|
| **Live Map** | Leaflet map with threat-coloured drone markers, MLAT circles, confidence radii, mismatch lines, node pulse rings, and track trails |
| **Threats** | Full-width panel with animated 0–100 score gauges, 8-factor breakdown bars, MLAT position detail, and spoof confidence meter |
| **Packet Feed** | Live scrolling MQTT detection stream with timestamp, node, radio, and telemetry columns |
| **Architecture** | System architecture reference diagram |

### Map overlays

- **Drone marker** — colour-coded by threat score: green (low) / amber (medium) / red (high, pulsing). Numeric score label above each dot.
- **MLAT marker** — dashed circle at RSSI-estimated position, colour encodes spoof confidence
- **Confidence circle** — `L.circle` radius = MLAT uncertainty in metres
- **Mismatch line** — dashed line from broadcast position to MLAT estimate (drawn when > 100m divergence, red above 500m)
- **Node hexagon** — amber hex with expanding CSS pulse rings when online
- **Track trail** — 60-point rolling history polyline, inherits threat colour

### Building from source

```bash
cd aegis-shield
npm install
npm run dev     # Dev server :5173, proxies /api and /ws to :8000
npm run build   # Production → dist/ (served by Nginx container)
```

---

## API Reference

Base URL: `http://<server>/api/` (via Nginx) or `http://<server>:8000/api/` (direct)

### Detections & tracking

```
GET  /api/detections                    Detection log (filter: node_id, drone_id, transport, start, end)
GET  /api/detections/tracks             Active drone tracks — one row per drone
GET  /api/detections/tracks/{id}        Single drone track
GET  /api/detections/tracks/{id}/history Time-series position history
GET  /api/detections/stats              Aggregate counts
```

### Nodes

```
GET  /api/nodes                         All ARGUS nodes with health
GET  /api/nodes/{id}                    Node detail
GET  /api/nodes/{id}/detections         Recent detections from one node
```

### Alerts

```
GET  /api/alerts                        Alert list (filter: level, category, acknowledged)
POST /api/alerts/{id}/acknowledge       Acknowledge single alert
POST /api/alerts/acknowledge-all        Acknowledge all open alerts
GET  /api/alerts/stats                  Counts by level
```

### Analysis

```
GET  /api/analysis/threats              Scored drones, sorted by threat_score DESC
GET  /api/analysis/threats/{id}         Full threat detail for one drone
GET  /api/analysis/mlat                 Drones with MLAT results (filter: min_mismatch_m)
GET  /api/analysis/stats                High/medium counts, spoofed count, average score
```

### Alert categories

| Category | Level | Trigger |
|---|---|---|
| `no_rid` | HIGH | Drone airborne above threshold altitude with no operator ID |
| `speed` | MEDIUM | Horizontal speed exceeds configured threshold (default 30 m/s) |
| `altitude` | MEDIUM | Above 400ft AGL without operator ID |
| `spoofed_position` | HIGH | Drone reporting position at (0, 0) while airborne |
| `new_drone` | LOW | First detection of a drone ID in the database |
| `threat_score` | HIGH | Threat score crosses into ≥ 70 band |
| `position_mismatch` | HIGH | MLAT estimate diverges > 250m from broadcast with spoof confidence > 60% |
| `gps_jamming` | MEDIUM | ARGUS node reports GPS jamming (`warning` or `critical`) from UBX-NAV-STATUS |

### WebSocket

`WS /ws` — Server pushes:

| Message type | Trigger | Content |
|---|---|---|
| `live_state` | Every 500ms | Full snapshot: drones, nodes, alerts, detection rate |
| `detection` | Per detection | Single detection for packet feed |
| `alert` | Per alert | New alert object |
| `node_update` | Per heartbeat | Node status + health metrics |

---

## Threat Scoring

Each active drone receives a score from 0–100:

| Factor | Weight | Triggers |
|---|---|---|
| No operator ID | **30** | Missing or suspiciously short operator ID |
| Position mismatch | **25** | MLAT estimate diverges from broadcast (sigmoid at 250m) |
| High altitude / no RID | **15** | Above 400ft AGL without operator ID |
| Unknown UA type | **8** | Type field absent, "none", or "ground_obstacle" while airborne |
| Speed anomaly | **8** | > 25 m/s (ramps) / > 50 m/s (max, physically implausible) |
| Single node only | **7** | Only one ARGUS node detecting the drone |
| Stale GPS | **4** | `last_seen` timestamp drifting behind wall clock |
| No self-ID | **3** | Missing description string |

**≥ 70 → HIGH** · **40–69 → MEDIUM** · **< 40 → LOW**

---

## MLAT / Trilateration

AEGIS uses weighted least-squares trilateration on RSSI measurements from ≥ 3 ARGUS nodes to independently estimate each drone's position. Compared to the drone's broadcast position to detect GPS spoofing.

**Model:** `RSSI = RSSI_REF − 10 × n × log₁₀(d)`

**Accuracy:**

| Conditions | Typical RMSE |
|---|---|
| Ideal (noiseless, open field, 4 nodes) | 50–80m |
| Real-world (5 dB noise, suburban) | 150–250m |
| Post-calibration improvement | ~40% reduction |

**Survey-in weight multiplier:** Nodes that have completed a GPS survey-in (`survey_complete = true`) receive a **3× weight** in the WLS solver. This reflects that their position is precisely known (sub-3m accuracy) rather than estimated from a live GPS fix. Deploy nodes with NEO-M9N receivers and allow the 10-minute survey-in to complete for best MLAT accuracy.

The calibration utility fits `n` and `RSSI_REF` independently per node from a calibration flight, then writes `aegis-server/analysis/calibration.yaml` which the trilateration engine loads at startup.

---

## Development

```bash
# Run all 133 tests
make test

# Individual suites
make test-node     # 53 tests — ARGUS Node RID parser + GPS subsystem
make test-server   # 49 tests — alert engine + trilateration + scoring

# Start server
make server-up && make server-logs

# UI dev (hot reload)
make ui-dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions and workflow.

---

## Test Suite

| Module | Tests |
|---|---|
| ARGUS Node — OpenDroneID parser | 11 |
| ARGUS Node — GPS subsystem (auto-detect, UBX, survey-in, jamming) | 42 |
| AEGIS Server — alert engine (rules + GPS jamming alerts) | 19 |
| AEGIS Server — trilateration + threat scoring | 30 |
| Calibration — engine, collector, config writer | 31 |
| **Total** | **133** |

---

## License

MIT — see [LICENSE](LICENSE)

## Acknowledgements

- [OpenDroneID](https://github.com/opendroneid/opendroneid-core-c) — ASTM F3411 reference implementation
- [FAA Remote ID](https://www.faa.gov/uas/getting_started/remote_id) — regulatory framework
- [Leaflet.js](https://leafletjs.com/) — mapping
- [TimescaleDB](https://www.timescale.com/) — time-series storage
- [FastAPI](https://fastapi.tiangolo.com/) — API framework
