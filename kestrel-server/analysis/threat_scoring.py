"""
analysis/threat_scoring.py
==========================
Multi-factor threat score (0–100) assigned to every active drone.

The score is a weighted sum of risk indicators, each normalised 0–1.
A score ≥ 70 is HIGH, 40–70 is MEDIUM, < 40 is LOW.

Factors
-------
Factor                          Weight  Rationale
────────────────────────────────────────────────────────────────────────────
no_operator_id                   30     FAA mandates operator ID in RID
position_mismatch (MLAT)         25     Spoofed coordinates
high_altitude_no_rid             15     Above 400ft without waiver
unknown_ua_type                   8     Unclassified aircraft type
single_node_only                  7     Can't triangulate; harder to verify
high_speed                        8     Above realistic drone speeds
stale_gps                         4     Old timestamp → replayed broadcast
no_description                    3     Missing self-ID string (minor)
────────────────────────────────────────────────────────────────────────────
Total possible:                 100

Each factor returns a normalised float in [0.0, 1.0].
"""

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("threat")

# ── Threat levels ──────────────────────────────────────────────────────────────
HIGH_THRESHOLD   = 70
MEDIUM_THRESHOLD = 40

# ── Physical constants ─────────────────────────────────────────────────────────
FAA_MAX_ALT_FT   = 400.0
FAA_MAX_ALT_M    = FAA_MAX_ALT_FT * 0.3048   # 121.92 m
MAX_SANE_SPEED   = 50.0   # m/s — above this is physically implausible for consumer drones
HIGH_SPEED_WARN  = 25.0   # m/s — above this raises concern

# ── Factor weights (must sum to 100) ──────────────────────────────────────────
WEIGHTS = {
    "no_operator_id":         30,
    "position_mismatch":      25,
    "high_altitude_no_rid":   15,
    "unknown_ua_type":         8,
    "single_node_only":        7,
    "high_speed":              8,
    "stale_gps":               4,
    "no_description":          3,
}
assert sum(WEIGHTS.values()) == 100, "Weights must sum to 100"


@dataclass
class ThreatFactors:
    """Raw factor values (0.0–1.0) and the final score."""
    no_operator_id:       float = 0.0
    position_mismatch:    float = 0.0
    high_altitude_no_rid: float = 0.0
    unknown_ua_type:      float = 0.0
    single_node_only:     float = 0.0
    high_speed:           float = 0.0
    stale_gps:            float = 0.0
    no_description:       float = 0.0

    score:      float = 0.0         # Weighted total 0–100
    level:      str   = "low"       # "low" | "medium" | "high"
    computed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "score":              round(self.score, 1),
            "level":              self.level,
            "factors": {
                "no_operator_id":       round(self.no_operator_id, 3),
                "position_mismatch":    round(self.position_mismatch, 3),
                "high_altitude_no_rid": round(self.high_altitude_no_rid, 3),
                "unknown_ua_type":      round(self.unknown_ua_type, 3),
                "single_node_only":     round(self.single_node_only, 3),
                "high_speed":           round(self.high_speed, 3),
                "stale_gps":            round(self.stale_gps, 3),
                "no_description":       round(self.no_description, 3),
            },
            "computed_at": self.computed_at,
        }


