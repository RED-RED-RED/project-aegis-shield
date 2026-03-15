"""
calibration/collector.py
========================
Fetches detection records from the server API for a specified time window
and pairs each detection with the drone's true position.

"True position" sources (in order of preference):
  1. GPX track file  — exported from drone controller app (highest accuracy)
  2. SRT file        — subtitle file with GPS from DJI video (±3m)
  3. Drone's own RID broadcast — usable for calibration ONLY because we
     know this is our cooperative drone and we trust its GPS. (Not
     appropriate for adversarial detection — that's what we're calibrating
     *for*.)

The collector syncs timestamps between sources and returns a list of
CalibrationSample objects ready for the engine.
"""

import csv
import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.parse import urlencode

from calibration.engine import CalibrationSample

log = logging.getLogger("calibration.collector")

EARTH_R = 6_371_000.0


# ── Ground-truth position at a given timestamp ────────────────────────────

@dataclass
class TruthPoint:
    ts:  float   # unix
    lat: float
    lon: float
    alt: float = 0.0


# ── Distance helpers ──────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def interpolate_truth(track: list[TruthPoint], ts: float) -> Optional[TruthPoint]:
    """
    Linear interpolation between two track points bracketing `ts`.
    Returns None if ts is outside the track window.
    """
    if not track:
        return None

    # Binary search for bracket
    lo, hi = 0, len(track) - 1
    if ts < track[lo].ts or ts > track[hi].ts:
        return None

    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if track[mid].ts <= ts:
            lo = mid
        else:
            hi = mid

    p0, p1 = track[lo], track[hi]
    dt = p1.ts - p0.ts
    if dt < 1e-6:
        return p0

    alpha = (ts - p0.ts) / dt
    return TruthPoint(
        ts  = ts,
        lat = p0.lat + alpha * (p1.lat - p0.lat),
        lon = p0.lon + alpha * (p1.lon - p0.lon),
        alt = p0.alt + alpha * (p1.alt - p0.alt),
    )


# ── Ground-truth file parsers ─────────────────────────────────────────────

