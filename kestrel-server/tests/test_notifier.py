"""Pushover notifier logic — gating, cooldown, emergency, rate cap, health."""

import asyncio
from unittest.mock import AsyncMock

from core.config import Settings
from mqtt.notifier import PushoverNotifier


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
    n._send = AsyncMock(return_value=True)   # never hit the network
    return n


def alert(level="high", category="no_rid", drone_id="D1", **kw):
    a = dict(level=level, category=category, drone_id=drone_id, node_id="N1",
             title=f"{category} alert", description="desc", lat=40.0, lon=-100.0)
    a.update(kw)
    return a


# ── gating ────────────────────────────────────────────────────────────────────

async def test_disabled_is_noop():
    n = notif(pushover_enabled=False)
    assert await n.notify_alert(alert()) is False
    n._send.assert_not_awaited()


async def test_missing_creds_is_noop():
    n = notif(pushover_token="")
    assert await n.notify_alert(alert()) is False


async def test_low_level_gated_out():
    n = notif()   # min_level medium
    assert await n.notify_alert(alert(level="low", category="new_drone")) is False


async def test_medium_passes_priority_0():
    n = notif()
    assert await n.notify_alert(alert(level="medium", category="speed")) is True
    assert n._send.await_args.args[2] == 0


async def test_high_priority_1():
    n = notif()
    await n.notify_alert(alert(level="high"))
    assert n._send.await_args.args[2] == 1


async def test_emergency_category_priority_2_and_bypasses_gate():
    # gps_jamming is medium but listed as emergency -> priority 2, always sends
    n = notif(pushover_emergency_categories="spoofed_position,gps_jamming")
    assert await n.notify_alert(alert(level="medium", category="gps_jamming", drone_id=None)) is True
    assert n._send.await_args.args[2] == 2


# ── cooldown + escalation ─────────────────────────────────────────────────────

async def test_cooldown_blocks_repeat_same_drone():
    n = notif()
    assert await n.notify_alert(alert(level="medium", category="speed")) is True
    assert await n.notify_alert(alert(level="medium", category="altitude")) is False  # same drone


async def test_escalation_overrides_cooldown():
    n = notif()
    assert await n.notify_alert(alert(level="medium", category="speed")) is True
    assert await n.notify_alert(alert(level="high", category="no_rid")) is True       # higher level breaks through


async def test_different_drones_independent():
    n = notif()
    assert await n.notify_alert(alert(drone_id="A")) is True
    assert await n.notify_alert(alert(drone_id="B")) is True


# ── rate cap (real _send path, rate logic is inside it) ────────────────────────

def test_rate_cap():
    n = PushoverNotifier(mk(pushover_max_per_hour=2))
    assert n._rate_ok() is True
    assert n._rate_ok() is True
    assert n._rate_ok() is False


# ── health thresholds ──────────────────────────────────────────────────────────

def test_health_crossings():
    n = PushoverNotifier(mk())
    # temp over, disk under -> only temp
    only_temp = n.maybe_health_from_heartbeat("N1", {"temp_c": 81, "disk_pct": 50})
    assert [k for k, _ in only_temp] == ["temp"]
    # both over
    kinds = {k for k, _ in n.maybe_health_from_heartbeat("N1", {"temp_c": 85, "disk_pct": 96})}
    assert kinds == {"temp", "disk"}
    # both under -> nothing
    assert n.maybe_health_from_heartbeat("N1", {"temp_c": 60, "disk_pct": 50}) == []


async def test_health_disabled():
    n = notif(pushover_health_enabled=False)
    assert await n.notify_health("N1", "temp", "hot") is False


# ── message body ───────────────────────────────────────────────────────────────

def test_alert_body_contains_fields():
    n = PushoverNotifier(mk())
    body = n._alert_body(alert(drone_id="DJI-1"))
    assert "DJI-1" in body and "40.00000" in body and "N1" in body


async def test_send_test_disabled():
    n = PushoverNotifier(mk(pushover_enabled=False))
    out = await n.send_test()
    assert out["sent"] is False


def test_is_active():
    assert notif().is_active() is True
    assert notif(pushover_enabled=False).is_active() is False


async def test_fire_runs_and_cleans_up():
    n = notif()
    n.fire(n.notify_alert(alert()))
    assert len(n._tasks) == 1          # reference held while in flight
    await asyncio.sleep(0.01)
    n._send.assert_awaited()           # the scheduled coro actually ran
    assert len(n._tasks) == 0          # done-callback discarded it
