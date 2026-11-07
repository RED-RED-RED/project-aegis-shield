"""
db/database.py
==============
Async PostgreSQL / TimescaleDB connection pool via asyncpg.

Schema created on startup if it doesn't exist:
  - nodes           : ARGUS node registry + last heartbeat
  - detections      : every RID detection event (TimescaleDB hypertable)
  - drone_tracks    : deduplicated drone state (one row per drone, upserted)
  - alerts          : generated alert events
  - rf_events       : SDR RF burst events (optional)

TimescaleDB hypertable is created on `detections` with time column `detected_at`,
chunked by 1 day. This gives fast time-range queries and automatic compression.
"""

import logging
from typing import AsyncGenerator

import asyncpg
from asyncpg import Pool

from core.config import get_settings

log = logging.getLogger("db")

_pool: Pool | None = None


async def get_pool() -> Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


async def get_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_db():
    global _pool
    settings = get_settings()

    _pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    log.info("Database pool created.")
    await _create_schema(_pool)


async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def _create_schema(pool: Pool):
    """Create tables and TimescaleDB hypertable if they don't exist."""
    async with pool.acquire() as conn:
        # Enable TimescaleDB extension
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
        except Exception as e:
            log.warning(f"Could not enable TimescaleDB extension (non-fatal if already enabled): {e}")

        # ---- nodes table ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id         TEXT PRIMARY KEY,
                site_name       TEXT,
                status          TEXT NOT NULL DEFAULT 'offline',
                last_seen       TIMESTAMPTZ,
                lat             DOUBLE PRECISION,
                lon             DOUBLE PRECISION,
                alt             DOUBLE PRECISION,
                gps_fix         BOOLEAN DEFAULT FALSE,
                satellites      INT DEFAULT 0,
                cpu_pct         REAL,
                mem_pct         REAL,
                disk_pct        REAL,
                temp_c          REAL,
                uptime_s        INT,
                radios          TEXT[],
                jamming_state   VARCHAR,
                spoofing_state  VARCHAR,
                survey_complete BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # Idempotently add new columns to existing deployments
        for col, dtype in [
            ("jamming_state",   "VARCHAR"),
            ("spoofing_state",  "VARCHAR"),
            ("survey_complete", "BOOLEAN DEFAULT FALSE"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE nodes ADD COLUMN IF NOT EXISTS {col} {dtype};"
                )
            except Exception:
                pass

        # ---- detections table (TimescaleDB hypertable) ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id              BIGSERIAL,
                detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                node_id         TEXT NOT NULL,
                transport       TEXT NOT NULL,   -- wifi_nan | bluetooth
                rssi            INT,
                src_addr        TEXT,

                -- Drone position at time of detection
                drone_id        TEXT NOT NULL,
                drone_lat       DOUBLE PRECISION,
                drone_lon       DOUBLE PRECISION,
                alt_baro        REAL,
                alt_geo         REAL,
                height_agl      REAL,
                speed_h         REAL,
                speed_v         REAL,
                heading         REAL,
                status          TEXT,

                -- Identity
                id_type         TEXT,
                ua_type         TEXT,
                operator_id     TEXT,
                operator_lat    DOUBLE PRECISION,
                operator_lon    DOUBLE PRECISION,
                description     TEXT,

                -- Node position at time of detection
                node_lat        DOUBLE PRECISION,
                node_lon        DOUBLE PRECISION,
                node_alt        DOUBLE PRECISION,

                PRIMARY KEY (id, detected_at)
            );
        """)

        # Create TimescaleDB hypertable (idempotent via IF NOT EXISTS flag)
        try:
            await conn.execute("""
                SELECT create_hypertable(
                    'detections', 'detected_at',
                    chunk_time_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );
            """)
            log.info("TimescaleDB hypertable ready on detections.detected_at")
        except Exception as e:
            log.warning(f"Hypertable creation skipped (may already exist): {e}")

        # Index for fast drone_id + time queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detections_drone_id_time
            ON detections (drone_id, detected_at DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detections_node_id_time
            ON detections (node_id, detected_at DESC);
        """)

        # ---- drone_tracks table (live state, one row per drone) ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drone_tracks (
                drone_id        TEXT PRIMARY KEY,
                first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_node_id    TEXT,
                last_transport  TEXT,
                last_rssi       INT,

                -- Latest position
                lat             DOUBLE PRECISION,
                lon             DOUBLE PRECISION,
                alt_baro        REAL,
                alt_geo         REAL,
                height_agl      REAL,
                speed_h         REAL,
                speed_v         REAL,
                heading         REAL,
                status          TEXT,

                -- Identity
                id_type         TEXT,
                ua_type         TEXT,
                operator_id     TEXT,
                operator_lat    DOUBLE PRECISION,
                operator_lon    DOUBLE PRECISION,
                description     TEXT,

                -- Compliance
                has_valid_rid   BOOLEAN DEFAULT FALSE,
                detection_count INT DEFAULT 1,
                detecting_nodes TEXT[],

                -- Analysis results (written by pipeline.py)
                threat_score        REAL,
                threat_level        TEXT,
                threat_factors      TEXT,
                mlat_lat            DOUBLE PRECISION,
                mlat_lon            DOUBLE PRECISION,
                mlat_radius_m       REAL,
                mlat_mismatch_m     REAL,
                spoof_confidence    REAL,
                mlat_node_count     INT,
                analysis_updated_at TIMESTAMPTZ
            );
        """)

        # Idempotently add analysis columns to existing deployments
        for col, dtype in [
            ("threat_score",        "REAL"),
            ("threat_level",        "TEXT"),
            ("threat_factors",      "TEXT"),
            ("mlat_lat",            "DOUBLE PRECISION"),
            ("mlat_lon",            "DOUBLE PRECISION"),
            ("mlat_radius_m",       "REAL"),
            ("mlat_mismatch_m",     "REAL"),
            ("spoof_confidence",    "REAL"),
            ("mlat_node_count",     "INT"),
            ("analysis_updated_at", "TIMESTAMPTZ"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE drone_tracks ADD COLUMN IF NOT EXISTS {col} {dtype};"
                )
            except Exception:
                pass

        # Index for WebSocket live-state query (last_seen filtered on every push)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drone_tracks_last_seen
            ON drone_tracks (last_seen DESC);
        """)

        # ---- alerts table ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id              BIGSERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                level           TEXT NOT NULL,   -- high | medium | low
                category        TEXT NOT NULL,   -- no_rid | speed | altitude | node_offline | rf_anomaly
                drone_id        TEXT,
                node_id         TEXT,
                title           TEXT NOT NULL,
                description     TEXT,
                lat             DOUBLE PRECISION,
                lon             DOUBLE PRECISION,
                acknowledged    BOOLEAN DEFAULT FALSE,
                acknowledged_at TIMESTAMPTZ
            );
        """)

        # ref_id ties an alert back to the thing it is about when that is not a
        # drone_id — e.g. an RF contact_id for an rf_no_rid alert — so the UI can
        # hide that contact's map point when the alert is acknowledged.
        await conn.execute(
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ref_id TEXT;"
        )

        # Index for alert list queries (unacknowledged alerts, sorted by time)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_unacked_created
            ON alerts (acknowledged, created_at DESC);
        """)

        # ---- rf_events table ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rf_events (
                id              BIGSERIAL PRIMARY KEY,
                detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                node_id         TEXT NOT NULL,
                freq_hz         BIGINT,
                power_db        REAL,
                snr_db          REAL,
                node_lat        DOUBLE PRECISION,
                node_lon        DOUBLE PRECISION,
                node_alt        DOUBLE PRECISION
            );
        """)

        # ---- rf_contacts table (live state, one row per classified emitter) ----
        # An RF contact is a link the SDR classified (ELRS, DJI, FPV video, ...)
        # without decoding Remote ID. It is NOT a drone_track; it may or may not
        # correlate with one. contact_id defaults to "<node>:<type>:<band>".
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rf_contacts (
                contact_id          TEXT PRIMARY KEY,
                first_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                signal_type         TEXT NOT NULL,
                band                TEXT NOT NULL,
                center_freq_hz      BIGINT,
                bandwidth_hz        BIGINT,
                last_rssi           REAL,
                best_rssi           REAL,
                confidence          REAL,
                hop_rate_hz         REAL,
                detecting_nodes     TEXT[] DEFAULT '{}',
                detection_count     INT DEFAULT 0,
                correlated          BOOLEAN DEFAULT FALSE,
                correlated_drone_id TEXT,
                compliant           BOOLEAN,
                est_lat             DOUBLE PRECISION,
                est_lon             DOUBLE PRECISION,
                est_radius_m        REAL,
                threat_score        REAL,
                threat_level        TEXT
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rf_contacts_last_seen
                ON rf_contacts (last_seen DESC);
        """)

        # ---- node_health time-series (health history for the System tab) ----
        # One row per heartbeat per node, plus periodic rows for the server
        # itself (node_id = '__server__'). temp_c/satellites/gps_fix are node-
        # only; db_ok/mqtt_ok are server-only. Kept ~7 days (retention below).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS node_health (
                time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                node_id     TEXT NOT NULL,
                cpu_pct     REAL,
                mem_pct     REAL,
                disk_pct    REAL,
                temp_c      REAL,
                uptime_s    INT,
                satellites  INT,
                gps_fix     BOOLEAN,
                db_ok       BOOLEAN,
                mqtt_ok     BOOLEAN
            );
        """)
        try:
            await conn.execute("""
                SELECT create_hypertable(
                    'node_health', 'time',
                    chunk_time_interval => INTERVAL '1 day',
                    if_not_exists => TRUE
                );
            """)
            log.info("TimescaleDB hypertable ready on node_health.time")
        except Exception as e:
            log.warning(f"node_health hypertable creation skipped (may already exist): {e}")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_node_health_node_time
                ON node_health (node_id, time DESC);
        """)
        # Keep ~1 week of health history; drop older chunks automatically.
        try:
            await conn.execute("""
                SELECT add_retention_policy('node_health', INTERVAL '7 days',
                                            if_not_exists => TRUE);
            """)
        except Exception as e:
            log.warning(f"node_health retention policy skipped: {e}")

        log.info("Database schema ready.")