def parse_gpx(path: str) -> list[TruthPoint]:
    """
    Parse a GPX track file (exported from most flight controllers).
    Supports both <trkpt> and <wpt> elements.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Handle namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    points = []
    for tag in [f"{ns}trkpt", f"{ns}wpt", f"{ns}rtept"]:
        for pt in root.iter(tag):
            try:
                lat = float(pt.attrib["lat"])
                lon = float(pt.attrib["lon"])
                ele_el = pt.find(f"{ns}ele")
                alt = float(ele_el.text) if ele_el is not None else 0.0
                time_el = pt.find(f"{ns}time")
                if time_el is None:
                    continue
                ts = datetime.fromisoformat(
                    time_el.text.replace("Z", "+00:00")
                ).timestamp()
                points.append(TruthPoint(ts=ts, lat=lat, lon=lon, alt=alt))
            except (KeyError, ValueError, AttributeError):
                continue

    points.sort(key=lambda p: p.ts)
    log.info(f"GPX: loaded {len(points)} track points from {path}")
    return points


def parse_srt(path: str) -> list[TruthPoint]:
    """
    Parse DJI SRT subtitle file (embedded GPS metadata in video files).

    DJI SRT format:
        1
        00:00:00,033 --> 00:00:00,066
        <font size="28">SrtCnt : 1, DiffTime : 33ms
        2024-03-15 10:23:41.033
        [iso : 100] [shutter : 1/2000.0] [fnum : 280] [ev : 0]
        [latitude: 42.36012] [longitude: -71.05888] [rel_alt: 42.341 abs_alt: 54.123]
        </font>
    """
    points = []
    content = open(path, encoding="utf-8", errors="replace").read()

    # Match timestamp lines
    date_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)"
    )
    lat_pat  = re.compile(r"\[latitude\s*:\s*([-\d.]+)\]")
    lon_pat  = re.compile(r"\[longitude\s*:\s*([-\d.]+)\]")
    alt_pat  = re.compile(r"\[rel_alt\s*:\s*([-\d.]+)")

    blocks = content.split("\n\n")
    for block in blocks:
        date_m = date_pat.search(block)
        lat_m  = lat_pat.search(block)
        lon_m  = lon_pat.search(block)
        if not (date_m and lat_m and lon_m):
            continue
        try:
            ts  = datetime.fromisoformat(date_m.group(1)).replace(
                tzinfo=timezone.utc).timestamp()
            lat = float(lat_m.group(1))
            lon = float(lon_m.group(1))
            alt_m = alt_pat.search(block)
            alt = float(alt_m.group(1)) if alt_m else 0.0
            points.append(TruthPoint(ts=ts, lat=lat, lon=lon, alt=alt))
        except (ValueError, AttributeError):
            continue

    points.sort(key=lambda p: p.ts)
    log.info(f"SRT: loaded {len(points)} GPS frames from {path}")
    return points


def parse_csv_track(path: str) -> list[TruthPoint]:
    """
    Parse a simple CSV file with columns: timestamp,lat,lon,alt_m
    timestamp can be ISO-8601 or unix float.
    """
    points = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                raw_ts = row.get("timestamp") or row.get("ts") or row.get("time")
                try:
                    ts = float(raw_ts)
                except ValueError:
                    ts = datetime.fromisoformat(
                        raw_ts.replace("Z", "+00:00")
                    ).timestamp()

                lat = float(row.get("lat") or row.get("latitude"))
                lon = float(row.get("lon") or row.get("longitude"))
                alt = float(row.get("alt_m") or row.get("alt") or row.get("altitude") or 0)
                points.append(TruthPoint(ts=ts, lat=lat, lon=lon, alt=alt))
            except (KeyError, ValueError, TypeError):
                continue

    points.sort(key=lambda p: p.ts)
    log.info(f"CSV: loaded {len(points)} track points from {path}")
    return points


# ── Server detection fetcher ──────────────────────────────────────────────

def fetch_detections_from_api(
    server_url: str,
    drone_id: str,
    start_ts: float,
    end_ts: float,
    limit: int = 5000,
) -> list[dict]:
    """
    Fetch detection records from the server REST API for the calibration window.
    Returns raw dicts from the /api/detections endpoint.
    """
    from datetime import datetime, timezone

    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    end_iso   = datetime.fromtimestamp(end_ts,   tz=timezone.utc).isoformat()

    params = urlencode({
        "drone_id": drone_id,
        "start":    start_iso,
        "end":      end_iso,
        "limit":    limit,
    })
    url = f"{server_url}/api/detections?{params}"

    log.info(f"Fetching detections: {url}")
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    log.info(f"Fetched {len(data)} detections from server")
    return data


def fetch_nodes_from_api(server_url: str) -> list[dict]:
    """Fetch node positions from the server."""
    url = f"{server_url}/api/nodes"
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as r:
        return json.loads(r.read())


# ── Sample builder ────────────────────────────────────────────────────────

def build_samples(
    detections:  list[dict],
    truth_track: list[TruthPoint],
    nodes:       list[dict],
    truth_source: str = "track",    # "track" | "rid_broadcast"
    max_interp_gap_s: float = 5.0,  # Reject if truth track has a gap here
) -> list[CalibrationSample]:
    """
    Pair each detection with a ground-truth position and compute true distance.

    truth_source="track"         — uses GPX/SRT/CSV truth track
    truth_source="rid_broadcast" — uses the drone's own RID lat/lon
                                   (only valid for cooperative calibration drone)
    """
    node_map = {n["node_id"]: n for n in nodes}
    samples  = []
    skipped  = {"no_truth": 0, "no_node": 0, "no_rssi": 0, "gap": 0}

    for det in detections:
        node_id = det.get("node_id")
        rssi    = det.get("rssi")
        ts_raw  = det.get("detected_at")

        if rssi is None or rssi < -105:
            skipped["no_rssi"] += 1
            continue

        if node_id not in node_map:
            skipped["no_node"] += 1
            continue

        node = node_map[node_id]
        node_lat = node.get("lat")
        node_lon = node.get("lon")
        if not node_lat or not node_lon:
            skipped["no_node"] += 1
            continue

        # Parse detection timestamp
        try:
            if isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            else:
                ts = datetime.fromisoformat(
                    str(ts_raw).replace("Z", "+00:00")
                ).timestamp()
        except (ValueError, TypeError):
            skipped["no_truth"] += 1
            continue

        # Get true drone position
        if truth_source == "rid_broadcast":
            drone_lat = det.get("drone_lat")
            drone_lon = det.get("drone_lon")
            if not drone_lat or not drone_lon:
                skipped["no_truth"] += 1
                continue
            truth_pt = TruthPoint(ts=ts, lat=drone_lat, lon=drone_lon)
        else:
            # Interpolate from provided truth track
            truth_pt = interpolate_truth(truth_track, ts)
            if truth_pt is None:
                skipped["no_truth"] += 1
                continue

            # Check for large gaps in truth track around this timestamp
            nearby = [p for p in truth_track if abs(p.ts - ts) < max_interp_gap_s * 2]
            if len(nearby) < 2:
                skipped["gap"] += 1
                continue

        # Compute true 3D distance (project altitude difference too)
        horiz_m = haversine_m(node_lat, node_lon, truth_pt.lat, truth_pt.lon)
        alt_diff = abs(truth_pt.alt - (node.get("alt") or 0.0))
        true_dist = math.sqrt(horiz_m**2 + alt_diff**2)

        if true_dist < 5.0:
            continue   # Too close — near-field effects dominate

        samples.append(CalibrationSample(
            node_id   = node_id,
            rssi      = float(rssi),
            true_dist = true_dist,
            ts        = ts,
            drone_lat = truth_pt.lat,
            drone_lon = truth_pt.lon,
            node_lat  = node_lat,
            node_lon  = node_lon,
        ))

    log.info(
        f"Built {len(samples)} samples. Skipped: {skipped}"
    )
    return samples
