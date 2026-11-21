"""
api/routes/analysis.py
======================
REST endpoints for threat scores, MLAT results, and position mismatch data.
"""

import json
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from db.database import get_pool

router = APIRouter()


@router.get("/threats")
async def list_threats(
    level:  Optional[str] = Query(None, description="high | medium | low"),
    limit:  int = Query(100, le=500),
):
    """All active drones with their threat scores, sorted by score descending."""
    pool = await get_pool()
    level_filter = level if level in ('high', 'medium', 'low') else None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                drone_id, threat_score, threat_level, threat_factors,
                mlat_lat, mlat_lon, mlat_radius_m, mlat_mismatch_m,
                spoof_confidence, mlat_node_count,
                lat AS broadcast_lat, lon AS broadcast_lon,
                operator_id, ua_type, height_agl, speed_h,
                last_seen, detecting_nodes, has_valid_rid,
                analysis_updated_at
            FROM drone_tracks
            WHERE last_seen > NOW() - INTERVAL '5 minutes'
              AND threat_score IS NOT NULL
              AND ($2::text IS NULL OR threat_level = $2)
            ORDER BY threat_score DESC
            LIMIT $1
        """, limit, level_filter)

    result = []
    for row in rows:
        d = dict(row)
        # Parse stored threat_factors JSON string back to dict
        if d.get("threat_factors"):
            try:
                d["threat_factors"] = json.loads(d["threat_factors"])
            except Exception:
                pass
        result.append(d)

    return result


@router.get("/threats/{drone_id}")
async def get_drone_threat(drone_id: str):
    """Detailed threat assessment for a single drone."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                drone_id, threat_score, threat_level, threat_factors,
                mlat_lat, mlat_lon, mlat_radius_m, mlat_mismatch_m,
                spoof_confidence, mlat_node_count,
                lat AS broadcast_lat, lon AS broadcast_lon,
                alt_baro, height_agl, speed_h, heading,
                operator_id, id_type, ua_type,
                operator_lat, operator_lon, description,
                first_seen, last_seen, detection_count,
                detecting_nodes, has_valid_rid,
                analysis_updated_at
            FROM drone_tracks
            WHERE drone_id = $1
        """, drone_id)

    if not row:
        raise HTTPException(status_code=404, detail="Drone not found")

    d = dict(row)
    if d.get("threat_factors"):
        try:
            d["threat_factors"] = json.loads(
                d["threat_factors"]
                .replace("'", '"')
                .replace("True", "true")
                .replace("False", "false")
            )
        except Exception:
            pass

    return d


@router.get("/mlat")
async def list_mlat_results(
    min_mismatch_m: float = Query(0, description="Only return drones where MLAT mismatch > this"),
    limit: int = Query(50, le=200),
):
    """Active drones where MLAT position estimation has run."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                drone_id,
                mlat_lat, mlat_lon, mlat_radius_m,
                mlat_mismatch_m, spoof_confidence, mlat_node_count,
                lat AS broadcast_lat, lon AS broadcast_lon,
                threat_score, threat_level,
                last_seen
            FROM drone_tracks
            WHERE last_seen > NOW() - INTERVAL '5 minutes'
              AND mlat_lat IS NOT NULL
              AND mlat_mismatch_m >= $1
            ORDER BY mlat_mismatch_m DESC
            LIMIT $2
        """, min_mismatch_m, limit)

    return [dict(r) for r in rows]


@router.get("/stats")
async def analysis_stats():
    """Summary statistics for the threat/MLAT analysis layer."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '5 minutes')
                    AS active_drones,
                COUNT(*) FILTER (
                    WHERE last_seen > NOW() - INTERVAL '5 minutes'
                    AND threat_level = 'high')
                    AS high_threat,
                COUNT(*) FILTER (
                    WHERE last_seen > NOW() - INTERVAL '5 minutes'
                    AND threat_level = 'medium')
                    AS medium_threat,
                COUNT(*) FILTER (
                    WHERE last_seen > NOW() - INTERVAL '5 minutes'
                    AND mlat_mismatch_m > 250)
                    AS position_mismatch,
                COUNT(*) FILTER (
                    WHERE last_seen > NOW() - INTERVAL '5 minutes'
                    AND spoof_confidence > 0.7)
                    AS likely_spoofed,
                AVG(threat_score) FILTER (
                    WHERE last_seen > NOW() - INTERVAL '5 minutes')
                    AS avg_threat_score
            FROM drone_tracks
        """)
    return dict(row)
