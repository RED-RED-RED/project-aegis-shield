"""
analysis/flights.py
===================
Turn the raw detections stream into discrete flights (sorties). A flight is a
run of detections for one drone_id with no gap longer than flight_gap_s; a new
gap starts a new flight. Pure functions here, no DB, so they are testable with
plain dicts.

Only detections that carry a drone position (lat/lon) are considered — a flight
is a path, and a path needs coordinates. RF-only / no-RID contacts have no
position and are handled separately (they are not flights).
"""

from __future__ import annotations

from datetime import datetime


def _epoch(t) -> float:
    return t.timestamp() if isinstance(t, datetime) else float(t)


def flight_id_for(drone_id: str, start) -> str:
    """Stable id for a flight: drone_id + its start epoch (whole seconds)."""
    return f"{drone_id}@{int(_epoch(start))}"


def parse_flight_id(flight_id: str):
    """-> (drone_id, start_epoch:int). Splits on the last '@'."""
    drone_id, _, start = flight_id.rpartition("@")
    if not drone_id:
        return flight_id, None
    try:
        return drone_id, int(start)
    except ValueError:
        return drone_id, None


def sessionize(rows: list[dict], gap_s: int) -> list[list[dict]]:
    """
    Split detection rows into sessions (flights). rows must be sorted by
    (drone_id, time). Each row needs 'drone_id' and 't' (datetime or epoch).
    Returns a list of sessions, each a list of the rows in that flight.
    """
    sessions: list[list[dict]] = []
    cur: list[dict] = []
    prev_drone = None
    prev_t = None
    for r in rows:
        d = r["drone_id"]
        t = _epoch(r["t"])
        if d != prev_drone or prev_t is None or (t - prev_t) > gap_s:
            if cur:
                sessions.append(cur)
            cur = [r]
        else:
            cur.append(r)
        prev_drone = d
        prev_t = t
    if cur:
        sessions.append(cur)
    return sessions


def _first(session, key):
    for r in session:
        if r.get(key) is not None:
            return r[key]
    return None


def _last(session, key):
    for r in reversed(session):
        if r.get(key) is not None:
            return r[key]
    return None


def summarize(session: list[dict], threat: dict | None = None) -> dict:
    """One flight -> a summary dict (no full path). threat is the drone_tracks
    row for this drone_id, if any."""
    drone_id = session[0]["drone_id"]
    start = session[0]["t"]
    end = session[-1]["t"]
    nodes = sorted({r["node_id"] for r in session if r.get("node_id")})
    op_lat = _last(session, "operator_lat")
    op_lon = _last(session, "operator_lon")
    threat = threat or {}
    return {
        "flight_id":   flight_id_for(drone_id, start),
        "drone_id":    drone_id,
        "ua_type":     _last(session, "ua_type"),
        "operator_id": _last(session, "operator_id"),
        "description": _last(session, "description"),
        "start":       start.isoformat() if isinstance(start, datetime) else start,
        "end":         end.isoformat() if isinstance(end, datetime) else end,
        "duration_s":  int(_epoch(end) - _epoch(start)),
        "point_count": len(session),
        "detecting_nodes": nodes,
        "node_count":  len(nodes),
        "has_operator_loc": op_lat is not None and op_lon is not None,
        "has_valid_rid":   threat.get("has_valid_rid"),
        "threat_score":    threat.get("threat_score"),
        "threat_level":    threat.get("threat_level"),
    }


def detail(session: list[dict], threat: dict | None = None) -> dict:
    """One flight -> full detail with the ordered path and derived series."""
    s = summarize(session, threat)
    path = []
    for r in session:
        t = r["t"]
        path.append({
            "t":       t.isoformat() if isinstance(t, datetime) else t,
            "lat":     r.get("lat"),
            "lon":     r.get("lon"),
            "alt_m":   r.get("alt_geo") if r.get("alt_geo") is not None else r.get("alt_baro"),
            "agl_m":   r.get("height_agl"),
            "speed_ms": r.get("speed_h"),
            "heading": r.get("heading"),
            "rssi":    r.get("rssi"),
            "node_id": r.get("node_id"),
        })
    # Distinct detecting nodes with a position (for map markers).
    nodes = {}
    for r in session:
        nid = r.get("node_id")
        if nid and nid not in nodes and r.get("node_lat") is not None:
            nodes[nid] = {"node_id": nid, "lat": r["node_lat"], "lon": r["node_lon"]}
    op_lat = _last(session, "operator_lat")
    op_lon = _last(session, "operator_lon")
    s.update({
        "path": path,
        "nodes": list(nodes.values()),
        "operator": ({"lat": op_lat, "lon": op_lon}
                     if op_lat is not None and op_lon is not None else None),
    })
    return s


# ── Exports (records / evidence) ─────────────────────────────────────────────

def _xml_escape(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_csv(d: dict) -> str:
    lines = ["time,lat,lon,alt_m,agl_m,speed_ms,heading_deg,rssi_dbm,node"]
    for p in d["path"]:
        if p["lat"] is None or p["lon"] is None:
            continue
        lines.append(",".join("" if v is None else str(v) for v in [
            p["t"], p["lat"], p["lon"], p["alt_m"], p["agl_m"],
            p["speed_ms"], p["heading"], p["rssi"], p["node_id"],
        ]))
    return "\n".join(lines) + "\n"


def to_gpx(d: dict) -> str:
    name = _xml_escape(d["drone_id"])
    pts = []
    for p in d["path"]:
        if p["lat"] is None or p["lon"] is None:
            continue
        ele = f"<ele>{p['alt_m']}</ele>" if p["alt_m"] is not None else ""
        pts.append(f'<trkpt lat="{p["lat"]}" lon="{p["lon"]}">{ele}<time>{_xml_escape(p["t"])}</time></trkpt>')
    body = "\n".join(pts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="AEGIS" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f'  <trk><name>{name}</name><trkseg>\n{body}\n  </trkseg></trk>\n'
        '</gpx>\n'
    )


def to_kml(d: dict) -> str:
    coords = " ".join(
        f'{p["lon"]},{p["lat"]},{p["alt_m"] if p["alt_m"] is not None else 0}'
        for p in d["path"] if p["lat"] is not None and p["lon"] is not None
    )
    name = _xml_escape(d["drone_id"])
    op = ""
    if d.get("operator"):
        op = (f'<Placemark><name>Operator</name><Point><coordinates>'
              f'{d["operator"]["lon"]},{d["operator"]["lat"]},0'
              f'</coordinates></Point></Placemark>')
    nodes = "".join(
        f'<Placemark><name>{_xml_escape(n["node_id"])}</name><Point><coordinates>'
        f'{n["lon"]},{n["lat"]},0</coordinates></Point></Placemark>'
        for n in d.get("nodes", [])
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
        f'  <name>AEGIS flight {name}</name>\n'
        f'  <Placemark><name>{name} track</name>'
        f'<LineString><tessellate>1</tessellate>'
        f'<coordinates>{coords}</coordinates></LineString></Placemark>\n'
        f'  {op}{nodes}\n'
        '</Document></kml>\n'
    )

