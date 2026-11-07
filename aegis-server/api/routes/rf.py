"""
api/routes/rf.py
================
REST endpoints for RF contacts (SDR-classified links). Used to seed the
AEGIS Shield RF layer on reload; the live feed comes over WebSocket.
"""

from fastapi import APIRouter, Query
from db.database import get_pool

router = APIRouter()


@router.get("/contacts")
async def list_rf_contacts(
    active_only: bool = Query(True, description="Only contacts seen in the last 2 minutes"),
    dark_only: bool = Query(False, description="Only links with no Remote ID correlation"),
    limit: int = Query(200, le=1000),
):
    """Current RF contacts, highest threat first."""
    pool = await get_pool()
    clauses = []
    if active_only:
        clauses.append("last_seen > NOW() - INTERVAL '2 minutes'")
    if dark_only:
        clauses.append("correlated = FALSE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT * FROM rf_contacts
            {where}
            ORDER BY threat_score DESC NULLS LAST, last_seen DESC
            LIMIT $1
        """, limit)
    return [dict(r) for r in rows]


@router.get("/stats")
async def rf_stats():
    """Summary counts for the RF layer."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '2 minutes') AS active,
                COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '2 minutes'
                                 AND correlated = FALSE) AS dark,
                COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '2 minutes'
                                 AND threat_level = 'high') AS high
            FROM rf_contacts
        """)
    return dict(row) if row else {"active": 0, "dark": 0, "high": 0}
