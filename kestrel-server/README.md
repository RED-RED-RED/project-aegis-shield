# KESTREL Server

FastAPI backend for the KESTREL drone detection platform. Consumes MQTT detections from KESTREL nodes, stores them in TimescaleDB, runs the alert engine, and pushes live state to connected KESTREL dashboards via WebSocket.

## Quick start

```bash
# Copy the env template and set passwords
cp docker/.env.example docker/.env
nano docker/.env

# Start the full stack (TimescaleDB + Mosquitto + KESTREL Server + Nginx)
make server-up
```

API available at `http://localhost:8000` · Dashboard at `http://localhost`

