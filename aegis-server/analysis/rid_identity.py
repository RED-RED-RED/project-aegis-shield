"""
analysis/rid_identity.py
========================
Resolve a broadcast Remote ID serial to a drone make (and model when known).

A compliant Remote ID serial follows ANSI/CTA-2063-A:

    [4-char manufacturer code][1-char length code][manufacturer serial]

  - Manufacturer code: 4 alphanumeric chars, excluding I and O. Assigned per
    manufacturer (e.g. DJI = "1581").
  - Length code: one hex char 1..F giving the length of the trailing section.
  - Manufacturer serial: that many alphanumeric chars, excluding I and O.

So the make is derivable from the first 4 chars offline, with zero network.
Model needs a reference table (the FAA Declaration of Compliance list), matched
by a serial prefix that pins a specific product.

Enrichment runs server-side at ingest (see mqtt/subscriber.py) and stamps the
drone track with make / model / mfr_code / id_source. The graded `id_source`
tells the UI how confident the result is:

    doc          make + model, matched against the reference table
    prefix       make only, from the manufacturer code (model unknown)
    unknown_mfr  valid serial, manufacturer code not in the table
    unverified   id_type says serial but the string isn't valid CTA-2063-A
                 (a non-compliance / spoof signal)
    na           id_type isn't a serial (CAA registration, etc.) — nothing to decode

The reference table lives in the `rid_identity` DB table (persists in the
TimescaleDB volume, so PBS backups cover it) with an in-memory cache rebuilt on
load and after a refresh. A small verified seed ships in data/rid_identity_seed.json
and is loaded on first boot when the table is empty.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

from db.database import get_pool

log = logging.getLogger("rid-identity")

# Allowed serial characters: digits + uppercase letters, excluding I and O.
_ALLOWED = set("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ")
_HEX_LEN = set("123456789ABCDEF")   # length code is 1..F (never 0)

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "rid_identity_seed.json"


# ────────────────────────────────────────────────────────────────────────────
# CTA-2063-A structural parse
# ────────────────────────────────────────────────────────────────────────────

def parse_cta2063a(serial: str) -> dict:
    """
    Validate a CTA-2063-A serial and pull out its parts. Returns a dict with
    `valid` (bool), `mfr_code` (first 4 chars when present, for triage even on
    invalid input), and on success `length` and `body`.
    """
    if not serial:
        return {"valid": False, "mfr_code": None}
    s = serial.strip().upper()
    mfr_code = s[:4] if len(s) >= 4 else None
    # Total length is 5 (code + length char) + N body chars, N in 1..15.
    if len(s) < 6 or len(s) > 20:
        return {"valid": False, "mfr_code": mfr_code}
    mfr, length_char, body = s[:4], s[4], s[5:]
    if not set(mfr) <= _ALLOWED:
        return {"valid": False, "mfr_code": mfr_code}
    if length_char not in _HEX_LEN:
        return {"valid": False, "mfr_code": mfr_code}
    if len(body) != int(length_char, 16):
        return {"valid": False, "mfr_code": mfr_code}
    if not set(body) <= _ALLOWED:
        return {"valid": False, "mfr_code": mfr_code}
    return {"valid": True, "mfr_code": mfr, "length": int(length_char, 16), "body": body}


# ────────────────────────────────────────────────────────────────────────────
# In-memory index
# ────────────────────────────────────────────────────────────────────────────

class IdentityIndex:
    """Make/model lookup keyed by manufacturer code, with optional per-model
    serial-prefix matching. Small (hundreds of rows), held in memory."""

    def __init__(self):
        self.mfr: dict[str, str] = {}          # mfr_code -> manufacturer
        self.models: dict[str, list] = {}      # mfr_code -> [(serial_prefix, model), ...]

    def clear(self):
        self.mfr.clear()
        self.models.clear()

    def add(self, mfr_code, manufacturer=None, model=None, serial_prefix=None):
        mfr_code = (mfr_code or "").strip().upper()
        if not mfr_code:
            return
        if manufacturer:
            self.mfr[mfr_code] = manufacturer.strip()
        if model:
            prefix = (serial_prefix or mfr_code).strip().upper()
            bucket = self.models.setdefault(mfr_code, [])
            bucket.append((prefix, model.strip()))
            # Longest prefix first so the most specific model wins.
            bucket.sort(key=lambda t: len(t[0]), reverse=True)

    def lookup(self, serial: str, mfr_code: str):
        make = self.mfr.get(mfr_code)
        model = None
        s = (serial or "").upper()
        for prefix, m in self.models.get(mfr_code, []):
            if s.startswith(prefix):
                model = m
                break
        return make, model

    @property
    def manufacturer_count(self) -> int:
        return len(self.mfr)

    @property
    def model_count(self) -> int:
        return sum(len(v) for v in self.models.values())


_index = IdentityIndex()


# ────────────────────────────────────────────────────────────────────────────
# Enrichment (called per detection at ingest)
# ────────────────────────────────────────────────────────────────────────────

def enrich(id_type: str, serial: str) -> dict:
    """Map a broadcast (id_type, serial) to {make, model, mfr_code, id_source,
    serial_valid}. Pure and cheap; reads only the in-memory index."""
    out = {"make": None, "model": None, "mfr_code": None,
           "id_source": "na", "serial_valid": None}

    # Only serial-type IDs follow CTA-2063-A. CAA registration IDs and unknown
    # types don't encode a manufacturer, so there's nothing to decode.
    if (id_type or "").strip().lower() != "serial_number":
        return out

    parsed = parse_cta2063a(serial)
    out["mfr_code"] = parsed.get("mfr_code")
    if not parsed["valid"]:
        out["id_source"] = "unverified"
        out["serial_valid"] = False
        return out

    out["serial_valid"] = True
    make, model = _index.lookup(serial, parsed["mfr_code"])
    if make and model:
        out.update(make=make, model=model, id_source="doc")
    elif make:
        out.update(make=make, id_source="prefix")
    else:
        out["id_source"] = "unknown_mfr"
    return out


# ────────────────────────────────────────────────────────────────────────────
# Reference-table load / seed / replace
# ────────────────────────────────────────────────────────────────────────────

async def load_index() -> None:
    """Rebuild the in-memory index from the DB reference table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT mfr_code, manufacturer, model, serial_prefix FROM rid_identity"
        )
    _index.clear()
    for r in rows:
        _index.add(r["mfr_code"], r["manufacturer"], r["model"], r["serial_prefix"])
    log.info(
        "RID identity index: %d manufacturers, %d models",
        _index.manufacturer_count, _index.model_count,
    )


