"""
api/routes/system.py
====================
Health history for the System tab. Serves downsampled cpu/mem/disk/temp series
per node and for the server itself, plus a current snapshot, from node_health.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Query

from db.database import get_pool
from analysis import rid_identity
from core.runtime_settings import get as rt_get

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


# ──────────────────────────────────────────────────────────────────────────
# Remote ID make/model reference table (CTA-2063-A identity)
# ──────────────────────────────────────────────────────────────────────────

@router.get("/rid-identity")
async def rid_identity_status():
    """Current state of the make/model reference table for the System panel."""
    status = await rid_identity.get_status()
    status["source_url"] = rt_get("rid_doc_source_url") or ""
    return status


@router.post("/rid-identity/refresh")
async def rid_identity_refresh(payload: dict = Body(default=None)):
    """
    Manually refresh the make/model reference from the FAA Declaration of
    Compliance list. Uses the URL in the request body if given, else the
    `rid_doc_source_url` setting. No auth by design (trusted LAN).
    """
    url = (payload or {}).get("url") or rt_get("rid_doc_source_url")
    if not url:
        return {
            "ok": False,
            "error": "No source URL configured. Set 'rid_doc_source_url' in "
                     "Settings or pass a url, then try again.",
        }
    try:
        count = await rid_identity.refresh_from_url(url)
    except Exception as e:
        log.warning("RID identity refresh failed: %s", e)
        return {"ok": False, "error": f"Refresh failed: {e}"}
    status = await rid_identity.get_status()
    status["ok"] = True
    return status


@router.post("/rid-identity/import")
async def rid_identity_import(payload: dict = Body(...)):
    """Import make/model entries from pasted CSV text (manual / air-gapped path)."""
    text = (payload or {}).get("csv")
    if not text or not text.strip():
        return {"ok": False, "error": "No CSV content provided."}
    try:
        count = await rid_identity.import_csv_text(text, source="import")
    except Exception as e:
        log.warning("RID identity import failed: %s", e)
        return {"ok": False, "error": f"Import failed: {e}"}
    status = await rid_identity.get_status()
    status["ok"] = True
    status["imported"] = count
    return status
