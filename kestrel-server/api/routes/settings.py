"""
api/routes/settings.py
=====================
Read + edit the GUI-manageable settings. Editable keys apply live (no restart);
everything else is shown read-only. Secrets are never returned — only whether
they are configured.

No authentication by design — trusted-LAN deployment only (see README).
"""

from fastapi import APIRouter, Body

from core import runtime_settings as rt

router = APIRouter()


@router.get("")
async def get_settings_view():
    return rt.snapshot()


@router.put("")
async def update_settings(updates: dict = Body(...)):
    """Apply a map of {key: value} for editable keys. Unknown or invalid keys
    are ignored. Returns the keys that were applied (secret values omitted)."""
    applied = await rt.set_many(updates)
    safe = [k for k in applied if not rt.EDITABLE.get(k, {}).get("secret")]
    secret = [k for k in applied if rt.EDITABLE.get(k, {}).get("secret")]
    return {"applied": safe, "applied_secret": secret, "count": len(applied)}
