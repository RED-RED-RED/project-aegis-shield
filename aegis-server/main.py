"""
main.py
=======
AEGIS AEGIS Platform — FastAPI application entry point.

Starts:
  - FastAPI app with REST + WebSocket endpoints
  - MQTT subscriber (background task consuming from all nodes)
  - TimescaleDB connection pool

Run locally:
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Production (via Docker Compose):
  See docker/docker-compose.yml
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import detections, nodes, alerts, websocket, analysis
from core.auth import require_api_key
from core.config import get_settings
from db.database import init_db, close_db
from mqtt.subscriber import MQTTSubscriber

log = logging.getLogger("server")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

settings = get_settings()


# ------------------------------------------------------------------ #
# Lifespan: startup / shutdown
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AEGIS server starting…")

    # Initialize TimescaleDB schema
    await init_db()
    log.info("Database ready.")

    # Start MQTT subscriber as a background task
    mqtt = MQTTSubscriber(settings)
    mqtt_task = asyncio.create_task(mqtt.run(), name="mqtt-subscriber")
    app.state.mqtt = mqtt
    log.info(f"MQTT subscriber connecting to {settings.mqtt_host}:{settings.mqtt_port}")

    yield  # Server is running

    log.info("Shutting down…")
    mqtt_task.cancel()
    try:
        await mqtt_task
    except asyncio.CancelledError:
        pass
    await close_db()
    log.info("Server stopped.")


# ------------------------------------------------------------------ #
# App
# ------------------------------------------------------------------ #

app = FastAPI(
    title="AEGIS AEGIS Platform",
    description="Multi-node Remote ID detection and aggregation API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST routes — protected by API key when AEGIS_API_KEY is set
_auth = [Depends(require_api_key)]
app.include_router(detections.router, prefix="/api/detections", tags=["detections"], dependencies=_auth)
app.include_router(nodes.router,      prefix="/api/nodes",      tags=["nodes"],       dependencies=_auth)
app.include_router(alerts.router,     prefix="/api/alerts",     tags=["alerts"],      dependencies=_auth)
app.include_router(analysis.router,   prefix="/api/analysis",   tags=["analysis"],    dependencies=_auth)

# WebSocket — protected by same API key
app.include_router(websocket.router, tags=["websocket"], dependencies=_auth)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
