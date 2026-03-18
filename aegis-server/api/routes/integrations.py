"""
api/routes/integrations.py
===========================
REST endpoints for hardware integration status and testing.

Routes
------
  GET  /api/integrations/algo/status  — Algo 8128 connection status + last trigger info
  POST /api/integrations/algo/test    — Fire a test flash to verify strobe connectivity
"""

from fastapi import APIRouter

from integrations.algo_notifier import AlgoNotifier
from mqtt.alert_engine import _algo

router = APIRouter()


@router.get("/algo/status")
async def algo_status():
    """
    Return Algo 8128 enabled state, configured URL, and last trigger metadata.
    The API key is never included in the response.
    """
    return {
        "enabled": _algo.enabled,
        "url": _algo.url,
        "last_trigger_ts": _algo.last_trigger_ts,
        "last_trigger_level": _algo.last_trigger_level,
    }


@router.post("/algo/test")
async def algo_test():
    """
    Trigger a brief test flash (pattern 3, intensity 2, 5 s) so operators can
    verify the strobe is reachable without needing a live drone detection.
    Returns success status and round-trip latency in milliseconds.
    """
    if not _algo.enabled:
        return {"success": False, "latency_ms": 0.0, "detail": "Algo 8128 integration is disabled"}

    success, latency_ms = await _algo.test_flash()
    return {
        "success": success,
        "latency_ms": round(latency_ms, 1),
        "detail": "Test flash sent" if success else "Request failed — check URL and API key",
    }
