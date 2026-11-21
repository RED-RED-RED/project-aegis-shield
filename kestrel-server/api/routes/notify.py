"""
api/routes/notify.py
===================
Notification status + a test trigger, so operators can verify Pushover config
without waiting for a live alert.
"""

from fastapi import APIRouter

from core import runtime_settings as rt
from mqtt.notifier import notifier

router = APIRouter()


@router.get("/status")
async def notify_status():
    return {
        "enabled":    rt.get("pushover_enabled"),
        "configured": bool(rt.get("pushover_token") and rt.get("pushover_user")),
        "min_level":  rt.get("pushover_min_level"),
        "emergency_categories": sorted(rt.emergency_set()),
        "health_enabled": rt.get("pushover_health_enabled"),
        "cooldown_s": rt.get("pushover_cooldown_s"),
        "max_per_hour": rt.get("pushover_max_per_hour"),
    }


@router.post("/test")
async def notify_test():
    return await notifier.send_test()
