"""
api/routes/notify.py
===================
Notification status + a test trigger, so operators can verify Pushover config
without waiting for a live alert.
"""

from fastapi import APIRouter

from core.config import get_settings
from mqtt.notifier import notifier

router = APIRouter()


@router.get("/status")
async def notify_status():
    s = get_settings()
    return {
        "enabled":    s.pushover_enabled,
        "configured": bool(s.pushover_token and s.pushover_user),
        "min_level":  s.pushover_min_level,
        "emergency_categories": sorted(s.pushover_emergency_set),
        "health_enabled": s.pushover_health_enabled,
        "cooldown_s": s.pushover_cooldown_s,
        "max_per_hour": s.pushover_max_per_hour,
    }


@router.post("/test")
async def notify_test():
    return await notifier.send_test()
