"""
mqtt/notifier.py
================
Outbound push notifications via Pushover. A thin, fire-and-forget layer hung off
the alert pipeline: when an alert is emitted (and for critical node health), send
a push. Everything here is best-effort — a Pushover hiccup must never block or
crash the alert engine, so every send is wrapped and failures are swallowed.

Behaviour (configurable in core/config.py):
  - HIGH  -> Pushover priority 1 (loud, bypasses the phone's quiet hours)
  - MEDIUM-> priority 0 (normal)
  - categories in pushover_emergency_categories -> priority 2 (Emergency: repeats
    until acknowledged), and those bypass the min-level gate
  - per-drone/node cooldown so one loitering contact doesn't buzz every cycle;
    an escalation to a higher level overrides the cooldown
  - a global max-per-hour cap as a flood guard
  - critical node health (offline is a normal alert; temp/disk crossings push here)
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from core import runtime_settings as rt

log = logging.getLogger("notifier")

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


class PushoverNotifier:
    def __init__(self, settings=None):
        # The module singleton reads settings live from runtime_settings so GUI
        # edits apply without a restart. Tests may inject a fixed Settings.
        self._settings = settings
        # key -> (last_sent_monotonic, last_level_rank)
        self._cooldown: dict[str, tuple[float, int]] = {}
        self._sent_times: list[float] = []   # monotonic timestamps, last hour
        self._tasks: set = set()             # keep refs to in-flight sends

    def is_active(self) -> bool:
        """Notifications configured + enabled — cheap gate for callers."""
        return self._ready()

    def fire(self, coro) -> None:
        """Schedule a notify coroutine fire-and-forget, keeping a reference so
        the task isn't garbage-collected mid-flight. Never blocks the caller."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _g(self, key: str):
        if key == "pushover_emergency_set":
            return self._settings.pushover_emergency_set if self._settings is not None else rt.emergency_set()
        if self._settings is not None:
            return getattr(self._settings, key)
        return rt.get(key)

    # ── public API ────────────────────────────────────────────────────────────

    async def notify_alert(self, alert: dict) -> bool:
        """Push a drone/alert if it clears the level gate and cooldown."""
        if not self._ready():
            return False
        category = alert.get("category", "")
        level = (alert.get("level") or "low").lower()
        emergency = category in self._g("pushover_emergency_set")

        if not emergency and _LEVEL_RANK.get(level, 0) < _LEVEL_RANK.get(self._g("pushover_min_level"), 1):
            return False

        key = f"drone:{alert.get('drone_id')}" if alert.get("drone_id") else f"cat:{category}:{alert.get('node_id')}"
        if not self._cooldown_ok(key, _LEVEL_RANK.get(level, 0)):
            return False

        priority = 2 if emergency else (1 if level == "high" else 0)
        title = f"AEGIS {level.upper()} — {alert.get('title', category)}"
        body = self._alert_body(alert)
        return await self._send(title, body, priority)

    async def notify_health(self, node_id: str, kind: str, detail: str) -> bool:
        """Push a critical node-health event (temp/disk). Cooldown-gated per node+kind."""
        if not self._ready() or not self._g("pushover_health_enabled"):
            return False
        key = f"health:{kind}:{node_id}"
        if not self._cooldown_ok(key, _LEVEL_RANK["high"]):
            return False
        title = f"AEGIS CRITICAL — {node_id}"
        return await self._send(title, detail, priority=1)

    def maybe_health_from_heartbeat(self, node_id: str, system: dict) -> list[tuple[str, str]]:
        """Return (kind, detail) for any critical metric crossing. Pure — the
        caller schedules the actual sends. Keeps threshold logic testable."""
        out = []
        temp = system.get("temp_c")
        disk = system.get("disk_pct")
        temp_crit = self._g("pushover_temp_crit_c")
        disk_crit = self._g("pushover_disk_crit_pct")
        if temp is not None and temp >= temp_crit:
            out.append(("temp", f"CPU temperature {temp:.0f}°C (>= {temp_crit:.0f}°C) on {node_id}"))
        if disk is not None and disk >= disk_crit:
            out.append(("disk", f"Disk usage {disk:.0f}% (>= {disk_crit:.0f}%) on {node_id}"))
        return out

    async def send_test(self) -> dict:
        """Fire a sample push to verify configuration."""
        if not self._g("pushover_enabled"):
            return {"sent": False, "detail": "notifications disabled (pushover_enabled=false)"}
        if not (self._g("pushover_token") and self._g("pushover_user")):
            return {"sent": False, "detail": "missing PUSHOVER_TOKEN or PUSHOVER_USER"}
        ok = await self._send("AEGIS test", "Pushover is configured correctly.", priority=0, count=False)
        return {"sent": ok, "detail": "test push sent" if ok else "Pushover API call failed — check token/user"}

    # ── internals ─────────────────────────────────────────────────────────────

    def _ready(self) -> bool:
        return bool(self._g("pushover_enabled") and self._g("pushover_token") and self._g("pushover_user"))

    def _cooldown_ok(self, key: str, level_rank: int) -> bool:
        """True if allowed now: outside the cooldown window, OR an escalation to a
        strictly higher level than the last push for this key."""
        now = time.monotonic()
        cd = self._g("pushover_cooldown_s")
        # Bound memory: drop entries that are well past their cooldown window.
        if len(self._cooldown) > 256:
            self._cooldown = {k: v for k, v in self._cooldown.items() if (now - v[0]) < cd}
        prev = self._cooldown.get(key)
        if prev is not None:
            last_t, last_rank = prev
            if (now - last_t) < cd and level_rank <= last_rank:
                return False
        self._cooldown[key] = (now, level_rank)
        return True

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        if len(self._sent_times) >= self._g("pushover_max_per_hour"):
            return False
        self._sent_times.append(now)
        return True

    def _alert_body(self, alert: dict) -> str:
        lines = []
        if alert.get("description"):
            lines.append(alert["description"])
        if alert.get("drone_id"):
            lines.append(f"ID: {alert['drone_id']}")
        if alert.get("node_id"):
            lines.append(f"Node: {alert['node_id']}")
        if alert.get("lat") is not None and alert.get("lon") is not None:
            lines.append(f"Loc: {alert['lat']:.5f}, {alert['lon']:.5f}")
        lines.append(time.strftime("%Y-%m-%d %H:%M:%S %Z"))
        return "\n".join(lines)

    async def _send(self, title: str, message: str, priority: int, count: bool = True) -> bool:
        if count and not self._rate_ok():
            log.warning("Pushover hourly cap reached — dropping notification")
            return False
        data = {
            "token": self._g("pushover_token"),
            "user": self._g("pushover_user"),
            "title": title[:250],
            "message": message[:1024],
            "priority": priority,
        }
        if priority == 2:
            data["retry"] = max(30, self._g("pushover_emergency_retry_s"))
            data["expire"] = min(10800, self._g("pushover_emergency_expire_s"))
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(_PUSHOVER_URL, data=data)
            if r.status_code == 200:
                return True
            log.warning(f"Pushover returned HTTP {r.status_code}: {r.text[:200]}")
            return False
        except Exception as e:
            log.debug(f"Pushover send failed: {e}")
            return False


# Module singleton — zero overhead when disabled.
notifier = PushoverNotifier()
