"""Tests for flight sessionization, summary/detail and exports (pure, no DB)."""

from datetime import datetime, timezone

from analysis import flights as F


def _row(drone, mins, lat=41.0, lon=-96.0, node="ARGUS-01", **kw):
    base = dict(drone_id=drone, t=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                .replace(minute=mins % 60, hour=mins // 60),
                lat=lat, lon=lon, node_id=node, alt_geo=100.0, alt_baro=None,
                height_agl=80.0, speed_h=5.0, heading=90.0, rssi=-60,
                operator_lat=None, operator_lon=None, operator_id=None,
                ua_type="rotorcraft", description=None, node_lat=41.5, node_lon=-96.5)
    base.update(kw)
    return base


def test_gap_splits_into_separate_flights():
    rows = [_row("D1", 0), _row("D1", 1), _row("D1", 2),   # flight 1
            _row("D1", 30), _row("D1", 31)]                # 28 min gap -> flight 2
    sessions = F.sessionize(rows, gap_s=300)
    assert len(sessions) == 2
    assert len(sessions[0]) == 3 and len(sessions[1]) == 2


def test_different_drones_never_merge():
    rows = [_row("D1", 0), _row("D2", 0), _row("D1", 1)]
    # rows must be pre-sorted by (drone, t); emulate that
    rows.sort(key=lambda r: (r["drone_id"], r["t"]))
    sessions = F.sessionize(rows, gap_s=300)
    drones = {s[0]["drone_id"] for s in sessions}
    assert drones == {"D1", "D2"}


def test_flight_id_roundtrip():
    rows = [_row("DJI-1", 0), _row("DJI-1", 1)]
    s = F.sessionize(rows, 300)[0]
    summ = F.summarize(s)
    drone, start = F.parse_flight_id(summ["flight_id"])
    assert drone == "DJI-1" and isinstance(start, int)


def test_summary_fields():
    rows = [_row("D1", 0, node="A"), _row("D1", 1, node="B"), _row("D1", 2, node="A")]
    summ = F.summarize(F.sessionize(rows, 300)[0],
                       threat={"has_valid_rid": True, "threat_score": 55.0, "threat_level": "medium"})
    assert summ["point_count"] == 3
    assert summ["detecting_nodes"] == ["A", "B"]
    assert summ["duration_s"] == 120
    assert summ["threat_level"] == "medium" and summ["has_valid_rid"] is True


def test_detail_has_path_and_nodes():
    rows = [_row("D1", 0, operator_lat=41.1, operator_lon=-96.1), _row("D1", 1)]
    d = F.detail(F.sessionize(rows, 300)[0])
    assert len(d["path"]) == 2
    assert d["path"][0]["alt_m"] == 100.0
    assert d["operator"] == {"lat": 41.1, "lon": -96.1}
    assert d["nodes"] and d["nodes"][0]["lat"] == 41.5


def test_exports_contain_coordinates():
    rows = [_row("D1", 0), _row("D1", 1)]
    d = F.detail(F.sessionize(rows, 300)[0])
    gpx = F.to_gpx(d); kml = F.to_kml(d); csv = F.to_csv(d)
    assert "<trkpt" in gpx and "41.0" in gpx
    assert "<LineString>" in kml and "-96.0,41.0" in kml
    assert csv.splitlines()[0].startswith("time,lat,lon")
    assert len(csv.strip().splitlines()) == 3   # header + 2 points
