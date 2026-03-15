"""
analysis/trilateration.py
==========================
RSSI-based position estimation using weighted least-squares trilateration.

Theory
------
Each ARGUS node measures the received signal strength (RSSI, dBm) of a drone's
Remote ID transmission. RSSI decreases with distance following the log-distance
path loss model:

    RSSI(d) = RSSI_ref - 10 * n * log10(d / d_ref)

Rearranged to give distance estimate:

    d = d_ref * 10 ^ ((RSSI_ref - RSSI) / (10 * n))

Where:
    RSSI_ref = reference RSSI at 1 metre (typically -40 dBm for 2.4 GHz BLE/WiFi)
    n        = path loss exponent (2.0 = free space, 2.5–4.0 = real world)
    d        = estimated distance in metres

With distances from ≥ 3 nodes we can trilaterate using weighted least-squares
minimisation (Scipy optimize.minimize). Nodes with stronger RSSI (closer to drone)
get higher weight.

Limitations
-----------
- RSSI is noisy. Single-measurement estimates have ±50% distance error.
- Multi-path reflection, foliage, and buildings skew readings.
- This gives a "sanity check" position, not a GPS replacement.
- The result is most useful for detecting SPOOFED coordinates:
  if the trilaterated position disagrees with the broadcast position by > 200m,
  that's a strong spoof indicator.

Output
------
Publishes estimated position + confidence radius + spoof confidence to:
  - drone_tracks.mlat_lat, drone_tracks.mlat_lon, drone_tracks.mlat_radius_m
  - A new 'position_mismatch' alert if broadcast vs. estimated diverge
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger("trilateration")

# ── Per-node calibration overrides (loaded from calibration.yaml) ─────────
# Format: { "ARGUS-01": {"rssi_ref_dbm": -18.4, "path_loss_exp": 2.45}, ... }
_CAL_CACHE: dict = {}
_CAL_LOADED = False

def _load_calibration():
    """Load calibration.yaml if it exists. Called lazily on first use."""
    global _CAL_CACHE, _CAL_LOADED
    if _CAL_LOADED:
        return
    _CAL_LOADED = True

    import os
    import yaml
    from pathlib import Path

    cal_path = Path(os.environ.get(
        "ARGUS_CALIBRATION_PATH",
        Path(__file__).parent / "calibration.yaml"
    ))

    if not cal_path.exists():
        return

    try:
        with open(cal_path) as f:
            doc = yaml.safe_load(f) or {}
        _CAL_CACHE = doc
        nodes = doc.get("nodes", {})
        log.info(
            f"Loaded calibration from {cal_path} "
            f"({len(nodes)} nodes, global n={doc.get('global',{}).get('path_loss_exp','—')})"
        )
    except Exception as e:
        log.warning(f"Could not load calibration.yaml: {e}")


def _node_params(node_id: str) -> tuple[float, float]:
    """Return (rssi_ref_dbm, path_loss_exp) for a node, falling back to globals."""
    _load_calibration()
    nodes  = _CAL_CACHE.get("nodes", {})
    global_ = _CAL_CACHE.get("global", {})

    node_cfg = nodes.get(node_id, {})
    rssi_ref = node_cfg.get("rssi_ref_dbm",
               global_.get("rssi_ref_dbm", RSSI_REF_DBM))
    n        = node_cfg.get("path_loss_exp",
               global_.get("path_loss_exp", PATH_LOSS_EXP))
    return float(rssi_ref), float(n)

# ── Path-loss model parameters ─────────────────────────────────────────────────
RSSI_REF_DBM    = -20.0    # RSSI at 1m for 2.4 GHz (calibrated for 100–1000m range)
PATH_LOSS_EXP   = 2.7      # Environment factor (2.0=free space, 3.5=suburban)
MIN_NODES       = 3        # Need at least 3 nodes for 2D trilateration
MIN_RSSI_DBM    = -100     # Ignore readings below this (too noisy)
MAX_DIST_M      = 3000.0   # Cap estimated distances at 3 km
EARTH_RADIUS_M  = 6_371_000.0

# Spoof detection: if broadcast vs. estimated diverge by more than this, flag it
SPOOF_THRESHOLD_M = 250.0


@dataclass
class NodeObservation:
    node_id:  str
    node_lat: float
    node_lon: float
    node_alt: float
    rssi:     int        # dBm
    ts:       float      # unix timestamp of measurement


@dataclass
class TrilaterationResult:
    drone_id:         str
    est_lat:          float
    est_lon:          float
    est_radius_m:     float   # 68% confidence radius
    broadcast_lat:    float
    broadcast_lon:    float
    mismatch_m:       float   # distance between broadcast and estimated position
    spoof_confidence: float   # 0.0–1.0
    node_count:       int
    ts:               float


class TrilaterationEngine:
    """
    Trilateration engine. Call update() each time a new detection arrives.
    Results are accumulated per drone_id and re-computed when enough nodes
    have recent observations.
    """

    def __init__(self):
        # drone_id → {node_id: NodeObservation}  (most recent per node)
        self._obs: dict[str, dict[str, NodeObservation]] = {}
        self._obs_window_s = 10.0   # Only use observations within last 10 seconds

    def update(
        self,
        drone_id: str,
        broadcast_lat: float,
        broadcast_lon: float,
        observation: NodeObservation,
    ) -> Optional[TrilaterationResult]:
        """
        Add a new RSSI observation. Returns a TrilaterationResult if we have
        enough nodes, otherwise None.
        """
        if observation.rssi < MIN_RSSI_DBM:
            return None

        if drone_id not in self._obs:
            self._obs[drone_id] = {}
        self._obs[drone_id][observation.node_id] = observation

        # Expire stale observations
        now = time.time()
        self._obs[drone_id] = {
            nid: obs for nid, obs in self._obs[drone_id].items()
            if now - obs.ts < self._obs_window_s
        }

        recent = list(self._obs[drone_id].values())
        if len(recent) < MIN_NODES:
            return None

        return self._compute(drone_id, broadcast_lat, broadcast_lon, recent)

    def _compute(
        self,
        drone_id: str,
        broadcast_lat: float,
        broadcast_lon: float,
        observations: list[NodeObservation],
    ) -> Optional[TrilaterationResult]:
        """
        Run weighted least-squares trilateration on the current node observations.
        Works in a local ENU (East-North-Up) coordinate frame centred on the
        centroid of the ARGUS nodes.
        """
        try:
            # Convert all node positions to Cartesian (metres from centroid)
            lats  = np.array([o.node_lat for o in observations])
            lons  = np.array([o.node_lon for o in observations])
            rssis = np.array([o.rssi     for o in observations], dtype=float)

            # Centroid as local origin
            lat0 = float(np.mean(lats))
            lon0 = float(np.mean(lons))

            # Project to ENU metres
            node_xy = np.array([
                _latlng_to_xy(o.node_lat, o.node_lon, lat0, lon0)
                for o in observations
            ])   # shape (N, 2)

            # Distance estimates from RSSI
            # Use per-node calibrated parameters if available
            distances = np.array([
                _rssi_to_distance(rssi, *_node_params(obs.node_id))
                for rssi, obs in zip(rssis, observations)
            ])
            distances = np.clip(distances, 10.0, MAX_DIST_M)

            # Weights: stronger RSSI → higher weight (inverse variance approx.)
            # RSSI variance ≈ 6 dB std dev → distance error ~ 50% per reading
            weights = 1.0 / (distances ** 0.5)
            weights /= weights.sum()

            # ── Weighted centroid as initial guess ──────────────────────────
            x0 = float(np.sum(weights * node_xy[:, 0]))
            y0 = float(np.sum(weights * node_xy[:, 1]))

            # ── Scipy WLS minimisation ──────────────────────────────────────
            from scipy.optimize import minimize

            def cost(xy):
                dx = node_xy[:, 0] - xy[0]
                dy = node_xy[:, 1] - xy[1]
                d_est = np.sqrt(dx**2 + dy**2)
                residuals = d_est - distances
                return float(np.sum(weights * residuals**2))

            result = minimize(cost, x0=[x0, y0], method='Nelder-Mead',
                              options={'maxiter': 1000, 'xatol': 1.0, 'fatol': 0.1})

            if not result.success and result.fun > 1e6:
                log.debug(f"Trilateration failed for {drone_id}: {result.message}")
                return None

            est_x, est_y = result.x

            # ── Back-project to lat/lon ─────────────────────────────────────
            est_lat, est_lon = _xy_to_latlng(est_x, est_y, lat0, lon0)

            # ── Confidence radius (RMSE of residuals in metres) ────────────
            dx = node_xy[:, 0] - est_x
            dy = node_xy[:, 1] - est_y
            d_est = np.sqrt(dx**2 + dy**2)
            residuals = d_est - distances
            rmse = float(np.sqrt(np.mean(residuals**2)))
            # Clamp to a reasonable range
            confidence_radius = float(np.clip(rmse, 20.0, 500.0))

            # ── Mismatch vs. broadcast position ────────────────────────────
            mismatch_m = _haversine_m(est_lat, est_lon, broadcast_lat, broadcast_lon)

            # ── Spoof confidence ────────────────────────────────────────────
            # Sigmoid: 0.5 at threshold, approaching 1.0 beyond 3x threshold
            spoof_confidence = _sigmoid((mismatch_m - SPOOF_THRESHOLD_M) / 150.0)

            return TrilaterationResult(
                drone_id         = drone_id,
                est_lat          = round(est_lat, 6),
                est_lon          = round(est_lon, 6),
                est_radius_m     = round(confidence_radius, 1),
                broadcast_lat    = broadcast_lat,
                broadcast_lon    = broadcast_lon,
                mismatch_m       = round(mismatch_m, 1),
                spoof_confidence = round(spoof_confidence, 3),
                node_count       = len(observations),
                ts               = time.time(),
            )

        except Exception as e:
            log.warning(f"Trilateration error for {drone_id}: {e}")
            return None


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _rssi_to_distance(
    rssi_dbm: float,
    rssi_ref: float = None,
    n: float = None,
) -> float:
    """Log-distance path loss model → metres."""
    ref = rssi_ref if rssi_ref is not None else RSSI_REF_DBM
    exp = n        if n        is not None else PATH_LOSS_EXP
    if rssi_dbm >= ref:
        return 1.0
    exponent = (ref - rssi_dbm) / (10.0 * exp)
    return float(1.0 * (10.0 ** exponent))


def _latlng_to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Convert lat/lon to ENU metres relative to origin (lat0, lon0)."""
    dlat = math.radians(lat - lat0)
    dlon = math.radians(lon - lon0)
    x = dlon * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = dlat * EARTH_RADIUS_M
    return x, y


def _xy_to_latlng(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Back-project ENU metres to lat/lon."""
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    R = EARTH_RADIUS_M
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam/2)**2
    return float(2 * R * math.asin(math.sqrt(a)))


def _sigmoid(x: float) -> float:
    """Standard sigmoid: maps (-inf,inf) → (0,1)."""
    return float(1.0 / (1.0 + math.exp(-x)))
