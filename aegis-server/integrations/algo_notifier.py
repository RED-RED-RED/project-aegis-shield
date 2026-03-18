"""
integrations/algo_notifier.py
==============================
Algo 8128 IP Visual Alerter integration.

Triggers a strobe flash on the Algo 8128 when drone threat levels change.
Wires into the alert engine dispatch pipeline; fully disabled (zero overhead)
when ALGO_8128_ENABLED=false.

Flash pattern mapping
---------------------
  LOW    → pattern 1, intensity 1
  MEDIUM → pattern 5, intensity 2
  HIGH   → pattern 9, intensity 3

Trigger logic
-------------
  - Fires on escalation: LOW→MEDIUM, MEDIUM→HIGH, or any new HIGH detection.
  - Does NOT re-trigger for the same threat level within the cooldown window
    (default 30 s, configurable via ALGO_8128_COOLDOWN_SECONDS).
  - clear() is a separate command that sends {"action": "stop"} to the device.
"""

import logging
import time
from typing import Optional

import httpx

from core.config import Settings

log = logging.getLogger("algo_notifier")

# threat_level (lower-cased) → (pattern, intensity)
_PATTERNS: dict[str, tuple[int, int]] = {
    "low":    (1, 1),
    "medium": (5, 2),
    "high":   (9, 3),
}

# Numeric rank for escalation comparisons
_RANK: dict[str, int] = {"": -1, "low": 0, "medium": 1, "high": 2}


class AlgoNotifier:
    """
    Singleton-friendly notifier for the Algo 8128 IP Visual Alerter.

    Thread/task safety: all public methods are async and safe to call from
    any asyncio task.  Internal state is not protected by a lock because
    CPython's GIL makes dict reads/writes effectively atomic for our
    single-threaded asyncio event loop.
    """

    def __init__(self, settings: Settings) -> None:
        self._enabled: bool = settings.algo_8128_enabled
        self._url: str = settings.algo_8128_url.rstrip("/")
        self._key: str = settings.algo_8128_api_key
        self._cooldown: int = settings.algo_8128_cooldown_seconds

        # drone_id → (last_trigger_ts, last_trigger_level)
        self._drone_state: dict[str, tuple[float, str]] = {}

        # Metadata exposed via the /status endpoint
        self._last_trigger_ts: Optional[float] = None
        self._last_trigger_level: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def url(self) -> str:
        return self._url

    @property
    def last_trigger_ts(self) -> Optional[float]:
        return self._last_trigger_ts

    @property
    def last_trigger_level(self) -> Optional[str]:
        return self._last_trigger_level

    async def trigger(self, threat_level: str, drone_id: str) -> bool:
        """
        Trigger the strobe for *drone_id* at *threat_level*.

        Returns True when the HTTP request was sent successfully.
        Returns False (without raising) on any failure or if the trigger is
        suppressed by the escalation / cooldown logic.
        """
        if not self._enabled:
            return False

        level = threat_level.lower()
        if level not in _PATTERNS:
            log.debug(f"Unknown threat level '{threat_level}' — skipping Algo trigger")
            return False

        now = time.monotonic()
        prev_ts, prev_level = self._drone_state.get(drone_id, (0.0, ""))

        # Suppress if same-or-lower level is within the cooldown window.
        # Escalations always bypass the cooldown.
        if _RANK.get(level, -1) <= _RANK.get(prev_level, -1):
            if now - prev_ts < self._cooldown:
                return False

        pattern, intensity = _PATTERNS[level]
        payload = {"pattern": pattern, "intensity": intensity}

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self._url}/api/v1/trigger",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Algo 8128 trigger failed for drone {drone_id}: {exc}")
            return False

        self._drone_state[drone_id] = (now, level)
        self._last_trigger_ts = time.time()
        self._last_trigger_level = level
        log.info(
            f"Algo 8128 triggered — drone={drone_id} level={level} "
            f"pattern={pattern} intensity={intensity}"
        )
        return True

    async def clear(self) -> bool:
        """
        Send the stop command to the strobe.

        Returns True on success, False on any failure (never raises).
        """
        if not self._enabled:
            return False

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self._url}/api/v1/trigger",
                    json={"action": "stop"},
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Algo 8128 clear failed: {exc}")
            return False

        log.info("Algo 8128 strobe cleared.")
        return True

    async def test_flash(self) -> tuple[bool, float]:
        """
        Fire a brief test pattern (pattern 3, intensity 2, 5 s duration).
        Returns (success, latency_ms).  Never raises.
        """
        if not self._enabled:
            return False, 0.0

        payload = {"pattern": 3, "intensity": 2, "duration": 5}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self._url}/api/v1/trigger",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
            latency_ms = (time.monotonic() - start) * 1000
            log.info(f"Algo 8128 test flash succeeded in {latency_ms:.0f} ms")
            return True, latency_ms
        except Exception as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - start) * 1000
            log.warning(f"Algo 8128 test flash failed: {exc}")
            return False, latency_ms

    def clear_drone_state(self, drone_id: str) -> None:
        """Remove per-drone trigger tracking (call when drone goes offline)."""
        self._drone_state.pop(drone_id, None)

    def get_drone_level(self, drone_id: str) -> str:
        """Return the last triggered level for a drone, or '' if not tracked."""
        _, level = self._drone_state.get(drone_id, (0.0, ""))
        return level
