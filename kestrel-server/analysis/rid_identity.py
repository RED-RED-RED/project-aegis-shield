"""
analysis/rid_identity.py
========================
Resolve a broadcast Remote ID serial to a drone make and model.

How matching works
------------------
The FAA Declaration of Compliance (DoC) system publishes, for every filing, the
full list of serial numbers that product ships with. Within a filing the serials
share a model-specific prefix — e.g. every Skydio "X10 with Parachute" serial is
`X10PCH` + 4 characters. So a broadcast serial is resolved by **longest-prefix
match** against those prefixes: the longest known prefix the serial starts with
gives the make and model. This is the same method community receivers use, and
it reaches model level, not just manufacturer.

The reference table (prefix → make/model) is built by scraping the FAA DoC API
(see rebuild_from_faa). A small verified seed ships in data/rid_identity_seed.json
and loads on first boot so common drones resolve before the first scrape.

Graded id_source tells the UI how confident the result is:

    doc          make + model, from a matched DoC serial prefix
    prefix       make only (a prefix with no model recorded)
    unknown_mfr  no prefix match, but the serial is valid CTA-2063-A
    unverified   no prefix match and the serial isn't valid CTA-2063-A
                 (a non-compliance / spoof signal)
    na           id_type isn't a serial (CAA registration, etc.)

A DoC-prefix match wins over strict CTA-2063-A validity: some real products use
serials the standard would reject (the FAA marks them NO_FORMAT), so a known
serial is identified even when it isn't CTA-compliant. Validity is still tracked
separately as serial_valid for the spoof signal.

The reference table lives in the `rid_identity` DB table (persists in the
TimescaleDB volume) with an in-memory prefix index rebuilt on load and refresh.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from pathlib import Path

from db.database import get_pool

log = logging.getLogger("rid-identity")

# Allowed CTA-2063-A serial characters: digits + uppercase letters, excluding I and O.
_ALLOWED = set("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ")
_HEX_LEN = set("123456789ABCDEF")   # length code is 1..F (never 0)

SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "rid_identity_seed.json"

# Shortest prefix we'll trust as model-specific from a scrape. Below this the
# serials diverge too early to pin a model (it's just the maker-code region),
# so we emit nothing and let the maker code carry the make. Skydio's "X10PCH"
# (6) passes; DJI's "1581F" (5, random after) does not.
MIN_PREFIX_LEN = 6


# ────────────────────────────────────────────────────────────────────────────
# CTA-2063-A structural parse (used only for the spoof / validity signal)
# ────────────────────────────────────────────────────────────────────────────

def parse_cta2063a(serial: str) -> dict:
    """Validate a CTA-2063-A serial and pull out its parts. Returns `valid`
    (bool) and `mfr_code` (first 4 chars when present, for display even on
    invalid input)."""
    if not serial:
        return {"valid": False, "mfr_code": None}
    s = serial.strip().upper()
    mfr_code = s[:4] if len(s) >= 4 else None
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
# Prefix index — longest-prefix match over DoC serial prefixes
# ────────────────────────────────────────────────────────────────────────────

class PrefixIndex:
    """Maps a serial prefix to (make, model). Matches a serial to the longest
    prefix it starts with. Small (a few thousand prefixes), held in memory."""

    def __init__(self):
        self.entries: list[tuple[str, str | None, str | None]] = []
        # 4-char CTA manufacturer code -> the make that actually owns it. Used to
        # anchor make and reject a third-party filing that reuses another
        # maker's serial space (e.g. "SBG Modified DJI Mini" under DJI's 1581).
        self.code_make: dict[str, str] = {}
        self._sorted = True

    def clear(self):
        self.entries = []
        self.code_make = {}
        self._sorted = True

    def add(self, prefix, manufacturer=None, model=None):
        prefix = (prefix or "").strip().upper()
        if not prefix:
            return
        self.entries.append((prefix, (manufacturer or None), (model or None)))
        # A 4-char model-less entry is a manufacturer-code -> make anchor.
        if len(prefix) == 4 and manufacturer and not model:
            self.code_make[prefix] = manufacturer
        self._sorted = False

    def _ensure_sorted(self):
        if not self._sorted:
            # Longest prefix first so the most specific match wins.
            self.entries.sort(key=lambda e: len(e[0]), reverse=True)
            self._sorted = True

    def match(self, serial: str):
        s = (serial or "").upper()
        if not s:
            return None, None, None
        self._ensure_sorted()
        for prefix, make, model in self.entries:
            if s.startswith(prefix):
                return make, model, prefix
        return None, None, None

    @property
    def prefix_count(self) -> int:
        return len(self.entries)

    @property
    def make_count(self) -> int:
        return len({m for _, m, _ in self.entries if m})

    @property
    def model_count(self) -> int:
        return len({(m, md) for _, m, md in self.entries if md})


_index = PrefixIndex()


# ────────────────────────────────────────────────────────────────────────────
# Enrichment (called per detection at ingest)
# ────────────────────────────────────────────────────────────────────────────

def _same_make(a: str, b: str) -> bool:
    """True when make `a` belongs to code-owner `b` — equal, or a's name starts
    with b's (so 'DJI Technologies' counts as DJI, but 'SBG Modified DJI Mini'
    does not, since it doesn't start with 'DJI')."""
    if not a or not b:
        return False
    return a.strip().upper().startswith(b.strip().upper())


