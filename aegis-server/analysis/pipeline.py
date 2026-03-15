"""
analysis/pipeline.py
====================
Orchestration layer. Called by the MQTT subscriber after each detection is
written to the database. Runs trilateration + threat scoring and writes
results back, triggering WebSocket updates and alerts as needed.

Integration point:
    In mqtt/subscriber.py, after the DB write:

        from analysis.pipeline import AnalysisPipeline
        _pipeline = AnalysisPipeline(settings)   # module-level singleton

        async def _handle_detection(self, payload):
            ...
            # After DB writes:
            await _pipeline.process(event, conn)

The pipeline is intentionally fire-and-forget: if it raises, the detection
is already safely stored. Errors are logged but don't bubble up.
"""

import logging
import time
from typing import Optional

from analysis.trilateration import TrilaterationEngine, NodeObservation, TrilaterationResult
from analysis.threat_scoring import ThreatScorer, ThreatFactors
from core.config import Settings
from models.schemas import DetectionEvent

log = logging.getLogger("pipeline")


class AnalysisPipeline:
    """
    Singleton that owns the trilateration engine and threat scorer.
    Thread-safe for asyncio use (single event loop).
    """

    def __init__(self, settings: Settings):
        self.settings     = settings
        self._trilat      = TrilaterationEngine()
        self._scorer      = ThreatScorer()
        self._last_scores: dict[str, float] = {}   # drone_id → last_score (for change detection)

    async def process(self, event: DetectionEvent, conn) -> None:
        """
        Run the full analysis pipeline for one detection event.
        conn: asyncpg connection (already in a transaction is fine)
        """
        try:
            await self._run(event, conn)
        except Exception as e:
            log.error(f"Analysis pipeline error for {event.drone.id}: {e}", exc_info=True)

    async def _run(self, event: DetectionEvent, conn) -> None:
        drone = event.drone

        # ── Step 1: Trilateration ──────────────────────────────────────────
        obs = NodeObservation(
            node_id  = event.node_id,
            node_lat = event.node_position.lat,
            node_lon = event.node_position.lon,
            node_alt = event.node_position.alt,
            rssi     = event.rssi or -80,
            ts       = event.ts,
        )

        mlat: Optional[TrilaterationResult] = self._trilat.update(
            drone_id      = drone.id,
            broadcast_lat = drone.lat,
            broadcast_lon = drone.lon,
            observation   = obs,
        )

        # ── Step 2: Fetch current drone state from DB ──────────────────────
        drone_row = await conn.fetchrow(
            "SELECT * FROM drone_tracks WHERE drone_id = $1", drone.id
        )
        if not drone_row:
            return

        drone_dict = dict(drone_row)

        # ── Step 3: Threat scoring ─────────────────────────────────────────
        threat: ThreatFactors = self._scorer.score(drone_dict, mlat)

        # ── Step 4: Persist results ────────────────────────────────────────
        await self._persist(conn, drone.id, threat, mlat)

        # ── Step 5: Alert on significant changes ──────────────────────────
        prev_score = self._last_scores.get(drone.id, 0.0)
        self._last_scores[drone.id] = threat.score

        await self._maybe_alert(conn, drone.id, threat, mlat, prev_score)

        log.debug(
            f"[{drone.id}] score={threat.score:.0f} ({threat.level})"
            + (f" mismatch={mlat.mismatch_m:.0f}m" if mlat else "")
        )

    # ── Persistence ────────────────────────────────────────────────────────

    async def _persist(
        self,
        conn,
        drone_id: str,
        threat: ThreatFactors,
        mlat: Optional[TrilaterationResult],
    ) -> None:
        """Write threat score and MLAT result into drone_tracks."""

        # Ensure the extra columns exist (idempotent — no-op if already present)
        await _ensure_analysis_columns(conn)

        if mlat:
            await conn.execute("""
                UPDATE drone_tracks SET
                    threat_score       = $2,
                    threat_level       = $3,
                    threat_factors     = $4,
                    mlat_lat           = $5,
                    mlat_lon           = $6,
                    mlat_radius_m      = $7,
                    mlat_mismatch_m    = $8,
                    spoof_confidence   = $9,
                    mlat_node_count    = $10,
                    analysis_updated_at = NOW()
                WHERE drone_id = $1
            """,
                drone_id,
                threat.score,
                threat.level,
                str(threat.to_dict()),     # JSON stored as text for simplicity
                mlat.est_lat,
                mlat.est_lon,
                mlat.est_radius_m,
                mlat.mismatch_m,
                mlat.spoof_confidence,
                mlat.node_count,
            )
        else:
            await conn.execute("""
                UPDATE drone_tracks SET
                    threat_score        = $2,
                    threat_level        = $3,
                    threat_factors      = $4,
                    analysis_updated_at = NOW()
                WHERE drone_id = $1
            """,
                drone_id,
                threat.score,
                threat.level,
                str(threat.to_dict()),
            )

    # ── Alert generation ───────────────────────────────────────────────────

    async def _maybe_alert(
        self,
        conn,
        drone_id: str,
        threat: ThreatFactors,
        mlat: Optional[TrilaterationResult],
        prev_score: float,
    ) -> None:
        """Generate analysis-driven alerts on threshold crossings."""
        from mqtt.ws_broadcaster import WSBroadcaster
        broadcaster = WSBroadcaster()

        # Alert: score crossed into HIGH
        if threat.score >= 70 and prev_score < 70:
            top_factors = _top_factors(threat)
            await _insert_alert(conn, broadcaster, {
                "level":    "high",
                "category": "threat_score",
                "drone_id": drone_id,
                "title":    f"High threat score — {drone_id} ({threat.score:.0f}/100)",
                "description": f"Factors: {top_factors}",
            })

        # Alert: position mismatch detected via MLAT
        if mlat and mlat.mismatch_m > 250 and mlat.spoof_confidence > 0.6:
            await _insert_alert(conn, broadcaster, {
                "level":    "high",
                "category": "position_mismatch",
                "drone_id": drone_id,
                "lat":      mlat.est_lat,
                "lon":      mlat.est_lon,
                "title":    f"Position mismatch — {drone_id}",
                "description": (
                    f"Broadcast position is {mlat.mismatch_m:.0f}m from MLAT estimate "
                    f"(spoof confidence {mlat.spoof_confidence*100:.0f}%). "
                    f"Broadcast: {mlat.broadcast_lat:.4f},{mlat.broadcast_lon:.4f} "
                    f"vs. estimated {mlat.est_lat:.4f},{mlat.est_lon:.4f}"
                ),
            })


