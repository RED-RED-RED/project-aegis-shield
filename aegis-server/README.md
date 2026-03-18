# AEGIS Server

FastAPI backend for the AEGIS drone detection platform. Consumes MQTT detections from ARGUS nodes, stores them in TimescaleDB, runs the alert engine, and pushes live state to connected AEGIS Shield dashboards via WebSocket.

## Quick start

```bash
# Copy the env template and set passwords
cp docker/.env.example docker/.env
nano docker/.env

# Start the full stack (TimescaleDB + Mosquitto + AEGIS Server + Nginx)
make server-up
```

API available at `http://localhost:8000` · Dashboard at `http://localhost`

---

## Integrations

### Algo 8128 IP Visual Alerter

AEGIS Server can trigger an [Algo 8128 IP Visual Alerter](https://www.algosolutions.com/product/8128/) strobe whenever drone threat levels change. The integration is **fully disabled and zero-overhead** by default.

#### 1 — Enable the REST API on the 8128

1. Open the 8128 web interface (default `http://192.168.1.50`).
2. Navigate to **Admin → Services → REST API** and toggle it **On**.
3. Navigate to **Admin → Security → API Key**, generate a key, and copy it.

#### 2 — Configure AEGIS Server

Add the following variables to `docker/.env`:

| Variable | Default | Description |
|---|---|---|
| `ALGO_8128_ENABLED` | `false` | Set to `true` to activate the integration |
| `ALGO_8128_URL` | `http://192.168.1.50` | LAN address of the Algo 8128 unit |
| `ALGO_8128_API_KEY` | _(empty)_ | Bearer token from the 8128 security settings |
| `ALGO_8128_COOLDOWN_SECONDS` | `30` | Minimum seconds between repeat triggers for the same drone at the same threat level |

#### 3 — Threat level → flash pattern mapping

| Threat level | Pattern | Intensity | Trigger condition |
|---|---|---|---|
| LOW | 1 | 1 | New drone first detection |
| MEDIUM | 5 | 2 | Escalation from LOW, or speed / altitude violation |
| HIGH | 9 | 3 | No Remote ID, spoofed position, or escalation from MEDIUM |

**Escalation rules**
- Escalations (LOW→MEDIUM, MEDIUM→HIGH) always fire immediately, bypassing the cooldown window.
- The same threat level will not re-trigger within `ALGO_8128_COOLDOWN_SECONDS` (prevents strobe spam on heartbeat updates).
- The strobe is cleared (stop command sent) when a drone's threat drops back to LOW or the drone stops broadcasting for more than 60 seconds.

#### 4 — Verify connectivity

Use the test endpoint to confirm the strobe is reachable without needing a live drone detection:

```bash
# Triggers pattern 3, intensity 2, 5-second duration
curl -X POST http://localhost:8000/api/integrations/algo/test \
     -H "X-Api-Key: $AEGIS_API_KEY"
```

Response:
```json
{
  "success": true,
  "latency_ms": 12.4,
  "detail": "Test flash sent"
}
```

The AEGIS Shield dashboard shows an **8128** indicator in the top bar — green when reachable, grey when disabled, red when enabled but unreachable. Click it to trigger the same test flash from the UI.

#### 5 — Status endpoint

```bash
curl http://localhost:8000/api/integrations/algo/status
```

```json
{
  "enabled": true,
  "url": "http://192.168.1.50",
  "last_trigger_ts": 1742224800.123,
  "last_trigger_level": "high"
}
```

The API key is never returned by the status endpoint.