class ThreatScorer:
    """
    Stateless scorer. Call score() with the current drone state and optional
    MLAT result. Returns a ThreatFactors dataclass.
    """

    def score(
        self,
        drone: dict,
        mlat_result=None,   # TrilaterationResult | None
    ) -> ThreatFactors:
        """
        drone: dict matching the drone_tracks table schema
        mlat_result: TrilaterationResult from the trilateration engine, or None
        """
        f = ThreatFactors()

        # ── Factor 1: No operator ID ───────────────────────────────────────
        # Full weight if completely absent; partial if suspiciously short
        op_id = (drone.get("operator_id") or "").strip()
        if not op_id:
            f.no_operator_id = 1.0
        elif len(op_id) < 6:
            # Very short ID — likely placeholder or truncated
            f.no_operator_id = 0.5

        # ── Factor 2: Position mismatch (MLAT vs. broadcast) ───────────────
        if mlat_result is not None:
            # Sigmoid centred at 250m: 0→0.02, 250m→0.5, 500m→0.88, 1000m→0.99
            f.position_mismatch = _sigmoid(
                (mlat_result.mismatch_m - 250.0) / 150.0
            )

            # Null-island special case: broadcast (0,0) always suspicious
            blat = drone.get("lat", 0.0) or 0.0
            blon = drone.get("lon", 0.0) or 0.0
            if abs(blat) < 0.01 and abs(blon) < 0.01:
                f.position_mismatch = max(f.position_mismatch, 0.9)

        elif (drone.get("lat", 0.0) or 0.0) == 0.0 and (drone.get("lon", 0.0) or 0.0) == 0.0:
            # No MLAT but coordinates are (0,0) — still suspicious
            f.position_mismatch = 0.85

        # ── Factor 3: High altitude without operator ID ────────────────────
        alt_agl = drone.get("height_agl") or drone.get("alt_baro") or 0.0
        if alt_agl > FAA_MAX_ALT_M and not op_id:
            # Linear ramp from 400ft to 800ft, clamped to 1.0
            f.high_altitude_no_rid = min(
                (alt_agl - FAA_MAX_ALT_M) / FAA_MAX_ALT_M, 1.0
            )
        elif alt_agl > FAA_MAX_ALT_M * 1.5 and op_id:
            # Even with ID, very high altitude is suspicious
            f.high_altitude_no_rid = 0.3

        # ── Factor 4: Unknown / suspicious UA type ─────────────────────────
        ua = (drone.get("ua_type") or "").lower()
        if not ua or ua in ("none", "unknown_0", "other"):
            f.unknown_ua_type = 1.0
        elif ua == "ground_obstacle":
            # Ground obstacle reporting altitude → definitely spoofed
            f.unknown_ua_type = 0.9

        # ── Factor 5: Single node only ─────────────────────────────────────
        detecting_nodes = drone.get("detecting_nodes") or []
        if len(detecting_nodes) == 1:
            f.single_node_only = 1.0
        elif len(detecting_nodes) == 2:
            f.single_node_only = 0.4

        # ── Factor 6: Speed anomaly ────────────────────────────────────────
        speed = drone.get("speed_h") or 0.0
        if speed >= MAX_SANE_SPEED:
            # Physically implausible for a consumer drone
            f.high_speed = 1.0
        elif speed > HIGH_SPEED_WARN:
            # Ramp from warn to max
            f.high_speed = (speed - HIGH_SPEED_WARN) / (MAX_SANE_SPEED - HIGH_SPEED_WARN)

        # ── Factor 7: Stale GPS timestamp ─────────────────────────────────
        # The Location message contains a 0.1s-resolution timestamp of last GPS fix.
        # If last_seen is old, the broadcast may be replayed.
        last_seen = drone.get("last_seen")
        if last_seen:
            try:
                from datetime import datetime, timezone
                if isinstance(last_seen, str):
                    ls_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                else:
                    ls_dt = last_seen
                age_s = (datetime.now(timezone.utc) - ls_dt).total_seconds()
                # Old data: 0 at <5s, 1.0 at >120s
                if age_s > 120:
                    f.stale_gps = 1.0
                elif age_s > 5:
                    f.stale_gps = (age_s - 5) / 115.0
            except Exception:
                pass

        # ── Factor 8: No self-ID description ──────────────────────────────
        if not (drone.get("description") or "").strip():
            f.no_description = 1.0

        # ── Weighted sum ───────────────────────────────────────────────────
        f.score = sum(
            WEIGHTS[name] * getattr(f, name)
            for name in WEIGHTS
        )
        f.score = round(min(f.score, 100.0), 1)

        f.level = (
            "high"   if f.score >= HIGH_THRESHOLD   else
            "medium" if f.score >= MEDIUM_THRESHOLD else
            "low"
        )

        return f


# ── Utility ───────────────────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