def enrich(id_type: str, serial: str) -> dict:
    """Map a broadcast (id_type, serial) to {make, model, mfr_code, id_source,
    serial_valid}. Pure and cheap; reads only the in-memory index."""
    out = {"make": None, "model": None, "mfr_code": None,
           "id_source": "na", "serial_valid": None}

    if (id_type or "").strip().lower() != "serial_number":
        return out  # CAA registration / unknown — nothing to decode

    parsed = parse_cta2063a(serial)
    code = parsed.get("mfr_code")
    out["mfr_code"] = code
    out["serial_valid"] = parsed["valid"]

    make, model, _prefix = _index.match(serial)
    auth = _index.code_make.get(code) if code else None   # who owns this code

    if make and auth and not _same_make(make, auth):
        # A filing in another maker's serial space (third-party / modifier):
        # trust only the code owner, drop the misattributed model.
        out.update(make=auth, model=None, id_source="prefix")
    elif make and model:
        out.update(make=make, model=model, id_source="doc")
    elif make:
        out.update(make=make, id_source="prefix")
    elif auth:
        out.update(make=auth, id_source="prefix")
    elif parsed["valid"]:
        out["id_source"] = "unknown_mfr"   # valid serial we just don't have on file
    else:
        out["id_source"] = "unverified"    # malformed serial + unknown = spoof signal
    return out


# ────────────────────────────────────────────────────────────────────────────
# Prefix extraction (from a filing's serial list)
# ────────────────────────────────────────────────────────────────────────────

def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    lo, hi = min(strings), max(strings)
    i = 0
    while i < len(lo) and i < len(hi) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def extract_prefixes(values: list[str], min_len: int = MIN_PREFIX_LEN) -> list[str]:
    """Derive the model-identifying prefix(es) from a filing's serial sample.

    FAA filings specify serials two ways:
      - enumerated: a list of individual serials (e.g. Skydio "X10PCH..."), whose
        shared prefix is their longest common prefix.
      - RANGE: a "start-end" value (e.g. "1581F8HG000000000000-1581F8HGZZ...ZZ"),
        whose model prefix is the common prefix of the two endpoints ("1581F8HG").
    A serial never contains a dash (CTA-2063-A excludes it), so a dash marks a
    range. We emit a prefix only when it's long enough to be model-specific
    (>= min_len); a shorter one is just the maker-code region, so we emit nothing
    and let the maker code carry the make."""
    range_prefixes: list[str] = []
    enumerated: list[str] = []
    for v in values:
        v = (v or "").strip().upper()
        if not v:
            continue
        if "-" in v:
            a, _, b = v.partition("-")
            p = _common_prefix([a.strip(), b.strip()])
            if len(p) >= min_len:
                range_prefixes.append(p)
        else:
            enumerated.append(v)

    out = list(dict.fromkeys(range_prefixes))   # dedup, preserve order
    if enumerated:
        lcp = _common_prefix(sorted(set(enumerated)))
        if len(lcp) >= min_len and lcp not in out:
            out.append(lcp)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Reference-table load / seed / replace
# ────────────────────────────────────────────────────────────────────────────

