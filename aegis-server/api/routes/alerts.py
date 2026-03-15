"""
api/routes/alerts.py
====================
Alert management: list, acknowledge, stats.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from db.database import get_pool

router = APIRouter()


@router.get("")
async def list_alerts(
    level: Optional[str] = None,
    category: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    limit: int = Query(100, le=500),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, created_at, level, category,
                   drone_id, node_id, title, description,
                   lat, lon, acknowledged, acknowledged_at
            FROM alerts
            WHERE ($1::text IS NULL OR level = $1)
              AND ($2::text IS NULL OR category = $2)
              AND ($3::boolean IS NULL OR acknowledged = $3)
            ORDER BY created_at DESC
            LIMIT $4
        """, level, category, acknowledged, limit)
    return [dict(r) for r in rows]


@router.post("/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE alerts
            SET acknowledged = TRUE, acknowledged_at = NOW()
            WHERE id = $1
            RETURNING id, acknowledged, acknowledged_at
        """, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return dict(row)


@router.post("/acknowledge-all")
async def acknowledge_all():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE alerts SET acknowledged = TRUE, acknowledged_at = NOW()
            WHERE acknowledged = FALSE
        """)
    return {"acknowledged": int(result.split()[-1])}


@router.get("/stats")
async def alert_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE NOT acknowledged) AS open,
                COUNT(*) FILTER (WHERE level = 'high' AND NOT acknowledged) AS high,
                COUNT(*) FILTER (WHERE level = 'medium' AND NOT acknowledged) AS medium,
                COUNT(*) FILTER (WHERE level = 'low' AND NOT acknowledged) AS low,
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS last_24h
            FROM alerts
        """)
    return dict(row)
