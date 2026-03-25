"""
core/config.py
==============
Server configuration via environment variables.
Uses pydantic-settings for validation and .env file support.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---- API Authentication ----
    # Set to a non-empty string to require X-Api-Key header on all API requests.
    # Leave empty to disable auth (development / trusted LAN only).
    api_key: str = ""

    # ---- Database ----
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "ridmesh"
    db_user: str = "ridmesh"
    db_password: str = "changeme"

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def db_dsn_sync(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ---- MQTT ----
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = "argus"
    mqtt_password: str = "changeme"
    mqtt_topic_prefix: str = "argus"

    # ---- Alert engine ----
    alert_no_rid_min_alt_m: float = 30.0       # Only alert if drone is above this AGL
    alert_speed_threshold_ms: float = 30.0     # Flag suspiciously fast drones
    alert_dedup_window_s: int = 60             # Don't re-alert for same drone within window

    # ---- WebSocket ----
    ws_broadcast_interval_ms: int = 500        # Push live state to clients every N ms

    # ---- Retention ----
    detection_retention_days: int = 30
    alert_retention_days: int = 90

    # ---- Algo 8128 IP Visual Alerter ----
    # Set ALGO_8128_ENABLED=true and configure the remaining vars to enable strobe alerts.
    algo_8128_enabled: bool = False
    algo_8128_url: str = "http://192.168.1.50"
    algo_8128_api_key: str = ""
    algo_8128_cooldown_seconds: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
