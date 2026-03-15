"""
core/auth.py
============
Optional API key authentication.

If AEGIS_API_KEY is set in the environment, all /api/* requests must include:
    X-Api-Key: <your-key>

If AEGIS_API_KEY is empty (default), auth is disabled — suitable for trusted
LAN deployments or development. The /health endpoint is always unauthenticated.
"""

from fastapi import Header, HTTPException, status
from core.config import get_settings


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """FastAPI dependency — validates X-Api-Key header when auth is enabled."""
    settings = get_settings()
    if not settings.api_key:
        return  # Auth disabled
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