async def load_index() -> None:
    """Rebuild the in-memory prefix index from the DB reference table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT serial_prefix, manufacturer, model FROM rid_identity "
            "WHERE serial_prefix IS NOT NULL"
        )
    _index.clear()
    for r in rows:
        _index.add(r["serial_prefix"], r["manufacturer"], r["model"])
    log.info(
        "RID identity index: %d prefixes, %d makes, %d models",
        _index.prefix_count, _index.make_count, _index.model_count,
    )


async def _replace_rows(conn, entries: list[dict], source: str) -> int:
    """Atomically replace the reference table with `entries` and stamp meta."""
    count = 0
    async with conn.transaction():
        await conn.execute("DELETE FROM rid_identity")
        for e in entries:
            prefix = (e.get("serial_prefix") or e.get("mfr_code") or "").strip().upper()
            manufacturer = (e.get("manufacturer") or "").strip() or None
            if not prefix:
                continue
            await conn.execute(
                """INSERT INTO rid_identity (mfr_code, manufacturer, model, serial_prefix)
                   VALUES ($1, $2, $3, $4)""",
                prefix[:4],
                manufacturer,
                (e.get("model") or "").strip() or None,
                prefix,
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


async def import_entries(entries: list[dict], source: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await _replace_rows(conn, entries, source=source)
    await load_index()
    return count


# ────────────────────────────────────────────────────────────────────────────
# CSV import (manual / air-gapped path)
# ────────────────────────────────────────────────────────────────────────────

_COL_ALIASES = {
    "serial_prefix": ["serial_prefix", "serial prefix", "prefix", "serial", "serial_number", "serial number"],
    "manufacturer":  ["manufacturer", "make", "makename", "mfr", "company", "brand"],
    "model":         ["model", "modelname", "model/series", "model series", "product"],
}


def _pick(row: dict, keys: list[str]):
    norm = {str(k).strip().lower(): v for k, v in row.items() if k}
    for cand in keys:
        if cand in norm and str(norm[cand]).strip():
            return str(norm[cand]).strip()
    return None


def normalize_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        entry = {
            "serial_prefix": _pick(row, _COL_ALIASES["serial_prefix"]),
            "manufacturer":  _pick(row, _COL_ALIASES["manufacturer"]),
            "model":         _pick(row, _COL_ALIASES["model"]),
        }
        if entry["serial_prefix"]:
            out.append(entry)
    return out


def parse_csv(text: str) -> list[dict]:
    return normalize_rows(list(csv.DictReader(io.StringIO(text))))


async def import_csv_text(text: str, source: str = "import") -> int:
    return await import_entries(parse_csv(text), source=source)


# ────────────────────────────────────────────────────────────────────────────
# FAA DoC scrape — build the prefix table from uasdoc.faa.gov
# ────────────────────────────────────────────────────────────────────────────

# Progress state for the System panel while a rebuild runs.
_build_state: dict = {
    "building": False, "done": 0, "total": 0,
    "prefixes": 0, "failed": 0, "error": None, "started_at": None,
}


def build_state() -> dict:
    return dict(_build_state)


def _faa_items(payload: dict) -> list[dict]:
    """Both FAA endpoints wrap rows in {"data": {"items": [...]}}."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(payload, list):
        return payload
    return []


async def _faa_get(client, url: str, params: dict, attempts: int = 4):
    """GET returning parsed JSON, with backoff on the FAA WAF's throttling.

    The uasdoc.faa.gov edge returns 403/429 (an HTML 'Access Denied' page, not
    JSON) when hit too fast, so a plain scrape silently drops filings. Retry
    those with growing backoff and surface a clean error if they persist."""
    import httpx
    last = None
    for i in range(attempts):
        try:
            r = await client.get(url, params=params)
            if r.status_code in (403, 429, 500, 502, 503):
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(1.5 * (2 ** i))   # 1.5, 3, 6, 12s
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            last = str(e)
            await asyncio.sleep(1.5 * (2 ** i))
    raise RuntimeError(f"request failed after {attempts} attempts: {last}")


