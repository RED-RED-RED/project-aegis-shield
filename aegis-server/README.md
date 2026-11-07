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

