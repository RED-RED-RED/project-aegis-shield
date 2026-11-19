"""
core/drone_registry.py
======================
The drone registry: per-drone operator annotations (a category and free notes)
that persist independently of detection retention.

Categories are a fixed ladder. Each has a stable `key` (stored in the DB and
used in code) and a `label` (shown in the UI) — so a label can be renamed
without a data migration. `behavior` decides what an alert for that drone does:

    suppress   don't send a push (your own / approved drones — cut the noise)
    normal     push per the usual level/cooldown rules
    escalate   force a loud emergency push regardless of level (Watchlist, Hostile)

An in-memory cache maps drone_id -> category key so the notifier can check
behavior cheaply on the alert path. It's loaded at startup and updated whenever
a category is set. Only non-default categories are cached; everything else is
`unknown` (normal).
"""

from __future__ import annotations

import logging

log = logging.getLogger("drone-registry")

DEFAULT_CATEGORY = "unknown"

# Ordered low -> high severity. Order is the UI's display order too.
CATEGORIES: list[dict] = [
    {"key": "authorized",     "label": "Authorized",     "severity": 0, "behavior": "suppress"},
    {"key": "unknown",        "label": "Unknown",        "severity": 1, "behavior": "normal"},
    {"key": "known_benign",   "label": "Known-benign",   "severity": 2, "behavior": "normal"},
    {"key": "nuisance",       "label": "Nuisance",       "severity": 3, "behavior": "normal"},
    {"key": "prior_incident", "label": "Prior incident", "severity": 4, "behavior": "normal"},
    {"key": "watchlist",      "label": "Watchlist",      "severity": 5, "behavior": "escalate"},
    {"key": "hostile",        "label": "Hostile",        "severity": 6, "behavior": "escalate"},
]

_BY_KEY = {c["key"]: c for c in CATEGORIES}
VALID_KEYS = set(_BY_KEY)

# drone_id -> category key, for non-default categories only.
_cache: dict[str, str] = {}


def is_valid(key: str) -> bool:
    return key in VALID_KEYS


def category_for(drone_id: str | None) -> str:
    if not drone_id:
        return DEFAULT_CATEGORY
    return _cache.get(drone_id, DEFAULT_CATEGORY)


def behavior_for(drone_id: str | None) -> str:
    """'suppress' | 'normal' | 'escalate' for this drone's current category."""
    cat = category_for(drone_id)
    return _BY_KEY.get(cat, _BY_KEY[DEFAULT_CATEGORY])["behavior"]


def set_cached(drone_id: str, category: str) -> None:
    if not drone_id:
        return
    if category == DEFAULT_CATEGORY or category not in VALID_KEYS:
        _cache.pop(drone_id, None)
    else:
        _cache[drone_id] = category


async def load_cache() -> None:
    """Populate the in-memory cache from the DB (non-default categories)."""
    from db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT drone_id, category FROM drone_registry WHERE category <> $1",
            DEFAULT_CATEGORY,
        )
    _cache.clear()
    for r in rows:
        if r["category"] in VALID_KEYS:
            _cache[r["drone_id"]] = r["category"]
    log.info("Drone registry: %d annotated drones loaded", len(_cache))
