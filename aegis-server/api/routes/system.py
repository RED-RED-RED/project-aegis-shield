"""
api/routes/system.py
====================
Health history for the System tab. Serves downsampled cpu/mem/disk/temp series
per node and for the server itself, plus a current snapshot, from node_health.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from db.database import get_pool

log = logging.getLogger("system-route")
router = APIRouter()

# range -> (window, bucket seconds). Bucket keeps each series to a sane point
# count regardless of how often samples arrive.
_RANGES = {
    "1h":  ("1 hour",   30),
    "6h":  ("6 hours",  120),
    "24h": ("24 hours", 600),
    "7d":  ("7 days",   3600),
}

_METRICS = ("cpu_pct", "mem_pct", "disk_pct", "temp_c", "satellites")


def _rows_to_series(rows) -> dict:
    """Group bucketed rows into {node_id: [{t, <metrics>}...]} in time order."""
    series: dict[str, list] = {}
    for r in rows:
        nid = r["node_id"]
        point = {"t": r["bucket"].isoformat() if r["bucket"] else None}
        for m in _METRICS:
            v = r[m]
            point[m] = round(float(v), 1) if v is not None else None
        series.setdefault(nid, []).append(point)
    return series


def _rows_to_current(rows) -> dict:
    """Latest reading per node_id -> {node_id: {...}}."""
    current = {}
    for r in rows:
        d = dict(r)
        t = d.pop("time", None)
        d["last"] = t.isoformat() if t else None
        current[d["node_id"]] = d
    return current


@router.get("/health/history")
async def health_history(range: str = Query("24h")):
    # window (str) and bucket_s (int) come from _RANGES — server-controlled
    # constants, never raw user input — so they are safe to inline.
    window, bucket_s = _RANGES.get(range, _RANGES["24h"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT node_id,
                   time_bucket(INTERVAL '{bucket_s} seconds', time) AS bucket,
                   avg(cpu_pct)    AS cpu_pct,
                   avg(mem_pct)    AS mem_pct,
                   avg(disk_pct)   AS disk_pct,
                   avg(temp_c)     AS temp_c,
                   avg(satellites) AS satellites
            FROM node_health
            WHERE time > NOW() - INTERVAL '{window}'
            GROUP BY node_id, bucket
            ORDER BY node_id, bucket
        """)
        cur_rows = await conn.fetch("""
            SELECT DISTINCT ON (node_id)
                   node_id, time, cpu_pct, mem_pct, disk_pct, temp_c,
                   uptime_s, satellites, gps_fix, db_ok, mqtt_ok
            FROM node_health
            ORDER BY node_id, time DESC
        """)

    return {
        "range":     range if range in _RANGES else "24h",
        "bucket_s":  bucket_s,
        "server_id": "__server__",
        "series":    _rows_to_series(rows),
        "current":   _rows_to_current(cur_rows),
    }
