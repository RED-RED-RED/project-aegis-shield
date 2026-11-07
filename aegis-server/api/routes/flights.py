"""
api/routes/flights.py
====================
Flight history: detections grouped into sorties (see analysis/flights.py), with
per-flight detail, replay data, and KML/GPX/CSV export. RF-only (no-RID)
contacts have no path and are served separately for their own list.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from core.config import get_settings
from db.database import get_pool
from analysis import flights as F

log = logging.getLogger("flights-route")
router = APIRouter()
settings = get_settings()

_RANGES = {"1h": "1 hour", "6h": "6 hours", "24h": "24 hours",
           "7d": "7 days", "30d": "30 days"}

_DET_COLS = """
    detected_at AS t, node_id, rssi, drone_id,
    drone_lat AS lat, drone_lon AS lon,
    alt_geo, alt_baro, height_agl, speed_h, heading,
    operator_id, operator_lat, operator_lon, ua_type, description,
    node_lat, node_lon
"""


async def _threat_map(conn, drone_ids: set[str]) -> dict:
    if not drone_ids:
        return {}
    rows = await conn.fetch("""
        SELECT drone_id, has_valid_rid, threat_score, threat_level
        FROM drone_tracks WHERE drone_id = ANY($1::text[])
    """, list(drone_ids))
    return {r["drone_id"]: dict(r) for r in rows}


@router.get("")
async def list_flights(
    range: str = Query("24h"),
    compliant: str = Query("all"),          # all | yes | no  (valid RID or not)
    node: str | None = Query(None),
    min_threat: float | None = Query(None),
    limit: int = Query(200, le=1000),
):
    window = _RANGES.get(range, _RANGES["24h"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT {_DET_COLS}
            FROM detections
            WHERE detected_at > NOW() - INTERVAL '{window}'
              AND drone_lat IS NOT NULL AND drone_lon IS NOT NULL
            ORDER BY drone_id, detected_at
        """)
        dicts = [dict(r) for r in rows]
        tmap = await _threat_map(conn, {d["drone_id"] for d in dicts})

    sessions = F.sessionize(dicts, settings.flight_gap_s)
    out = []
    for s in sessions:
        if len(s) < settings.flight_min_points:
            continue
        summ = F.summarize(s, tmap.get(s[0]["drone_id"]))
        if node and node not in summ["detecting_nodes"]:
            continue
        if compliant == "yes" and not summ["has_valid_rid"]:
            continue
        if compliant == "no" and summ["has_valid_rid"]:
            continue
        if min_threat is not None and (summ["threat_score"] or 0) < min_threat:
            continue
        out.append(summ)

    out.sort(key=lambda f: f["start"], reverse=True)   # newest first
    return {"range": range if range in _RANGES else "24h", "count": len(out),
            "flights": out[:limit]}


async def _load_flight(flight_id: str):
    drone_id, start = F.parse_flight_id(flight_id)
    if start is None:
        raise HTTPException(400, "bad flight id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT {_DET_COLS}
            FROM detections
            WHERE drone_id = $1
              AND drone_lat IS NOT NULL AND drone_lon IS NOT NULL
              AND detected_at > NOW() - INTERVAL '31 days'
            ORDER BY detected_at
        """, drone_id)
        dicts = [dict(r) for r in rows]
        tmap = await _threat_map(conn, {drone_id})

    for s in F.sessionize(dicts, settings.flight_gap_s):
        if abs(int(s[0]["t"].timestamp()) - start) <= settings.flight_gap_s:
            return F.detail(s, tmap.get(drone_id))
    raise HTTPException(404, "flight not found")


@router.get("/rf-contacts")
async def rf_contact_history(range: str = Query("24h"), dark_only: bool = Query(True)):
    """No-RID / RF-only contacts over the range (no flight path — point only)."""
    window = _RANGES.get(range, _RANGES["24h"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(f"""
                SELECT contact_id, first_seen, last_seen, signal_type, band,
                       center_freq_hz, best_rssi, last_rssi, confidence,
                       detecting_nodes, detection_count, correlated,
                       est_lat, est_lon, est_radius_m, threat_score, threat_level
                FROM rf_contacts
                WHERE last_seen > NOW() - INTERVAL '{window}'
                  AND ($1::bool IS FALSE OR correlated = FALSE)
                ORDER BY last_seen DESC
                LIMIT 500
            """, dark_only)
        except Exception:
            rows = []
    def ser(r):
        d = dict(r)
        for k in ("first_seen", "last_seen"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        return d
    return {"range": range if range in _RANGES else "24h",
            "count": len(rows), "contacts": [ser(r) for r in rows]}


@router.get("/{flight_id}")
async def flight_detail(flight_id: str):
    return await _load_flight(flight_id)


@router.get("/{flight_id}/export")
async def flight_export(flight_id: str, format: str = Query("gpx")):
    d = await _load_flight(flight_id)
    fmt = format.lower()
    safe = flight_id.replace("/", "_").replace("@", "_")
    if fmt == "csv":
        return Response(F.to_csv(d), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})
    if fmt == "kml":
        return Response(F.to_kml(d), media_type="application/vnd.google-earth.kml+xml",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.kml"'})
    if fmt == "gpx":
        return Response(F.to_gpx(d), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.gpx"'})
    raise HTTPException(400, "format must be gpx, kml or csv")