async def _replace_rows(conn, entries: list[dict], source: str) -> int:
    """Atomically replace the reference table with `entries` and stamp meta."""
    count = 0
    async with conn.transaction():
        await conn.execute("DELETE FROM rid_identity")
        for e in entries:
            mfr_code = (e.get("mfr_code") or "").strip().upper()
            manufacturer = (e.get("manufacturer") or "").strip() or None
            if not mfr_code and not manufacturer:
                continue
            await conn.execute(
                """INSERT INTO rid_identity (mfr_code, manufacturer, model, serial_prefix)
                   VALUES ($1, $2, $3, $4)""",
                mfr_code or None,
                manufacturer,
                (e.get("model") or "").strip() or None,
                (e.get("serial_prefix") or "").strip().upper() or None,
            )
            count += 1
        await conn.execute(
            """INSERT INTO rid_identity_meta (id, last_refreshed, source, entry_count)
               VALUES (1, NOW(), $1, $2)
               ON CONFLICT (id) DO UPDATE SET
                   last_refreshed = NOW(), source = $1, entry_count = $2""",
            source, count,
        )
    return count


async def seed_if_empty() -> None:
    """On first boot, populate the reference table from the bundled seed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM rid_identity")
        if n and n > 0:
            await load_index()
            return
        if not SEED_PATH.exists():
            log.warning("RID identity seed not found at %s", SEED_PATH)
            return
        data = json.loads(SEED_PATH.read_text())
        count = await _replace_rows(conn, data.get("entries", []), source="seed")
        log.info("RID identity seeded with %d entries", count)
    await load_index()


# ────────────────────────────────────────────────────────────────────────────
# Import / refresh
# ────────────────────────────────────────────────────────────────────────────

# Flexible column-name matching for the FAA DoC export (and hand-made CSVs).
_COL_ALIASES = {
    "mfr_code":      ["mfr_code", "manufacturer_code", "manufacturer code", "cta_code", "code"],
    "manufacturer":  ["manufacturer", "make", "mfr", "company", "brand"],
    "model":         ["model", "model/series", "model series", "product", "product name", "uas_model"],
    "serial_prefix": ["serial_prefix", "serial prefix", "serial", "serial_number", "serial number"],
}


def _pick(row: dict, keys: list[str]):
    norm = {k.strip().lower(): v for k, v in row.items() if k}
    for cand in keys:
        if cand in norm and str(norm[cand]).strip():
            return str(norm[cand]).strip()
    return None


def normalize_rows(rows: list[dict]) -> list[dict]:
    """Map arbitrary FAA/CSV rows onto our entry schema, tolerant of column
    naming. Rows without at least a manufacturer or mfr_code are dropped."""
    out = []
    for row in rows:
        entry = {
            "mfr_code":      _pick(row, _COL_ALIASES["mfr_code"]),
            "manufacturer":  _pick(row, _COL_ALIASES["manufacturer"]),
            "model":         _pick(row, _COL_ALIASES["model"]),
            "serial_prefix": _pick(row, _COL_ALIASES["serial_prefix"]),
        }
        # If a full serial came through as the prefix, keep only its 4-char code
        # as the key when no explicit code column was present.
        if not entry["mfr_code"] and entry["serial_prefix"]:
            sp = entry["serial_prefix"].upper()
            if len(sp) >= 4:
                entry["mfr_code"] = sp[:4]
        if entry["manufacturer"] or entry["mfr_code"]:
            out.append(entry)
    return out


def parse_csv(text: str) -> list[dict]:
    """Parse CSV text into normalized entries."""
    reader = csv.DictReader(io.StringIO(text))
    return normalize_rows(list(reader))


async def import_entries(entries: list[dict], source: str) -> int:
    """Replace the reference table with normalized entries and reload the index."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await _replace_rows(conn, entries, source=source)
    await load_index()
    return count


async def import_csv_text(text: str, source: str = "import") -> int:
    return await import_entries(parse_csv(text), source=source)


async def refresh_from_url(url: str, timeout_s: float = 30.0) -> int:
    """Download the FAA DoC list (or any CSV) from `url` and import it. Runs on
    the server, which has outbound network; raises on fetch/parse failure so the
    caller can report it."""
    import httpx  # local import: only needed on refresh, keeps import graph light

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.text
    # Accept JSON ({"entries":[...]} or a bare list) or CSV.
    stripped = body.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(body)
        rows = data.get("entries", data) if isinstance(data, dict) else data
        entries = normalize_rows(rows)
    else:
        entries = parse_csv(body)
    return await import_entries(entries, source="faa-doc")


async def get_status() -> dict:
    """Current reference-table status for the System panel."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        meta = await conn.fetchrow(
            "SELECT last_refreshed, source, entry_count FROM rid_identity_meta WHERE id = 1"
        )
    return {
        "manufacturers": _index.manufacturer_count,
        "models": _index.model_count,
        "entry_count": (meta["entry_count"] if meta else 0),
        "source": (meta["source"] if meta else None),
        "last_refreshed": (meta["last_refreshed"].isoformat() if meta and meta["last_refreshed"] else None),
    }
