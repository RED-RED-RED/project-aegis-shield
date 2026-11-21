"""
api/routes/registry.py
======================
The drone registry: list every drone ever seen with its make/model and operator
annotations (category + notes), and edit those annotations. Category drives
alert behavior (see core/drone_registry.py). No auth, by design (trusted LAN).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from db.database import get_pool
from analysis import rid_identity
from core import drone_registry

log = logging.getLogger("registry-route")
router = APIRouter()


@router.get("/categories")
async def list_categories():
    """The fixed category ladder (key, label, severity, behavior) for the UI."""
    return {"categories": drone_registry.CATEGORIES,
            "default": drone_registry.DEFAULT_CATEGORY}


@router.get("")
async def list_drones():
    """Every drone ever seen, with identity, annotation, and note count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT t.drone_id, t.first_seen, t.last_seen, t.id_type,
                   t.detection_count, t.threat_score, t.threat_level,
                   t.has_valid_rid, t.last_transport, t.detecting_nodes,
                   COALESCE(r.category, 'unknown') AS category,
                   r.label, r.updated_at AS annotated_at,
                   (SELECT COUNT(*) FROM drone_notes n WHERE n.drone_id = t.drone_id) AS note_count
            FROM drone_tracks t
            LEFT JOIN drone_registry r ON r.drone_id = t.drone_id
            ORDER BY t.last_seen DESC
        """)
    out = []
    for r in rows:
        d = dict(r)
        ident = rid_identity.enrich(d.pop("id_type", None), d["drone_id"])
        d["make"] = ident["make"]
        d["model"] = ident["model"]
        d["id_source"] = ident["id_source"]
        d["annotated_at"] = d["annotated_at"].isoformat() if d["annotated_at"] else None
        d["first_seen"] = d["first_seen"].isoformat() if d["first_seen"] else None
        d["last_seen"] = d["last_seen"].isoformat() if d["last_seen"] else None
        out.append(d)
    return {"count": len(out), "drones": out}


@router.get("/{drone_id}")
async def get_drone(drone_id: str):
    """One drone: identity, annotation, and its full notes log."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        t = await conn.fetchrow(
            "SELECT * FROM drone_tracks WHERE drone_id = $1", drone_id)
        if not t:
            raise HTTPException(404, "drone not found")
        reg = await conn.fetchrow(
            "SELECT category, label, updated_at FROM drone_registry WHERE drone_id = $1",
            drone_id)
        notes = await conn.fetch(
            "SELECT id, created_at, body FROM drone_notes WHERE drone_id = $1 "
            "ORDER BY created_at DESC", drone_id)
    d = dict(t)
    ident = rid_identity.enrich(d.get("id_type"), drone_id)
    d["make"], d["model"], d["id_source"] = ident["make"], ident["model"], ident["id_source"]
    d["category"] = reg["category"] if reg else drone_registry.DEFAULT_CATEGORY
    d["label"] = reg["label"] if reg else None
    for k in ("first_seen", "last_seen", "analysis_updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    d["notes"] = [
        {"id": n["id"], "created_at": n["created_at"].isoformat(), "body": n["body"]}
        for n in notes
    ]
    return d


@router.put("/{drone_id}")
async def set_category(drone_id: str, payload: dict = Body(...)):
    """Set a drone's category (and optional label). Updates the alert-behavior cache."""
    category = (payload or {}).get("category", drone_registry.DEFAULT_CATEGORY)
    if not drone_registry.is_valid(category):
        raise HTTPException(400, f"invalid category '{category}'")
    label = (payload or {}).get("label")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO drone_registry (drone_id, category, label, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (drone_id) DO UPDATE SET
                category = $2, label = $3, updated_at = NOW()
        """, drone_id, category, label)
    drone_registry.set_cached(drone_id, category)
    return {"ok": True, "drone_id": drone_id, "category": category,
            "behavior": drone_registry.behavior_for(drone_id)}


@router.post("/{drone_id}/notes")
async def add_note(drone_id: str, payload: dict = Body(...)):
    """Append a timestamped note to a drone's log."""
    body = (payload or {}).get("body", "").strip()
    if not body:
        raise HTTPException(400, "note body is required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO drone_notes (drone_id, body) VALUES ($1, $2) "
            "RETURNING id, created_at, body", drone_id, body[:2000])
    return {"ok": True, "note": {"id": row["id"],
            "created_at": row["created_at"].isoformat(), "body": row["body"]}}
