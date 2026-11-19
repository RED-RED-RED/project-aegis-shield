"""
Drone registry: category -> alert behavior, and the notifier honoring it.
Pure logic; the notifier's network send is mocked.
"""

import pytest
from unittest.mock import AsyncMock

from core.config import Settings
from core import drone_registry
from mqtt.notifier import PushoverNotifier


@pytest.fixture(autouse=True)
def clean_cache():
    drone_registry._cache.clear()
    yield
    drone_registry._cache.clear()


def mk(**kw):
    base = dict(pushover_enabled=True, pushover_token="t", pushover_user="u",
                pushover_min_level="medium", pushover_cooldown_s=600,
                pushover_max_per_hour=30, pushover_emergency_categories="",
                pushover_health_enabled=True, pushover_temp_crit_c=80.0,
                pushover_disk_crit_pct=95.0)
    base.update(kw)
    return Settings(**base)


def notif(**kw):
    n = PushoverNotifier(mk(**kw))
    n._send = AsyncMock(return_value=True)
    return n


def alert(level="high", category="no_rid", drone_id="D1", **kw):
    a = dict(level=level, category=category, drone_id=drone_id, node_id="N1",
             title="x", description="d", lat=40.0, lon=-100.0)
    a.update(kw)
    return a


# ── category module ─────────────────────────────────────────────────────────

def test_default_behavior_is_normal():
    assert drone_registry.behavior_for("D1") == "normal"
    assert drone_registry.category_for("D1") == "unknown"


def test_set_and_clear_cached():
    drone_registry.set_cached("D1", "watchlist")
    assert drone_registry.category_for("D1") == "watchlist"
    assert drone_registry.behavior_for("D1") == "escalate"
    drone_registry.set_cached("D1", "unknown")   # default clears it
    assert drone_registry.category_for("D1") == "unknown"


def test_invalid_category_not_cached():
    drone_registry.set_cached("D1", "bogus")
    assert drone_registry.category_for("D1") == "unknown"


def test_behaviors_by_tier():
    drone_registry.set_cached("A", "authorized")
    drone_registry.set_cached("W", "watchlist")
    drone_registry.set_cached("H", "hostile")
    drone_registry.set_cached("N", "nuisance")
    assert drone_registry.behavior_for("A") == "suppress"
    assert drone_registry.behavior_for("W") == "escalate"
    assert drone_registry.behavior_for("H") == "escalate"
    assert drone_registry.behavior_for("N") == "normal"


# ── notifier honors category ────────────────────────────────────────────────

async def test_authorized_suppresses_push():
    n = notif()
    drone_registry.set_cached("D1", "authorized")
    assert await n.notify_alert(alert(level="high")) is False
    n._send.assert_not_awaited()


async def test_watchlist_escalates_low_alert_to_emergency():
    # A LOW alert would normally be gated out (min_level medium); a watchlisted
    # drone pushes anyway, at emergency priority 2.
    n = notif()
    drone_registry.set_cached("D1", "watchlist")
    assert await n.notify_alert(alert(level="low", category="new_drone")) is True
    assert n._send.await_args.args[2] == 2   # priority 2 = emergency


async def test_hostile_escalates():
    n = notif()
    drone_registry.set_cached("D1", "hostile")
    assert await n.notify_alert(alert(level="low", category="new_drone")) is True
    assert n._send.await_args.args[2] == 2


async def test_normal_drone_unaffected():
    n = notif()
    # no category set -> normal gating: low is dropped
    assert await n.notify_alert(alert(level="low", category="new_drone")) is False
