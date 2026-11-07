"""Tests for the System health-history transforms (pure, no DB)."""

from datetime import datetime, timezone

from api.routes.system import _rows_to_series, _rows_to_current, _RANGES


def _ts(mins):
    return datetime(2026, 1, 1, 0, mins, 0, tzinfo=timezone.utc)


def test_rows_to_series_groups_by_node_in_order():
    rows = [
        {"node_id": "ARGUS-01", "bucket": _ts(0), "cpu_pct": 12.0, "mem_pct": 40.0,
         "disk_pct": 50.0, "temp_c": 55.0, "satellites": 9.0},
        {"node_id": "ARGUS-01", "bucket": _ts(10), "cpu_pct": 18.0, "mem_pct": 41.0,
         "disk_pct": 50.0, "temp_c": 57.0, "satellites": 10.0},
        {"node_id": "__server__", "bucket": _ts(0), "cpu_pct": 5.0, "mem_pct": 30.0,
         "disk_pct": 20.0, "temp_c": None, "satellites": None},
    ]
    series = _rows_to_series(rows)
    assert set(series) == {"ARGUS-01", "__server__"}
    assert len(series["ARGUS-01"]) == 2
    assert series["ARGUS-01"][0]["cpu_pct"] == 12.0
    assert series["ARGUS-01"][1]["t"] == _ts(10).isoformat()
    # server has no temperature sensor -> stays None, not 0
    assert series["__server__"][0]["temp_c"] is None


def test_rows_to_current_latest_per_node():
    rows = [
        {"node_id": "ARGUS-01", "time": _ts(10), "cpu_pct": 18.0, "mem_pct": 41.0,
         "disk_pct": 50.0, "temp_c": 57.0, "uptime_s": 3600, "satellites": 10,
         "gps_fix": True, "db_ok": None, "mqtt_ok": None},
        {"node_id": "__server__", "time": _ts(10), "cpu_pct": 5.0, "mem_pct": 30.0,
         "disk_pct": 20.0, "temp_c": None, "uptime_s": 7200, "satellites": None,
         "gps_fix": None, "db_ok": True, "mqtt_ok": True},
    ]
    current = _rows_to_current(rows)
    assert current["ARGUS-01"]["temp_c"] == 57.0
    assert current["ARGUS-01"]["last"] == _ts(10).isoformat()
    assert current["__server__"]["db_ok"] is True
    assert "time" not in current["ARGUS-01"]


def test_ranges_have_expected_keys():
    assert set(_RANGES) == {"1h", "6h", "24h", "7d"}
    for window, bucket in _RANGES.values():
        assert isinstance(bucket, int) and bucket > 0
