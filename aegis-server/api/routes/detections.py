"""
api/routes/detections.py
========================
REST endpoints for detection history and drone tracks.
"""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from db.database import get_pool

router = APIRouter()


@router.get("/tracks")
async def get_drone_tracks(
    active_only: bool = Query(True, description="Only drones seen in last 5 minutes"),
    limit: int = Query(100, le=1000),
):
    """Current live drone tracks (one row per drone)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE last_seen > NOW() - INTERVAL '5 minutes'" if active_only else ""
        rows = await conn.fetch(f"""
            SELECT * FROM drone_tracks
            {where}
            ORDER BY last_seen DESC
            LIMIT $1
        """, limit)
    return [dict(r) for r in rows]


@router.get("/tracks/{drone_id}")
async def get_drone_track(drone_id: str):
    """Single drone track details."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM drone_tracks WHERE drone_id = $1", drone_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Drone not found")
    return dict(row)


@router.get("/tracks/{drone_id}/history")
async def get_drone_history(
    drone_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = Query(500, le=5000),
):
    """Time-series position history for a single drone from the hypertable."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT detected_at, node_id, transport, band, rssi,
                   drone_lat AS lat, drone_lon AS lon,
                   alt_baro, height_agl, speed_h, speed_v, heading, status
            FROM detections
            WHERE drone_id = $1
              AND ($2::timestamptz IS NULL OR detected_at >= $2)
              AND ($3::timestamptz IS NULL OR detected_at <= $3)
            ORDER BY detected_at DESC
            LIMIT $4
        """, drone_id, start, end, limit)
    return [dict(r) for r in rows]


@router.get("")
async def list_detections(
    node_id: Optional[str] = None,
    drone_id: Optional[str] = None,
    transport: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = Query(200, le=2000),
):
    """Paginated detection log with optional filters."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, detected_at, node_id, transport, band, rssi,
                   drone_id, drone_lat, drone_lon, alt_baro, height_agl,
                   speed_h, heading, status, operator_id
            FROM detections
            WHERE ($1::text IS NULL OR node_id = $1)
              AND ($2::text IS NULL OR drone_id = $2)
              AND ($3::text IS NULL OR transport = $3)
              AND ($4::timestamptz IS NULL OR detected_at >= $4)
              AND ($5::timestamptz IS NULL OR detected_at <= $5)
            ORDER BY detected_at DESC
            LIMIT $6
        """, node_id, drone_id, transport, start, end, limit)
    return [dict(r) for r in rows]


@router.get("/stats")
async def detection_stats():
    """Aggregate stats for AEGIS Shield header cards."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE detected_at > NOW() - INTERVAL '1 hour') AS last_hour,
                COUNT(*) FILTER (WHERE detected_at > NOW() - INTERVAL '24 hours') AS last_24h,
                COUNT(DISTINCT drone_id) FILTER (WHERE detected_at > NOW() - INTERVAL '5 minutes') AS active_drones,
                COUNT(DISTINCT drone_id) FILTER (
                    WHERE detected_at > NOW() - INTERVAL '5 minutes'
                    AND operator_id IS NULL
                ) AS no_rid_drones
            FROM detections
        """)
    return dict(stats)