async def rebuild_from_faa(base_url: str, page_size: int = 100,
                           delay_s: float = 0.8) -> int:
    """
    Scrape the FAA DoC API and rebuild the prefix table. Runs in-process on the
    server (which has outbound network). Pages the filing list, fetches each
    filing's serial snapshot, extracts the model prefix(es), and imports
    (prefix → make, model). Returns the number of prefixes stored.

    Paced gently with retry/backoff because the FAA edge throttles bursts
    (403/429); `_build_state["failed"]` reports any filings that never came
    through so a throttled run isn't mistaken for a complete one.

    base_url is the API root, e.g. "https://uasdoc.faa.gov/api/v1".
    """
    import httpx
    import random
    from collections import Counter

    base = base_url.rstrip("/")
    prefix_map: dict[str, dict] = {}         # model prefix -> {manufacturer, model}
    code_makes: dict[str, Counter] = {}      # 4-char maker code -> Counter(make)

    async def _pace():
        # delay_s plus up to 50% jitter, so the request cadence isn't a uniform
        # pattern the FAA edge might flag.
        await asyncio.sleep(delay_s + random.uniform(0, delay_s * 0.5))

    _build_state.update(building=True, done=0, total=0, prefixes=0, failed=0,
                        error=None, started_at=time.time())
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # 1. Page the filing list.
            filings: list[dict] = []
            page = 0
            while True:
                body = await _faa_get(client, f"{base}/publicDOCRev",
                                      {"itemsPerPage": page_size, "pageIndex": page})
                items = _faa_items(body)
                if not items:
                    break
                filings.extend(items)
                total = (body.get("data") or {}).get("totalItems")
                page += 1
                if total and len(filings) >= int(total):
                    break
                if page > 1000:   # safety valve
                    break
                await _pace()

            _build_state["total"] = len(filings)

            # 2. Per filing, fetch the serial snapshot and extract prefix(es).
            for f in filings:
                tracking = f.get("trackingNumber") or f.get("docTrackingNumber")
                make = (f.get("makeName") or f.get("manufacturer") or "").strip() or None
                model = (f.get("modelName") or f.get("model") or "").strip() or None
                if tracking:
                    try:
                        body = await _faa_get(client, f"{base}/serialNumbers", {
                            "snapshot": "true", "isPublic": "true",
                            "findBy": "docTrackingNumber",
                            "docTrackingNumber": tracking,
                            "formatForDocView": "true",
                        })
                        values = [it.get("value") for it in _faa_items(body)
                                  if it.get("value")]
                        prefixes = extract_prefixes(values)
                        # Tally the maker code (first 4 chars) -> make, so the
                        # make that files the most products under a code owns it.
                        code = (prefixes[0][:4] if prefixes
                                else (values[0][:4].upper() if values else None))
                        if code and make:
                            code_makes.setdefault(code, Counter())[make] += 1
                        for p in prefixes:
                            cur = prefix_map.get(p)
                            if cur is None:
                                prefix_map[p] = {"manufacturer": make, "model": model}
                            elif not cur.get("model") and model:
                                cur["model"] = model
                    except Exception as e:
                        _build_state["failed"] += 1
                        log.warning("serials fetch failed for %s: %s", tracking, e)
                _build_state["done"] += 1
                _build_state["prefixes"] = len(prefix_map)
                await _pace()

        # Model prefixes (>= MIN_PREFIX_LEN) ...
        entries = [{"serial_prefix": p, "manufacturer": v["manufacturer"], "model": v["model"]}
                   for p, v in prefix_map.items()]
        # ... plus one maker-code -> make anchor per code (plurality winner).
        code_entries = {c: ctr.most_common(1)[0][0] for c, ctr in code_makes.items() if ctr}
        # Keep any seed-defined code anchors the scrape didn't cover (e.g. DJI 1581).
        try:
            for e in json.loads(SEED_PATH.read_text()).get("entries", []):
                sp = (e.get("serial_prefix") or "").strip().upper()
                if len(sp) == 4 and e.get("manufacturer") and not e.get("model"):
                    code_entries.setdefault(sp, e["manufacturer"])
        except Exception:
            pass
        for code, mk in code_entries.items():
            entries.append({"serial_prefix": code, "manufacturer": mk, "model": None})

        if not entries:
            raise RuntimeError("No serial prefixes extracted — check the source URL/API shape.")
        count = await import_entries(entries, source="faa-doc")
        _build_state.update(prefixes=count)
        return count
    except Exception as e:
        _build_state["error"] = str(e)
        log.warning("RID identity rebuild failed: %s", e)
        raise
    finally:
        _build_state["building"] = False


async def get_status() -> dict:
    """Reference-table status + any in-progress build, for the System panel."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        meta = await conn.fetchrow(
            "SELECT last_refreshed, source, entry_count FROM rid_identity_meta WHERE id = 1"
        )
    return {
        "prefixes": _index.prefix_count,
        "makes": _index.make_count,
        "models": _index.model_count,
        "entry_count": (meta["entry_count"] if meta else 0),
        "source": (meta["source"] if meta else None),
        "last_refreshed": (meta["last_refreshed"].isoformat() if meta and meta["last_refreshed"] else None),
        "build": build_state(),
    }
