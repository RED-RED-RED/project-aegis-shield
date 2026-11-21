"""
api/routes/nodes.py — Node management endpoints
api/routes/alerts.py — Alert management endpoints
(Combined file for brevity)
"""

# ---- nodes.py ----

from fastapi import APIRouter, HTTPException
from db.database import get_pool

router = APIRouter()


@router.get("")
async def list_nodes():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT node_id, site_name, status, last_seen,
                   lat, lon, alt, gps_fix, satellites,
                   cpu_pct, mem_pct, disk_pct, temp_c, uptime_s
            FROM nodes ORDER BY node_id
        """)
    return [dict(r) for r in rows]


@router.get("/{node_id}")
async def get_node(node_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM nodes WHERE node_id = $1", node_id)
    if not row:
        raise HTTPException(status_code=404, detail="Node not found")
    return dict(row)


@router.get("/{node_id}/detections")
async def node_detections(node_id: str, limit: int = 100):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT detected_at, drone_id, transport, rssi,
                   drone_lat, drone_lon, alt_baro, speed_h, status
            FROM detections
            WHERE node_id = $1
            ORDER BY detected_at DESC
            LIMIT $2
        """, node_id, limit)
    return [dict(r) for r in rows]
