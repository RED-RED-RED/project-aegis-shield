"""
core/runtime_settings.py
========================
A thin runtime layer over the frozen .env Settings so a handful of settings can
be edited from the GUI and take effect **without a redeploy**.

Values live in the `settings` DB table and override the matching .env default.
Only keys in EDITABLE can be changed; everything else still comes from .env.
Consumers that want live values call get(key) instead of reading the cached
Settings singleton.

NOTE: there is intentionally no authentication on the settings API — this
platform is designed for a trusted LAN only (see README). Anyone who can reach
the dashboard can change these.
"""

from __future__ import annotations

import logging

from core.config import get_settings
from db.database import get_pool

log = logging.getLogger("runtime-settings")

# key -> metadata. type drives coercion + the form widget; secret hides the value.
EDITABLE: dict[str, dict] = {
    # Notifications (Pushover) — all live-editable
    "pushover_enabled":        {"type": "bool",  "group": "Notifications", "label": "Enabled"},
    "pushover_token":          {"type": "str",   "group": "Notifications", "label": "API token", "secret": True},
    "pushover_user":           {"type": "str",   "group": "Notifications", "label": "User / group key", "secret": True},
    "pushover_min_level":      {"type": "enum",  "group": "Notifications", "label": "Min level", "choices": ["medium", "high"]},
    "pushover_cooldown_s":     {"type": "int",   "group": "Notifications", "label": "Per-drone cooldown (s)"},
    "pushover_max_per_hour":   {"type": "int",   "group": "Notifications", "label": "Max per hour"},
    "pushover_emergency_categories": {"type": "str", "group": "Notifications", "label": "Emergency categories (CSV)"},
    "pushover_health_enabled": {"type": "bool",  "group": "Notifications", "label": "Critical health pushes"},
    "pushover_temp_crit_c":    {"type": "float", "group": "Notifications", "label": "Temp critical (°C)"},
    "pushover_disk_crit_pct":  {"type": "float", "group": "Notifications", "label": "Disk critical (%)"},
    # Live map freshness — live-editable
    "live_active_s":           {"type": "int",   "group": "Live map", "label": "Solid window (s)"},
    "live_window_s":           {"type": "int",   "group": "Live map", "label": "Remove after (s)"},
}

# Shown read-only in the GUI for visibility (never secret).
READONLY_DISPLAY = [
    "db_host", "db_name", "mqtt_host", "mqtt_port",
    "alert_speed_threshold_ms", "alert_no_rid_min_alt_m",
    "rf_alert_min_confidence", "rf_correlation_radius_m",
    "flight_gap_s", "flight_min_points",
    "server_health_interval_s", "ws_broadcast_interval_ms",
    "detection_retention_days", "alert_retention_days",
]

_overrides: dict[str, object] = {}


def _coerce(key: str, raw) -> object:
    t = EDITABLE[key]["type"]
    if t == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if t == "int":
        return int(float(raw))
    if t == "float":
        return float(raw)
    return str(raw)


def get(key: str):
    """Current value: a runtime override if present, else the .env default."""
    if key in _overrides:
        return _overrides[key]
    return getattr(get_settings(), key)


def emergency_set() -> set[str]:
    return {c.strip() for c in str(get("pushover_emergency_categories")).split(",") if c.strip()}


async def load_overrides() -> None:
    """Populate the in-memory override cache from the settings table at startup."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM settings")
    except Exception as e:
        log.warning(f"could not load settings overrides: {e}")
        return
    _overrides.clear()
    for r in rows:
        k = r["key"]
        if k in EDITABLE:
            try:
                _overrides[k] = _coerce(k, r["value"])
            except Exception:
                pass
    if _overrides:
        log.info(f"Loaded {len(_overrides)} runtime setting override(s)")


async def set_many(updates: dict) -> dict:
    """Validate + persist + apply editable settings. Returns what was applied
    (secret values are applied but not echoed back by the API layer)."""
    applied = {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        for key, val in updates.items():
            meta = EDITABLE.get(key)
            if meta is None:
                continue
            if meta["type"] == "enum" and str(val) not in meta["choices"]:
                continue
            try:
                coerced = _coerce(key, val)
            except Exception:
                continue
            await conn.execute("""
                INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
            """, key, str(val))
            _overrides[key] = coerced
            applied[key] = coerced
    return applied


def snapshot() -> dict:
    """Describe all settings for the GUI. Secret values are never returned —
    only whether they are configured."""
    editable = []
    for key, meta in EDITABLE.items():
        val = get(key)
        entry = {
            "key": key, "group": meta["group"], "label": meta["label"],
            "type": meta["type"], "secret": meta.get("secret", False),
        }
        if meta.get("choices"):
            entry["choices"] = meta["choices"]
        if meta.get("secret"):
            entry["configured"] = bool(val)
        else:
            entry["value"] = val
        editable.append(entry)

    s = get_settings()
    readonly = [{"key": k, "value": getattr(s, k, None)} for k in READONLY_DISPLAY]
    return {"editable": editable, "readonly": readonly}