# ── Helpers ────────────────────────────────────────────────────────────────────

def _top_factors(threat: ThreatFactors, n: int = 3) -> str:
    """Return the n highest-weight contributing factors as a readable string."""
    from analysis.threat_scoring import WEIGHTS
    contributions = {
        name: WEIGHTS[name] * getattr(threat, name)
        for name in WEIGHTS
    }
    top = sorted(contributions, key=contributions.__getitem__, reverse=True)[:n]
    parts = [f"{name.replace('_',' ')} ({contributions[name]:.0f}pt)" for name in top if contributions[name] > 0]
    return ', '.join(parts) if parts else 'none'


async def _insert_alert(conn, broadcaster, alert: dict) -> None:
    """Insert alert into DB and broadcast to WS clients."""
    import time
    try:
        row = await conn.fetchrow("""
            INSERT INTO alerts (level, category, drone_id, node_id, title, description, lat, lon)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id, created_at, level, category, drone_id, node_id,
                      title, description, lat, lon, acknowledged
        """,
            alert.get("level", "medium"),
            alert.get("category", "analysis"),
            alert.get("drone_id"),
            alert.get("node_id"),
            alert.get("title"),
            alert.get("description"),
            alert.get("lat"),
            alert.get("lon"),
        )
        if row:
            await broadcaster.broadcast_alert(dict(row))
    except Exception as e:
        log.warning(f"Alert insert failed: {e}")


_analysis_columns_created = False

async def _ensure_analysis_columns(conn) -> None:
    """Lazily add analysis columns to drone_tracks if they don't exist yet."""
    global _analysis_columns_created
    if _analysis_columns_created:
        return

    columns = {
        "threat_score":        "REAL",
        "threat_level":        "TEXT",
        "threat_factors":      "TEXT",
        "mlat_lat":            "DOUBLE PRECISION",
        "mlat_lon":            "DOUBLE PRECISION",
        "mlat_radius_m":       "REAL",
        "mlat_mismatch_m":     "REAL",
        "spoof_confidence":    "REAL",
        "mlat_node_count":     "INT",
        "analysis_updated_at": "TIMESTAMPTZ",
    }

    for col, dtype in columns.items():
        try:
            await conn.execute(
                f"ALTER TABLE drone_tracks ADD COLUMN IF NOT EXISTS {col} {dtype};"
            )
        except Exception:
            pass   # Column likely already exists

    _analysis_columns_created = True
